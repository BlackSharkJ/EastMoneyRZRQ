"""assets 模块单元测试: 静态资源目录解析, 探活, 临时自举服务的生命周期.

临时服务是真的绑定本地端口并收发 HTTP, 但只访问 127.0.0.1, 不碰外网.
"""

import socket
from pathlib import Path
from typing import Any

import httpx
import pytest

from eastmoneyrzrq import assets, config

FAKE_ASSETS_BYTES = b"// fake echarts bundle\n"


def _make_assets_dir(root: Path) -> Path:
    """造一个只含探针文件的假资源目录.

    Parameters
    ----------
    root : Path
        父目录.

    Returns
    -------
    Path
        含 ``v5/echarts.min.js`` 的目录.
    """
    target = root / config.ASSETS_DIR_NAME / config.ASSETS_DIR_SUBDIR
    version_dir = target / config.ASSETS_VERSION_DIR
    version_dir.mkdir(parents=True, exist_ok=True)
    (version_dir / config.ASSETS_PROBE_FILE).write_bytes(FAKE_ASSETS_BYTES)
    return target


@pytest.fixture
def fake_assets_dir(tmp_path: Path) -> Path:
    """含探针文件的假资源目录."""
    return _make_assets_dir(tmp_path)


def test_check_assets_server_port_closed(monkeypatch: pytest.MonkeyPatch, closed_port: int) -> None:
    monkeypatch.setenv(config.ASSETS_HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(closed_port))
    assert assets.check_assets_server(timeout=1.0) is False


