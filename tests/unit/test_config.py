"""config 模块单元测试: 静态资源地址与环境变量, webhook key, .env 加载."""

import logging
import os
from pathlib import Path

import pytest

from eastmoneyrzrq import config


def test_assets_host_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(config.ASSETS_HOST_ENV, raising=False)
    assert config.assets_host() == "127.0.0.1"
    assert config.assets_host() == config.DEFAULT_ASSETS_HOST


def test_assets_host_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.ASSETS_HOST_ENV, "192.168.1.10")
    assert config.assets_host() == "192.168.1.10"


def test_assets_port_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(config.ASSETS_PORT_ENV, raising=False)
    assert config.assets_port() == 8888
    assert config.assets_port() == config.DEFAULT_ASSETS_PORT


def test_assets_port_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.ASSETS_PORT_ENV, "7001")
    assert config.assets_port() == 7001


@pytest.mark.parametrize("raw", ["abc", "8888.0", "", "80a", "八八八"])
def test_assets_port_rejects_non_integer(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(config.ASSETS_PORT_ENV, raw)
    with pytest.raises(ValueError, match=f"{config.ASSETS_PORT_ENV} 必须是整数") as excinfo:
        config.assets_port()
    assert repr(raw) in str(excinfo.value)


@pytest.mark.parametrize("raw", ["0", "-1", "65536", "70000", "100000"])
def test_assets_port_rejects_out_of_range(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(config.ASSETS_PORT_ENV, raw)
    with pytest.raises(ValueError, match="必须在 1~65535 之间") as excinfo:
        config.assets_port()
    assert f"当前为 {int(raw)}" in str(excinfo.value)


@pytest.mark.parametrize("raw, expected", [("1", 1), ("65535", 65535)])
def test_assets_port_accepts_boundaries(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: int
) -> None:
    monkeypatch.setenv(config.ASSETS_PORT_ENV, raw)
    assert config.assets_port() == expected


def test_assets_base_url_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(config.ASSETS_HOST_ENV, raising=False)
    monkeypatch.delenv(config.ASSETS_PORT_ENV, raising=False)
    assert config.assets_base_url() == "http://127.0.0.1:8888/pyecharts_assets/v5/"
    assert config.assets_base_url().endswith("/")


def test_assets_base_url_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.ASSETS_HOST_ENV, "10.0.0.5")
    monkeypatch.setenv(config.ASSETS_PORT_ENV, "9000")
    assert config.assets_base_url() == "http://10.0.0.5:9000/pyecharts_assets/v5/"


def test_assets_probe_url_uses_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(config.ASSETS_HOST_ENV, raising=False)
    monkeypatch.delenv(config.ASSETS_PORT_ENV, raising=False)
    probe_url = config.assets_probe_url()
    assert probe_url == "http://127.0.0.1:8888/pyecharts_assets/v5/echarts.min.js"
    assert probe_url.endswith("echarts.min.js")
    assert probe_url == f"{config.assets_base_url()}{config.ASSETS_PROBE_FILE}"


def test_assets_probe_url_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.ASSETS_HOST_ENV, "example.local")
    monkeypatch.setenv(config.ASSETS_PORT_ENV, "1234")
    assert (
        config.assets_probe_url() == "http://example.local:1234/pyecharts_assets/v5/echarts.min.js"
    )


def test_assets_port_error_is_chained(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.ASSETS_PORT_ENV, "not-a-number")
    with pytest.raises(ValueError) as excinfo:
        config.assets_port()
    assert isinstance(excinfo.value.__cause__, ValueError)


def test_get_wechat_webhook_key_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.WECHAT_WEBHOOK_KEY_ENV, "  abc-123-key \n")
    assert config.get_wechat_webhook_key() == "abc-123-key"


def test_get_wechat_webhook_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(config.WECHAT_WEBHOOK_KEY_ENV, raising=False)
    with pytest.raises(RuntimeError, match="WECHAT_WEBHOOK_KEY") as excinfo:
        config.get_wechat_webhook_key()
    message = str(excinfo.value)
    assert config.WECHAT_WEBHOOK_KEY_ENV in message
    assert "未配置" in message


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_get_wechat_webhook_key_blank(monkeypatch: pytest.MonkeyPatch, blank: str) -> None:
    monkeypatch.setenv(config.WECHAT_WEBHOOK_KEY_ENV, blank)
    with pytest.raises(RuntimeError, match=config.WECHAT_WEBHOOK_KEY_ENV):
        config.get_wechat_webhook_key()


def test_load_env_file_returns_false_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "not-there.env"
    monkeypatch.setattr(config, "ENV_FILE_PATH", missing)
    assert missing.is_file() is False
    assert config.load_env_file() is False


def test_load_env_file_returns_false_for_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "ENV_FILE_PATH", tmp_path)
    assert config.load_env_file() is False


def test_load_env_file_injects_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        f"{config.WECHAT_WEBHOOK_KEY_ENV}=from-dotenv\n{config.ASSETS_PORT_ENV}=7002\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "ENV_FILE_PATH", env_path)
    monkeypatch.delenv(config.WECHAT_WEBHOOK_KEY_ENV, raising=False)
    monkeypatch.delenv(config.ASSETS_PORT_ENV, raising=False)

    assert config.load_env_file() is True
    assert os.environ[config.WECHAT_WEBHOOK_KEY_ENV] == "from-dotenv"
    assert config.get_wechat_webhook_key() == "from-dotenv"
    assert config.assets_port() == 7002


def test_load_env_file_does_not_override_existing_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(f"{config.WECHAT_WEBHOOK_KEY_ENV}=from-dotenv\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE_PATH", env_path)
    monkeypatch.setenv(config.WECHAT_WEBHOOK_KEY_ENV, "from-env")

    assert config.load_env_file() is True
    assert os.environ[config.WECHAT_WEBHOOK_KEY_ENV] == "from-env"


def test_setup_logging_passes_level_format_and_datefmt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_basic_config(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(config.logging, "basicConfig", fake_basic_config)

    config.setup_logging(logging.DEBUG)
    assert captured == {
        "level": logging.DEBUG,
        "format": config.LOG_FORMAT,
        "datefmt": config.LOG_DATE_FORMAT,
    }

    config.setup_logging()
    assert captured["level"] == logging.INFO
    assert captured["format"] == "%(asctime)s | %(levelname)s | %(funcName)s | %(message)s"
    assert captured["datefmt"] == "%Y-%m-%d %H:%M:%S"
