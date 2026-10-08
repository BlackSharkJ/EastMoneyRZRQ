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


def test_check_assets_server_ok(
    monkeypatch: pytest.MonkeyPatch,
    listening_port: int,
    make_client: Any,
) -> None:
    monkeypatch.setenv(config.ASSETS_HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(listening_port))
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    client = make_client(handler)
    try:
        assert assets.check_assets_server(timeout=1.0, client=client) is True
    finally:
        client.close()
    assert seen == [config.assets_probe_url()]


def test_check_assets_server_rejects_non_200(
    monkeypatch: pytest.MonkeyPatch,
    listening_port: int,
    make_client: Any,
) -> None:
    monkeypatch.setenv(config.ASSETS_HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(listening_port))
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(404)

    client = make_client(handler)
    try:
        assert assets.check_assets_server(timeout=1.0, client=client) is False
    finally:
        client.close()
    assert seen == [config.assets_probe_url()]


def test_check_assets_server_survives_connect_error(
    monkeypatch: pytest.MonkeyPatch,
    listening_port: int,
    make_client: Any,
) -> None:
    monkeypatch.setenv(config.ASSETS_HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(listening_port))

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("模拟连接失败", request=request)

    client = make_client(handler)
    try:
        assert assets.check_assets_server(timeout=1.0, client=client) is False
    finally:
        client.close()


def test_check_assets_server_uses_httpx_get_without_client(
    monkeypatch: pytest.MonkeyPatch,
    listening_port: int,
) -> None:
    """不注入 client 时走 httpx.get 分支, 子进程/cli 场景就是这个路径."""
    monkeypatch.setenv(config.ASSETS_HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(listening_port))
    seen: list[tuple[str, float]] = []

    def fake_get(url: str, timeout: float) -> httpx.Response:
        seen.append((url, timeout))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(assets.httpx, "get", fake_get)

    assert assets.check_assets_server(timeout=1.5) is True
    assert seen == [(config.assets_probe_url(), 1.5)]


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
        assets.temporary_assets_server(assets_dir=fake_assets_dir) as base_url,
    ):
        assert base_url == f"http://127.0.0.1:{closed_port}/{config.ASSETS_PATH}"
        response = client.get(config.assets_probe_url())
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


def test_temporary_assets_server_raises_when_port_taken(
    monkeypatch: pytest.MonkeyPatch, fake_assets_dir: Path, listening_port: int
) -> None:
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(listening_port))

    with pytest.raises(OSError), assets.temporary_assets_server(assets_dir=fake_assets_dir):
        pass


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

    with assets.ensure_assets_server() as source:
        assert source == assets.SOURCE_EXTERNAL

    assert started == []


def test_ensure_assets_server_starts_temporary(
    monkeypatch: pytest.MonkeyPatch, fake_assets_dir: Path, closed_port: int
) -> None:
    monkeypatch.setenv(config.ASSETS_HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(closed_port))
    monkeypatch.setenv(config.ASSETS_DIR_ENV, str(fake_assets_dir))

    with httpx.Client(timeout=3.0) as client, assets.ensure_assets_server(timeout=1.0) as source:
        assert source == assets.SOURCE_TEMPORARY
        assert client.get(config.assets_probe_url()).content == FAKE_ASSETS_BYTES

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


def test_ensure_assets_server_returns_none_when_port_taken_by_other_server(
    monkeypatch: pytest.MonkeyPatch, fake_assets_dir: Path, listening_port: int
) -> None:
    monkeypatch.setenv(config.ASSETS_PORT_ENV, str(listening_port))
    monkeypatch.setenv(config.ASSETS_DIR_ENV, str(fake_assets_dir))

    with assets.ensure_assets_server(timeout=0.5) as source:
        assert source is None


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