def _recording_probe_handler(
    status: int = 200, *, head_status: int | None = None
) -> tuple[Any, list[httpx.Request]]:
    """造一个记录请求的 handler.

    Parameters
    ----------
    status : int, default=200
        非 HEAD 请求返回的状态码.
    head_status : int | None, default=None
        不为 None 时 HEAD 返回它, 用来模拟服务端不支持 HEAD.

    Returns
    -------
    tuple[Any, list[httpx.Request]]
        (handler, 收到的请求列表).
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "HEAD" and head_status is not None:
            return httpx.Response(head_status)
        return httpx.Response(status)

    return handler, seen


def test_resolve_timeout_default_and_float() -> None:
    """不传超时时用连接 3 秒 / 读取 10 秒, 传数字时四项统一."""
    default = assets._resolve_timeout(None)
    assert default is assets.PROBE_CLIENT_TIMEOUT
    assert default.connect == config.PROBE_CONNECT_TIMEOUT == 3.0
    assert default.read == config.PROBE_READ_TIMEOUT == 10.0

    uniform = assets._resolve_timeout(1.5)
    assert (uniform.connect, uniform.read, uniform.write, uniform.pool) == (1.5, 1.5, 1.5, 1.5)

    explicit = httpx.Timeout(2.0, read=9.0)
    assert assets._resolve_timeout(explicit) is explicit


@pytest.mark.parametrize(
    ("status", "expected"),
    [(200, True), (206, True), (301, False), (404, False), (500, False)],
)
def test_is_ok_status(status: int, expected: bool) -> None:
    """只有 200 与 206 算探活通过."""
    assert assets._is_ok_status(status) is expected


def test_check_assets_server_ok_uses_head_only(
    monkeypatch: pytest.MonkeyPatch,
    listening_port: int,
    make_client: Any,
) -> None:
    """探活只发 HEAD, 不下载 1MB 的 echarts.min.js."""
    monkeypatch.setenv(config.ASSETS_HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(listening_port))
    handler, seen = _recording_probe_handler()

    with make_client(handler) as client:
        assert assets.check_assets_server(timeout=1.0, client=client) is True

    assert [request.method for request in seen] == ["HEAD"]
    assert str(seen[0].url) == config.assets_probe_url()
    assert seen[0].headers.get("range") is None


@pytest.mark.parametrize("head_status", [405, 501])
def test_check_assets_server_falls_back_to_range_get(
    monkeypatch: pytest.MonkeyPatch,
    listening_port: int,
    make_client: Any,
    head_status: int,
) -> None:
    """服务端不支持 HEAD 时退化成带 Range 的 GET, 仍然只要一个字节."""
    monkeypatch.setenv(config.ASSETS_HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(listening_port))
    handler, seen = _recording_probe_handler(206, head_status=head_status)

    with make_client(handler) as client:
        assert assets.check_assets_server(timeout=1.0, client=client) is True

    assert [request.method for request in seen] == ["HEAD", "GET"]
    assert seen[1].headers["range"] == "bytes=0-0"


def test_check_assets_server_accepts_partial_content(
    monkeypatch: pytest.MonkeyPatch,
    listening_port: int,
    make_client: Any,
) -> None:
    """206 也算资源存在."""
    monkeypatch.setenv(config.ASSETS_HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(listening_port))
    handler, seen = _recording_probe_handler(206)

    with make_client(handler) as client:
        assert assets.check_assets_server(timeout=1.0, client=client) is True

    assert [request.method for request in seen] == ["HEAD"]


def test_check_assets_server_rejects_non_200(
    monkeypatch: pytest.MonkeyPatch,
    listening_port: int,
    make_client: Any,
) -> None:
    monkeypatch.setenv(config.ASSETS_HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(listening_port))
    handler, seen = _recording_probe_handler(404)

    with make_client(handler) as client:
        assert assets.check_assets_server(timeout=1.0, client=client) is False

    assert [request.method for request in seen] == ["HEAD"]
    assert str(seen[0].url) == config.assets_probe_url()


def test_check_assets_server_survives_connect_error(
    monkeypatch: pytest.MonkeyPatch,
    listening_port: int,
    make_client: Any,
) -> None:
    monkeypatch.setenv(config.ASSETS_HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(listening_port))

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("模拟连接失败", request=request)

    with make_client(handler) as client:
        assert assets.check_assets_server(timeout=1.0, client=client) is False


def test_check_assets_server_owns_client_when_not_injected(
    monkeypatch: pytest.MonkeyPatch, listening_port: int
) -> None:
    """不注入 client 时自己创建一个, 并且用解析好的超时设置."""
    monkeypatch.setenv(config.ASSETS_HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(listening_port))
    seen: list[tuple[str, httpx.Timeout]] = []

    class _FakeClient:
        def __init__(self, *, timeout: httpx.Timeout) -> None:
            self.timeout = timeout

        def __enter__(self) -> "_FakeClient":
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def head(self, url: str) -> httpx.Response:
            seen.append((url, self.timeout))
            return httpx.Response(200)

    monkeypatch.setattr(assets.httpx, "Client", _FakeClient)

    assert assets.check_assets_server(timeout=1.5) is True
    assert seen[0][0] == config.assets_probe_url()
    assert seen[0][1].connect == 1.5


def test_check_assets_server_prefers_port_from_base_url(
    monkeypatch: pytest.MonkeyPatch,
    listening_port: int,
    closed_port: int,
    make_client: Any,
) -> None:
    """base_url 里的端口优先于配置端口, 自举落到随机端口后就是这么探的."""
    monkeypatch.setenv(config.ASSETS_HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(closed_port))
    handler, seen = _recording_probe_handler()
    base_url = config.assets_base_url(listening_port)

    with make_client(handler) as client:
        assert assets.check_assets_server(base_url=base_url, timeout=1.0, client=client) is True

    assert str(seen[0].url) == f"{base_url}{config.ASSETS_PROBE_FILE}"


# --------------------------------------------------------------------------- #
# 资源目录解析与开关
# --------------------------------------------------------------------------- #


def test_is_assets_dir_requires_probe_file(fake_assets_dir: Path, tmp_path: Path) -> None:
    assert config.is_assets_dir(fake_assets_dir) is True
    assert config.is_assets_dir(tmp_path / "not-there") is False

    empty = tmp_path / "empty" / config.ASSETS_VERSION_DIR
    empty.mkdir(parents=True)
    assert config.is_assets_dir(tmp_path / "empty") is False


def test_asset_dir_candidates_cover_repo_and_sibling() -> None:
    candidates = config.asset_dir_candidates()
    assert len(candidates) == 2
    assert candidates[0] == config.PROJECT_ROOT / config.ASSETS_DIR_NAME / config.ASSETS_DIR_SUBDIR
    assert (
        candidates[1]
        == config.PROJECT_ROOT.parent / config.ASSETS_DIR_NAME / config.ASSETS_DIR_SUBDIR
    )


def test_assets_dir_prefers_env_override(
    monkeypatch: pytest.MonkeyPatch, fake_assets_dir: Path, tmp_path: Path
) -> None:
    other = _make_assets_dir(tmp_path / "other")
    monkeypatch.setenv(config.ASSETS_DIR_ENV, str(fake_assets_dir))
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path / "other")

    assert config.assets_dir() == fake_assets_dir
    assert config.assets_dir() != other


def test_assets_dir_raises_on_bad_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(config.ASSETS_DIR_ENV, str(tmp_path / "missing"))

    with pytest.raises(ValueError, match=config.ASSETS_DIR_ENV) as excinfo:
        config.assets_dir()
    assert str(tmp_path / "missing") in str(excinfo.value)


def test_assets_dir_falls_back_to_candidates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(config.ASSETS_DIR_ENV, raising=False)
    expected = _make_assets_dir(tmp_path)
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path / "child")

    assert config.assets_dir() == expected


def test_assets_dir_returns_none_when_nothing_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(config.ASSETS_DIR_ENV, raising=False)
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path / "no-such" / "child")

    assert config.assets_dir() is None


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " On "])
def test_allow_temporary_assets_server_accepts_truthy(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(config.ALLOW_TEMP_ASSETS_SERVER_ENV, raw)
    assert config.allow_temporary_assets_server() is True


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off", " Off "])
def test_allow_temporary_assets_server_accepts_falsy(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(config.ALLOW_TEMP_ASSETS_SERVER_ENV, raw)
    assert config.allow_temporary_assets_server() is False


def test_allow_temporary_assets_server_defaults_to_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(config.ALLOW_TEMP_ASSETS_SERVER_ENV, raising=False)
    assert config.allow_temporary_assets_server() is True
    assert config.DEFAULT_ALLOW_TEMP_ASSETS_SERVER is True


def test_allow_temporary_assets_server_rejects_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.ALLOW_TEMP_ASSETS_SERVER_ENV, "maybe")

    with pytest.raises(ValueError, match=config.ALLOW_TEMP_ASSETS_SERVER_ENV):
        config.allow_temporary_assets_server()


# --------------------------------------------------------------------------- #
# 临时服务
# --------------------------------------------------------------------------- #


def test_temporary_assets_server_serves_probe_file(
    monkeypatch: pytest.MonkeyPatch, fake_assets_dir: Path, closed_port: int
) -> None:
    monkeypatch.setenv(config.ASSETS_HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(closed_port))

    with (
        httpx.Client(timeout=3.0) as client,
        assets.temporary_assets_server(assets_dir=fake_assets_dir) as service,
    ):
        assert service.source == assets.SOURCE_TEMPORARY
        assert service.port == closed_port
        assert service.base_url == config.assets_base_url(closed_port)
        response = client.get(f"{service.base_url}{config.ASSETS_PROBE_FILE}")
        assert response.status_code == 200
        assert response.content == FAKE_ASSETS_BYTES
        assert assets.check_assets_server(timeout=1.0, client=client) is True


def test_temporary_assets_server_shuts_down_on_exit(
    monkeypatch: pytest.MonkeyPatch, fake_assets_dir: Path, closed_port: int
) -> None:
    monkeypatch.setenv(config.ASSETS_HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(closed_port))

    with (
        httpx.Client(timeout=3.0) as client,
        assets.temporary_assets_server(assets_dir=fake_assets_dir),
    ):
        assert client.get(config.assets_probe_url()).status_code == 200

    # 退出后端口必须已经释放, 否则会留下一个没人管的进程内服务
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", closed_port), timeout=0.5).close()


def test_temporary_assets_server_shuts_down_on_exception(
    monkeypatch: pytest.MonkeyPatch, fake_assets_dir: Path, closed_port: int
) -> None:
    monkeypatch.setenv(config.ASSETS_HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(closed_port))

    with (
        pytest.raises(RuntimeError, match="故意炸一下"),
        assets.temporary_assets_server(assets_dir=fake_assets_dir),
    ):
        raise RuntimeError("故意炸一下")

    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", closed_port), timeout=0.5).close()


def test_temporary_assets_server_404_outside_prefix(
    monkeypatch: pytest.MonkeyPatch, fake_assets_dir: Path, closed_port: int
) -> None:
    monkeypatch.setenv(config.ASSETS_HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(closed_port))
    base = f"http://127.0.0.1:{closed_port}"

    with (
        httpx.Client(timeout=3.0) as client,
        assets.temporary_assets_server(assets_dir=fake_assets_dir),
    ):
        # 不带 pyecharts_assets 前缀的路径一律 404, 与外部服务的路由保持一致
        assert client.get(f"{base}/v5/{config.ASSETS_PROBE_FILE}").status_code == 404
        assert client.get(f"{base}/").status_code == 404
        # 目录不列列表, 只按具体文件路径取
        assert client.get(f"{base}/{config.ASSETS_PATH}").status_code == 404
        assert client.get(f"{base}/{config.ASSETS_PATH}nope.js").status_code == 404
        # 有前缀的正常文件仍然可取
        assert client.get(config.assets_probe_url()).content == FAKE_ASSETS_BYTES


def test_temporary_assets_server_raises_when_probe_fails(
    monkeypatch: pytest.MonkeyPatch, fake_assets_dir: Path, closed_port: int
) -> None:
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(closed_port))
    monkeypatch.setattr(assets, "check_assets_server", lambda **kwargs: False)

    with (
        pytest.raises(RuntimeError, match="探活失败"),
        assets.temporary_assets_server(assets_dir=fake_assets_dir),
    ):
        pass


def test_temporary_assets_server_falls_back_to_free_port(
    monkeypatch: pytest.MonkeyPatch, fake_assets_dir: Path, listening_port: int
) -> None:
    """配置端口被占时换到随机空闲端口继续跑, 而不是直接把整次运行搞失败."""
    monkeypatch.setenv(config.ASSETS_HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(listening_port))

    with (
        httpx.Client(timeout=3.0) as client,
        assets.temporary_assets_server(assets_dir=fake_assets_dir) as service,
    ):
        assert service.source == assets.SOURCE_TEMPORARY
        assert service.port != listening_port
        assert service.port > 0
        assert service.base_url == config.assets_base_url(service.port)
        response = client.get(f"{service.base_url}{config.ASSETS_PROBE_FILE}")
        assert response.content == FAKE_ASSETS_BYTES

    # 换过去的随机端口在退出后也要释放
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", service.port), timeout=0.5).close()


def test_bind_server_uses_requested_port_when_free(closed_port: int) -> None:
    """端口空闲时就用配置的端口."""
    handler = assets._assets_request_handler(Path.cwd())
    server = assets._bind_server(handler, "127.0.0.1", closed_port)
    try:
        assert server.server_address[1] == closed_port
    finally:
        server.server_close()


def test_bind_server_falls_back_when_port_taken(listening_port: int) -> None:
    """端口被占时换到随机端口."""
    handler = assets._assets_request_handler(Path.cwd())
    server = assets._bind_server(handler, "127.0.0.1", listening_port)
    try:
        assert server.server_address[1] != listening_port
        assert server.server_address[1] > 0
    finally:
        server.server_close()


def test_reusable_http_server_does_not_set_so_reuseaddr() -> None:
    """Windows 上 SO_REUSEADDR 会让撞端口报 WSAEACCES 并抢流量, 必须关掉."""
    assert assets._ReusableHTTPServer.allow_reuse_address is False


# --------------------------------------------------------------------------- #
# ensure_assets_server
# --------------------------------------------------------------------------- #


def test_ensure_assets_server_uses_external(
    monkeypatch: pytest.MonkeyPatch, fake_assets_dir: Path, closed_port: int
) -> None:
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(closed_port))
    started: list[Path] = []
    monkeypatch.setattr(assets, "check_assets_server", lambda **kwargs: True)
    monkeypatch.setattr(
        assets, "temporary_assets_server", lambda **kwargs: started.append(fake_assets_dir)
    )

    with assets.ensure_assets_server() as service:
        assert service == assets.AssetsService(
            source=assets.SOURCE_EXTERNAL,
            base_url=config.assets_base_url(),
            port=config.assets_port(),
        )

    assert started == []


def test_ensure_assets_server_starts_temporary(
    monkeypatch: pytest.MonkeyPatch, fake_assets_dir: Path, closed_port: int
) -> None:
    monkeypatch.setenv(config.ASSETS_HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(closed_port))
    monkeypatch.setenv(config.ASSETS_DIR_ENV, str(fake_assets_dir))

    with httpx.Client(timeout=3.0) as client, assets.ensure_assets_server(timeout=1.0) as service:
        assert service is not None
        assert service.source == assets.SOURCE_TEMPORARY
        assert service.port == closed_port
        assert service.base_url == config.assets_base_url(closed_port)
        probe = f"{service.base_url}{config.ASSETS_PROBE_FILE}"
        assert client.get(probe).content == FAKE_ASSETS_BYTES

    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", closed_port), timeout=0.5).close()


def test_ensure_assets_server_returns_none_when_disallowed(
    monkeypatch: pytest.MonkeyPatch, fake_assets_dir: Path, closed_port: int
) -> None:
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(closed_port))
    monkeypatch.setenv(config.ASSETS_DIR_ENV, str(fake_assets_dir))
    monkeypatch.setenv(config.ALLOW_TEMP_ASSETS_SERVER_ENV, "0")

    with assets.ensure_assets_server(timeout=0.3) as source:
        assert source is None


def test_ensure_assets_server_returns_none_without_assets_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, closed_port: int
) -> None:
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(closed_port))
    monkeypatch.delenv(config.ASSETS_DIR_ENV, raising=False)
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path / "no-such" / "child")

    with assets.ensure_assets_server(timeout=0.3) as source:
        assert source is None


def test_ensure_assets_server_returns_none_on_bad_env_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, closed_port: int
) -> None:
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(closed_port))
    monkeypatch.setenv(config.ASSETS_DIR_ENV, str(tmp_path / "missing"))

    with assets.ensure_assets_server(timeout=0.3) as source:
        assert source is None


def test_ensure_assets_server_falls_back_when_port_taken(
    monkeypatch: pytest.MonkeyPatch, fake_assets_dir: Path, listening_port: int
) -> None:
    """外部探测失败且端口被别人占着时, 换端口自举仍然能出图."""
    monkeypatch.setenv(config.ASSETS_HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(listening_port))
    monkeypatch.setenv(config.ASSETS_DIR_ENV, str(fake_assets_dir))

    with (
        httpx.Client(timeout=3.0) as client,
        assets.ensure_assets_server(timeout=0.5) as service,
    ):
        assert service is not None
        assert service.source == assets.SOURCE_TEMPORARY
        assert service.port != listening_port
        probe = f"{service.base_url}{config.ASSETS_PROBE_FILE}"
        assert client.get(probe).content == FAKE_ASSETS_BYTES


def test_ensure_assets_server_returns_none_on_bind_error(
    monkeypatch: pytest.MonkeyPatch, fake_assets_dir: Path, closed_port: int
) -> None:
    """连随机端口都绑不上时 (极端情况) 才返回 None."""
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(closed_port))
    monkeypatch.setenv(config.ASSETS_DIR_ENV, str(fake_assets_dir))

    def boom(**kwargs: object) -> None:
        raise OSError("模拟无法绑定任何端口")

    monkeypatch.setattr(assets, "temporary_assets_server", boom)

    with assets.ensure_assets_server(timeout=0.3) as service:
        assert service is None


def test_temporary_assets_server_head_request_guards_prefix(
    monkeypatch: pytest.MonkeyPatch, fake_assets_dir: Path, closed_port: int
) -> None:
    """HEAD 请求也要守前缀规则: 带前缀的给 200, 不带的给 404."""
    monkeypatch.setenv(config.ASSETS_HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(closed_port))
    base = f"http://127.0.0.1:{closed_port}"

    with (
        httpx.Client(timeout=3.0) as client,
        assets.temporary_assets_server(assets_dir=fake_assets_dir),
    ):
        ok = client.head(config.assets_probe_url())
        assert ok.status_code == 200
        assert ok.headers["content-length"] == str(len(FAKE_ASSETS_BYTES))

        blocked = client.head(f"{base}/v5/{config.ASSETS_PROBE_FILE}")
        assert blocked.status_code == 404


def test_temporary_assets_server_logs_requests_at_debug(
    monkeypatch: pytest.MonkeyPatch, fake_assets_dir: Path, closed_port: int
) -> None:
    """请求日志走 logger.debug, 不直接写到 stderr."""
    monkeypatch.setenv(config.ASSETS_HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(closed_port))
    seen: list[str] = []

    class _Recorder:
        def debug(self, message: str) -> None:
            seen.append(message)

        def info(self, message: str) -> None:
            seen.append(message)

        def error(self, message: str) -> None:
            seen.append(message)

    monkeypatch.setattr(assets, "logger", _Recorder())

    with (
        httpx.Client(timeout=3.0) as client,
        assets.temporary_assets_server(assets_dir=fake_assets_dir),
    ):
        assert client.get(config.assets_probe_url()).status_code == 200

    assert any(config.ASSETS_PROBE_FILE in message for message in seen)
