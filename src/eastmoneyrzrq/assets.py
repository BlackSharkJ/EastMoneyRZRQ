"""pyecharts 静态资源的探测与临时自举.

正常情况下资源服务由外部进程提供 (例如 QMTStrategy/xtquant_trader). 端口没起时,
如果配置允许且本地能找到 pyecharts-assets 目录, 就在同一个端口上临时起一个静态服务,
整个流程结束后自动关掉.

两个踩过的坑, 决定了这里的实现方式:

1. 探活只用 HEAD, 不下载 1MB 的 echarts.min.js. 外部服务忙的时候响应头很快回来,
   但文件本身可能几秒传不完, 按 GET 判活会把健康的服务误判成不可用.
2. 自举的监听 socket 不开 SO_REUSEADDR. Windows 上这个选项语义与 Linux 不同,
   撞到别人占用的地址时会报 WSAEACCES (10013) 而不是 EADDRINUSE (10048), 难排查;
   而且真让它绑上会造成同一端口两个监听互相抢流量.

临时服务只读本地文件, 绑定在 127.0.0.1, 不对外暴露, 退出时由 ``ensure_assets_server``
的 finally 保证关闭.
"""

import logging
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from os import PathLike
from pathlib import Path
from typing import Final

import httpx

from . import config

logger = logging.getLogger(__name__)

# 与 http.server 基类签名里的 StrPath 保持一致
type StrPath = str | PathLike[str]

# 探活结果: 外部服务可用 / 本次自举的服务可用 / 都不可用
SOURCE_EXTERNAL: Final[str] = "external"
SOURCE_TEMPORARY: Final[str] = "temporary"

# HEAD 不被支持时用带 Range 的 GET 兜底, 只要第一个字节, 不要整个文件
HEAD_UNSUPPORTED_STATUS: Final[frozenset[int]] = frozenset(
    {HTTPStatus.METHOD_NOT_ALLOWED, HTTPStatus.NOT_IMPLEMENTED}
)
RANGE_PROBE_HEADER: Final[dict[str, str]] = {"Range": "bytes=0-0"}

SHUTDOWN_TIMEOUT: Final[float] = 5.0
PROBE_CLIENT_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(
    config.PROBE_CONNECT_TIMEOUT,
    read=config.PROBE_READ_TIMEOUT,
    write=config.PROBE_READ_TIMEOUT,
)


@dataclass(frozen=True, slots=True)
class AssetsService:
    """本次运行实际使用的静态资源服务.

    Attributes
    ----------
    source : str
        SOURCE_EXTERNAL 或 SOURCE_TEMPORARY.
    base_url : str
        实际可用的基础 URL, 末尾带斜杠.
    port : int
        实际使用的端口. 自举时端口被别人占用会落到随机端口, 所以不能只看配置.
    """

    source: str
    base_url: str
    port: int


class _ReusableHTTPServer(ThreadingHTTPServer):
    """不开 SO_REUSEADDR 的 HTTP 服务: 端口被占时直接报 EADDRINUSE, 不抢流量."""

    allow_reuse_address = False
    daemon_threads = True


def _assets_request_handler(assets_dir: Path) -> type[SimpleHTTPRequestHandler]:
    """造一个把 URL 前缀映射到本地资源目录的请求处理器.

    Parameters
    ----------
    assets_dir : Path
        包含 ``v5/`` 的本地资源目录.

    Returns
    -------
    type[SimpleHTTPRequestHandler]
        已绑定目录的处理器类.

    Notes
    -----
    URL 是 ``/pyecharts_assets/v5/echarts.min.js``, 本地是 ``<assets_dir>/v5/echarts.min.js``,
    所以要先把 ``pyecharts_assets`` 这一段前缀摘掉.
    """
    # translate_path 只在前缀已经校验通过后才会被调用, 这里的哨兵值仅用于兜底
    prefix_only = "/__prefix_only__"

    class _AssetsHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, directory=str(assets_dir), **kwargs)  # type: ignore[arg-type]

        def _strip_prefix(self, path: str) -> str | None:
            """把 /pyecharts_assets/xxx 换成 /xxx, 不带前缀时返回 None."""
            prefix = f"/{config.ASSETS_URL_PREFIX}/"
            if path == f"/{config.ASSETS_URL_PREFIX}" or path.startswith(prefix):
                return f"/{path[len(prefix) :]}"
            return None

        def translate_path(self, path: str) -> str:
            stripped = self._strip_prefix(path)
            return super().translate_path(stripped if stripped is not None else prefix_only)

        def do_GET(self) -> None:
            if self._strip_prefix(self.path) is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            super().do_GET()

        def do_HEAD(self) -> None:
            if self._strip_prefix(self.path) is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            super().do_HEAD()

        def list_directory(self, path: StrPath) -> None:
            """不提供目录列表, 只按具体文件路径取资源."""
            self.send_error(HTTPStatus.NOT_FOUND)
            return None

        def log_message(self, format: str, *args: object) -> None:
            # 默认会往 stderr 打每一行请求日志, 这里降级成 debug 免得刷屏
            logger.debug(f"静态资源服务 {self.address_string()} {format % args}")

    return _AssetsHandler


