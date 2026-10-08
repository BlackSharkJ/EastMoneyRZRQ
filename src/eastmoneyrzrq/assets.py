"""pyecharts 静态资源的探测与临时自举.

正常情况下资源服务由外部进程提供 (例如 QMTStrategy/xtquant_trader). 端口没起时,
如果配置允许且本地能找到 pyecharts-assets 目录, 就在同一个端口上临时起一个静态服务,
整个流程结束后自动关掉.

临时服务只读本地文件, 绑定在 127.0.0.1, 不对外暴露, 退出时由 ``ensure_assets_server``
的 finally 保证关闭.
"""

import logging
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
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

DEFAULT_PROBE_TIMEOUT: Final[float] = 3.0
SHUTDOWN_TIMEOUT: Final[float] = 5.0


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

        def list_directory(self, path: StrPath) -> None:
            """不提供目录列表, 只按具体文件路径取资源."""
            self.send_error(HTTPStatus.NOT_FOUND)
            return None

        def do_HEAD(self) -> None:
            if self._strip_prefix(self.path) is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            super().do_HEAD()

        def log_message(self, format: str, *args: object) -> None:
            # 默认会往 stderr 打每一行请求日志, 这里降级成 debug 免得刷屏
            logger.debug(f"静态资源服务 {self.address_string()} {format % args}")

    return _AssetsHandler


def check_assets_server(
    *, timeout: float = DEFAULT_PROBE_TIMEOUT, client: httpx.Client | None = None
) -> bool:
    """检查 pyecharts 静态资源服务是否可用.

    先探测 TCP 端口是否存活, 再请求一个真实存在的资源文件确认目录存在,
    两层都通过才返回 True, 避免服务没启动时继续渲染出空白图片.

    Parameters
    ----------
    timeout : float, default=DEFAULT_PROBE_TIMEOUT
        单次探测的超时时间, 单位秒.
    client : httpx.Client | None, default=None
        复用外部客户端 (测试里注入 MockTransport); 为 None 时用 httpx.get.

    Returns
    -------
    bool
        端口存活且资源文件可访问时返回 True, 否则返回 False.
    """
    host = config.assets_host()
    port = config.assets_port()
    probe_url = config.assets_probe_url()

    # 第一层: TCP 端口是否有人监听, 服务没启动时在这里就会失败
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError as exc:
        logger.error(f"pyecharts 资源服务端口不可用: {host}:{port} ({exc})")
        return False

    # 第二层: 资源目录是否存在, 端口活着但目录缺失时 HTTP 状态码不是 200
    try:
        if client is None:
            response = httpx.get(probe_url, timeout=timeout)
        else:
            response = client.get(probe_url)
    except httpx.HTTPError as exc:
        logger.error(f"pyecharts 资源请求失败: {probe_url} ({exc})")
        return False

    if response.status_code != 200:
        logger.error(f"pyecharts 资源目录不可用, HTTP {response.status_code}: {probe_url}")
        return False

    logger.info(f"pyecharts 资源服务正常: {probe_url}")
    return True


@contextmanager
def temporary_assets_server(
    *, assets_dir: Path, timeout: float = DEFAULT_PROBE_TIMEOUT
) -> Iterator[str]:
    """在配置的端口上临时起一个静态资源服务, 退出时关掉.

    Parameters
    ----------
    assets_dir : Path
        包含 ``v5/`` 的本地资源目录.
    timeout : float, default=DEFAULT_PROBE_TIMEOUT
        启动后再次探活的等待上限, 单位秒.

    Yields
    ------
    str
        可用的静态资源基础 URL.

    Raises
    ------
    OSError
        端口已被别的进程占用, 或没有权限绑定时抛出.
    RuntimeError
        服务起来了但探活仍然失败时抛出.
    """
    host = config.assets_host()
    port = config.assets_port()
    handler = _assets_request_handler(assets_dir)
    with ThreadingHTTPServer((host, port), handler) as httpd:
        worker = threading.Thread(target=httpd.serve_forever, name="assets-server", daemon=True)
        worker.start()
        try:
            if not check_assets_server(timeout=timeout):
                raise RuntimeError(f"临时静态资源服务启动后探活失败: {config.assets_probe_url()}")
            logger.info(f"已在 {host}:{port} 临时启动静态资源服务, 资源目录 {assets_dir}")
            yield config.assets_base_url()
        finally:
            httpd.shutdown()
            httpd.server_close()
            worker.join(timeout=SHUTDOWN_TIMEOUT)
            logger.info(f"已关闭临时静态资源服务: {host}:{port}")


@contextmanager
def ensure_assets_server(*, timeout: float = DEFAULT_PROBE_TIMEOUT) -> Iterator[str | None]:
    """确保本次运行有可用的静态资源服务.

    先探活外部服务; 不通时按配置决定是否自举一个临时服务, 并在退出时关掉.

    Parameters
    ----------
    timeout : float, default=DEFAULT_PROBE_TIMEOUT
        单次探测的超时时间, 单位秒.

    Yields
    ------
    str | None
        可用时返回 SOURCE_EXTERNAL 或 SOURCE_TEMPORARY, 都不可用时返回 None.
    """
    if check_assets_server(timeout=timeout):
        yield SOURCE_EXTERNAL
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
        with temporary_assets_server(assets_dir=assets_dir, timeout=timeout) as base_url:
            logger.info(f"本次运行使用临时静态资源服务: {base_url}")
            yield SOURCE_TEMPORARY
    except OSError as exc:
        logger.error(f"端口被占用, 无法自举静态资源服务: {exc}")
        yield None
