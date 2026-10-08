"""测试公共 fixture: 假数据, MockTransport 客户端, 本地 TCP 监听口."""

import json
import socket
import threading
from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import pytest

from eastmoneyrzrq import config, data

TEST_WEBHOOK_KEY = "test-webhook-key"


def make_raw_record(day: str, **overrides: Any) -> dict[str, Any]:
    """造一条东财风格的原始记录.

    Parameters
    ----------
    day : str
        日期字符串, 形如 2026-09-30 00:00:00.
    **overrides : Any
        需要覆盖的字段, key 用中文列名, 值直接用最终数值.

    Returns
    -------
    dict[str, Any]
        字段名是英文大写名的原始记录.
    """
    reverse_mapping = {chinese: english for english, chinese in data.COLUMN_MAPPING.items()}
    record: dict[str, Any] = {
        "DIM_DATE": day,
        "NEW": 4000.0,
        "RZYE": 2500000000000.0,
        "RZYEZB": 2.5,
        "RZMRE": 200000000000.0,
        "RZCHE": 195000000000.0,
        "RZJME": 5000000000.0,
        "RQYE": 20000000000.0,
        "RQYL": 3000000000.0,
        "RQCHL": 80000000.0,
        "RQMCL": 90000000.0,
        "RQJMG": 10000000.0,
        "RZRQYE": 2520000000000.0,
    }
    for chinese_name, value in overrides.items():
        record[reverse_mapping[chinese_name]] = value
    return record


@pytest.fixture
def raw_records() -> list[dict[str, Any]]:
    """三条原始记录, 日期故意乱序, 用来验证排序."""
    return [
        make_raw_record("2026-09-30 00:00:00"),
        make_raw_record(
            "2026-09-28 00:00:00",
            融资净买额=-30000000000.0,
            融券净卖额=-20000000.0,
        ),
        make_raw_record(
            "2026-09-29 00:00:00",
            融资净买额=0.0,
            融券净卖额=0.0,
        ),
    ]


@pytest.fixture
def sample_df(raw_records: list[dict[str, Any]]) -> pl.DataFrame:
    """整理好的 DataFrame, 已按日期升序."""
    return data.to_dataframe(raw_records)


@pytest.fixture
def jsonp_payload(raw_records: list[dict[str, Any]]) -> str:
    """带 JSONP 外壳的接口响应文本."""
    body = json.dumps({"result": {"data": raw_records}}, ensure_ascii=False)
    return f"{config.EASTMONEY_JSONP_CALLBACK}({body})"


@pytest.fixture
def webhook_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """注入一个测试用 webhook key."""
    monkeypatch.setenv(config.WECHAT_WEBHOOK_KEY_ENV, TEST_WEBHOOK_KEY)
    return TEST_WEBHOOK_KEY


@pytest.fixture
def make_client() -> Callable[[Callable[[httpx.Request], httpx.Response]], httpx.Client]:
    """返回一个工厂: 传入 handler 就能拿到拦住真实请求的 client."""

    def factory(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler))

    return factory


@pytest.fixture
def listening_port() -> Iterator[int]:
    """起一个真实的本地 TCP 监听口, 用于验证端口探测的成功分支.

    Yields
    ------
    int
        已监听但不会收发数据的端口号, 用完自动关闭.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    port = int(server.getsockname()[1])

    def serve() -> None:
        server.settimeout(1.0)
        try:
            while True:
                connection, _ = server.accept()
                connection.close()
        except OSError:
            return

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    try:
        yield port
    finally:
        server.close()
        worker.join(timeout=2)


@pytest.fixture
def closed_port() -> int:
    """一个确定没人监听的端口: 先占住再释放."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    return port


@pytest.fixture
def report_date() -> date:
    """用于断言文本消息的日期."""
    return date(2026, 9, 30)


@pytest.fixture
def png_file(tmp_path: Path) -> Path:
    """一个内容固定的假图片文件."""
    path = tmp_path / "fake.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n fake image bytes")
    return path