def _resolve_timeout(timeout: float | httpx.Timeout | None) -> httpx.Timeout:
    """把超时参数统一成 httpx.Timeout.

    Parameters
    ----------
    timeout : float | httpx.Timeout | None
        传数字时四项都用这个值; 传 None 时用 PROBE_CLIENT_TIMEOUT.

    Returns
    -------
    httpx.Timeout
        可直接交给 httpx 的超时设置.
    """
    if timeout is None:
        return PROBE_CLIENT_TIMEOUT
    if isinstance(timeout, httpx.Timeout):
        return timeout
    return httpx.Timeout(timeout)


def check_assets_server(
    *,
    base_url: str | None = None,
    timeout: float | httpx.Timeout | None = None,
    client: httpx.Client | None = None,
) -> bool:
    """检查 pyecharts 静态资源服务是否可用.

    先探测 TCP 端口是否存活, 再用 HEAD 请求探针文件确认资源目录存在.
    两层都通过才返回 True, 避免服务没启动时继续渲染出空白图片.

    Parameters
    ----------
    base_url : str | None, default=None
        资源基础 URL, 为 None 时取配置里的. 自举服务落到随机端口时会传进来.
    timeout : float | httpx.Timeout | None, default=None
        超时设置, 为 None 时用连接 3 秒 / 读取 10 秒.
    client : httpx.Client | None, default=None
        复用外部客户端 (测试里注入 MockTransport); 为 None 时临时创建一个.

    Returns
    -------
    bool
        端口存活且资源文件可访问时返回 True, 否则返回 False.
    """
    target_base = config.assets_base_url() if base_url is None else base_url
    probe_url = f"{target_base}{config.ASSETS_PROBE_FILE}"
    request_timeout = _resolve_timeout(timeout)
    # 显式给了地址就按地址里的主机与端口探, 否则自举落到随机端口后会探错地方
    target = httpx.URL(target_base)
    host = config.assets_host() if base_url is None else target.host
    port = config.assets_port() if base_url is None else (target.port or 80)

    # 第一层: TCP 端口是否有人监听, 服务没启动时在这里就会失败
    try:
        with socket.create_connection((host, port), timeout=request_timeout.connect):
            pass
    except OSError as exc:
        logger.error(f"pyecharts 资源服务端口不可用: {host}:{port} ({exc})")
        return False

    # 第二层: 资源目录是否存在, 端口活着但目录缺失时状态码不是 200/206
    try:
        if client is not None:
            status_code = _probe_status(client, probe_url)
        else:
            with httpx.Client(timeout=request_timeout) as owned_client:
                status_code = _probe_status(owned_client, probe_url)
    except httpx.HTTPError as exc:
        logger.error(f"pyecharts 资源请求失败: {probe_url} ({exc})")
        return False

    if not _is_ok_status(status_code):
        logger.error(f"pyecharts 资源目录不可用, HTTP {status_code}: {probe_url}")
        return False

    logger.info(f"pyecharts 资源服务正常: {probe_url}")
    return True


def _is_ok_status(status_code: int) -> bool:
    """判断探活状态码是否算通过.

    Parameters
    ----------
    status_code : int
        HTTP 状态码.

    Returns
    -------
    bool
        200 (完整响应) 或 206 (Range 响应) 返回 True.
    """
    return status_code in {HTTPStatus.OK, HTTPStatus.PARTIAL_CONTENT}


def _probe_status(client: httpx.Client, probe_url: str) -> int:
    """用最省流量的方式问一下资源文件在不在.

    Parameters
    ----------
    client : httpx.Client
        已经建好的客户端.
    probe_url : str
        探针文件地址.

    Returns
    -------
    int
        HTTP 状态码.

    Notes
    -----
    先发 HEAD, 拿不到方法支持时再发带 Range 的 GET 兜底, 两种情况都不需要下载整个文件.
    """
    response = client.head(probe_url)
    if response.status_code in HEAD_UNSUPPORTED_STATUS:
        logger.debug(f"服务端不支持 HEAD, 改用 Range GET 探活: {probe_url}")
        response = client.get(probe_url, headers=RANGE_PROBE_HEADER)
    return response.status_code


@contextmanager
def temporary_assets_server(
    *,
    assets_dir: Path,
    timeout: float | httpx.Timeout | None = None,
) -> Iterator[AssetsService]:
    """临时起一个静态资源服务, 退出时关掉.

    Parameters
    ----------
    assets_dir : Path
        包含 ``v5/`` 的本地资源目录.
    timeout : float | httpx.Timeout | None, default=None
        启动后再次探活的超时设置.

    Yields
    ------
    AssetsService
        实际生效的服务信息, 端口固定取实际绑定的值 (可能与配置不同).

    Raises
    ------
    RuntimeError
        服务起来了但探活仍然失败时抛出.
    """
    host = config.assets_host()
    handler = _assets_request_handler(assets_dir)
    httpd = _bind_server(handler, host, config.assets_port())
    actual_port = int(httpd.server_address[1])
    actual_base_url = config.assets_base_url(actual_port)
    worker = threading.Thread(target=httpd.serve_forever, name="assets-server", daemon=True)
    worker.start()
    try:
        if not check_assets_server(base_url=actual_base_url, timeout=timeout):
            raise RuntimeError(f"临时静态资源服务启动后探活失败: {actual_base_url}")
        logger.info(f"已在 {host}:{actual_port} 临时启动静态资源服务, 资源目录 {assets_dir}")
        yield AssetsService(source=SOURCE_TEMPORARY, base_url=actual_base_url, port=actual_port)
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=SHUTDOWN_TIMEOUT)
        logger.info(f"已关闭临时静态资源服务: {host}:{actual_port}")


def _bind_server(
    handler: type[SimpleHTTPRequestHandler], host: str, port: int
) -> _ReusableHTTPServer:
    """在指定端口上建服务, 端口不可用时退到随机端口.

    Parameters
    ----------
    handler : type[SimpleHTTPRequestHandler]
        请求处理器类.
    host : str
        监听地址.
    port : int
        期望端口.

    Returns
    -------
    _ReusableHTTPServer
        已经绑定成功的服务实例.
    """
    try:
        return _ReusableHTTPServer((host, port), handler)
    except OSError as exc:
        # 端口被别人占着 (WinError 10048/10013) 也要出图, 换一个空闲端口就是了:
        # 自举的服务只服务于本机这次截图, 换端口不影响任何东西
        logger.warning(f"端口 {host}:{port} 不可用 ({exc}), 改用随机空闲端口")
        return _ReusableHTTPServer((host, 0), handler)


@contextmanager
def ensure_assets_server(
    *, timeout: float | httpx.Timeout | None = None
) -> Iterator[AssetsService | None]:
    """确保本次运行有可用的静态资源服务.

    先探活外部服务; 不通时按配置决定是否自举一个临时服务, 并在退出时关掉.

    Parameters
    ----------
    timeout : float | httpx.Timeout | None, default=None
        探测与启动后复探的超时设置, 为 None 时用默认值.

    Yields
    ------
    AssetsService | None
        可用时返回外部服务或临时服务的信息, 都不可用时返回 None.
    """
    if check_assets_server(timeout=timeout):
        yield AssetsService(
            source=SOURCE_EXTERNAL,
            base_url=config.assets_base_url(),
            port=config.assets_port(),
        )
        return

    if not config.allow_temporary_assets_server():
        logger.error(
            f"静态资源服务不可用, 且已通过 {config.ALLOW_TEMP_ASSETS_SERVER_ENV} 关闭了自举"
        )
        yield None
        return

    try:
        assets_dir = config.assets_dir()
    except ValueError as exc:
        logger.error(f"{exc}")
        yield None
        return

    if assets_dir is None:
        candidates = ", ".join(str(path) for path in config.asset_dir_candidates())
        logger.error(
            f"静态资源服务不可用, 且本地找不到资源目录 (可用 {config.ASSETS_DIR_ENV} 指定), "
            f"候选位置: {candidates}"
        )
        yield None
        return

    try:
        with temporary_assets_server(assets_dir=assets_dir, timeout=timeout) as service:
            logger.info(f"本次运行使用临时静态资源服务: {service.base_url}")
            yield service
    except OSError as exc:
        logger.error(f"无法自举静态资源服务: {exc}")
        yield None
