"""notify 模块单元测试: 消息体拼装, 发送, 素材上传与一条龙推送.

所有 HTTP 请求都走 MockTransport, 不访问真实网络.
"""

import base64
import hashlib
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from eastmoneyrzrq import config, notify

# png_file fixture 写入的固定字节, 期望值在这里独立手算, 不复用被测实现
FAKE_PNG_BYTES: bytes = b"\x89PNG\r\n\x1a\n fake image bytes"
EXPECTED_BASE64: str = "iVBORw0KGgogZmFrZSBpbWFnZSBieXRlcw=="
EXPECTED_MD5: str = "8d307f22eca2f74216e99ad4f62bb7b1"

Handler = Callable[[httpx.Request], httpx.Response]
MakeClient = Callable[[Handler], httpx.Client]


def _text_payload(content: str) -> dict[str, object]:
    """造一个不含 @ 名单的文本消息体, 用于发送测试."""
    return {
        "msgtype": "text",
        "text": {"content": content, "mentioned_list": []},
    }


def test_hand_computed_fixture_bytes_match_png_file(png_file: Path) -> None:
    assert png_file.read_bytes() == FAKE_PNG_BYTES
    assert base64.b64encode(FAKE_PNG_BYTES).decode("ascii") == EXPECTED_BASE64
    assert hashlib.md5(FAKE_PNG_BYTES).hexdigest() == EXPECTED_MD5


def test_resolve_webhook_key_reads_environment(webhook_key: str) -> None:
    assert notify.resolve_webhook_key() == webhook_key == "test-webhook-key"


def test_resolve_webhook_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(config.WECHAT_WEBHOOK_KEY_ENV, raising=False)
    with pytest.raises(RuntimeError, match="WECHAT_WEBHOOK_KEY") as excinfo:
        notify.resolve_webhook_key()
    assert config.WECHAT_WEBHOOK_KEY_ENV in str(excinfo.value)


def test_path_to_base64_matches_expected(png_file: Path) -> None:
    assert notify.path_to_base64(png_file) == EXPECTED_BASE64
    assert notify.path_to_base64(str(png_file)) == EXPECTED_BASE64
    assert base64.b64decode(notify.path_to_base64(png_file)) == FAKE_PNG_BYTES


def test_file_md5_matches_expected(png_file: Path) -> None:
    assert notify.file_md5(png_file) == EXPECTED_MD5
    assert notify.file_md5(str(png_file)) == EXPECTED_MD5
    assert len(notify.file_md5(png_file)) == 32


def test_path_to_base64_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        notify.path_to_base64(tmp_path / "nope.png")


def test_build_text_payload_uses_default_mentions() -> None:
    payload = notify.build_text_payload("今日两融")
    assert payload == {
        "msgtype": "text",
        "text": {"content": "今日两融", "mentioned_list": ["longjunfan", "lijing"]},
    }
    assert payload["text"]["mentioned_list"] == list(config.WECHAT_MENTIONED_LIST)


def test_build_text_payload_default_mentions_are_a_copy() -> None:
    payload = notify.build_text_payload("hi")
    payload["text"]["mentioned_list"].append("intruder")
    assert notify.build_text_payload("hi")["text"]["mentioned_list"] == [
        "longjunfan",
        "lijing",
    ]
    assert isinstance(config.WECHAT_MENTIONED_LIST, tuple)


def test_build_text_payload_custom_mentions() -> None:
    payload = notify.build_text_payload("hi", ["alice", "bob"])
    assert payload["text"]["mentioned_list"] == ["alice", "bob"]
    assert payload["msgtype"] == "text"
    assert payload["text"]["content"] == "hi"


def test_build_text_payload_empty_mentions() -> None:
    payload = notify.build_text_payload("hi", [])
    assert payload["text"]["mentioned_list"] == []


def test_build_image_payload_matches_expected(png_file: Path) -> None:
    payload = notify.build_image_payload(png_file)
    assert payload == {
        "msgtype": "image",
        "image": {"base64": EXPECTED_BASE64, "md5": EXPECTED_MD5},
    }
    assert base64.b64decode(payload["image"]["base64"]) == FAKE_PNG_BYTES
    assert payload["image"]["md5"] == hashlib.md5(FAKE_PNG_BYTES).hexdigest()


def test_build_file_payload() -> None:
    assert notify.build_file_payload("MEDIA_ID_1") == {
        "msgtype": "file",
        "file": {"media_id": "MEDIA_ID_1"},
    }


def test_build_report_text(report_date: date) -> None:
    assert notify.build_report_text(report_date) == "2026-09-30 两融信息"
    assert notify.build_report_text(date(2025, 1, 2)) == "2025-01-02 两融信息"


def test_send_message_posts_payload_with_key(make_client: MakeClient, webhook_key: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    payload = _text_payload("hello")
    with make_client(handler) as client:
        result = notify.send_message(payload, client=client)

    assert result == {"errcode": 0, "errmsg": "ok"}
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert request.url.scheme == "https"
    assert request.url.host == "qyapi.weixin.qq.com"
    assert request.url.path == "/cgi-bin/webhook/send"
    assert str(request.url).startswith(config.WECHAT_SEND_URL)
    assert request.url.params["key"] == webhook_key
    assert request.headers["content-type"] == "application/json"
    assert request.headers["charset"] == "utf-8"
    assert json.loads(request.content) == payload


def test_send_message_explicit_key_wins(make_client: MakeClient, webhook_key: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"errcode": 0})

    with make_client(handler) as client:
        notify.send_message(_text_payload("hi"), key="explicit-key", client=client)

    assert seen[0].url.params["key"] == "explicit-key"
    assert seen[0].url.params["key"] != webhook_key


def test_send_message_returns_raw_result_on_errcode(make_client: MakeClient) -> None:
    failed = {"errcode": 93000, "errmsg": "invalid webhook url, hint: [abc]"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=failed)

    with make_client(handler) as client:
        result = notify.send_message(_text_payload("hi"), key="k", client=client)

    assert result == failed
    assert result["errcode"] == 93000
    assert "invalid webhook url" in result["errmsg"]


def test_send_message_raises_on_http_error(make_client: MakeClient) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server exploded")

    with make_client(handler) as client, pytest.raises(httpx.HTTPStatusError) as excinfo:
        notify.send_message(_text_payload("hi"), key="k", client=client)

    assert excinfo.value.response.status_code == 500
    assert excinfo.value.response.text == "server exploded"


def test_send_message_missing_key_never_posts(
    make_client: MakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(config.WECHAT_WEBHOOK_KEY_ENV, raising=False)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"errcode": 0})

    with make_client(handler) as client, pytest.raises(RuntimeError, match="WECHAT_WEBHOOK_KEY"):
        notify.send_message(_text_payload("hi"), client=client)

    assert seen == []


def test_upload_media_returns_media_id(make_client: MakeClient, png_file: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok", "media_id": "MEDIA_ID_1"})

    with make_client(handler) as client:
        media_id = notify.upload_media(png_file, key="upload-key", client=client)

    assert media_id == "MEDIA_ID_1"
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/cgi-bin/webhook/upload_media"
    assert str(request.url).startswith(config.WECHAT_UPLOAD_URL)
    assert request.url.params["key"] == "upload-key"
    assert request.url.params["type"] == "file"
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    body = request.content
    assert b'name="file"' in body
    assert b'filename="fake.png"' in body
    assert FAKE_PNG_BYTES in body


def test_upload_media_uses_environment_key(
    make_client: MakeClient, png_file: Path, webhook_key: str
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"errcode": 0, "media_id": "MEDIA_ID_2"})

    with make_client(handler) as client:
        assert notify.upload_media(png_file, client=client) == "MEDIA_ID_2"

    assert seen[0].url.params["key"] == webhook_key


def test_upload_media_raises_on_errcode(make_client: MakeClient, png_file: Path) -> None:
    failed = {"errcode": 45009, "errmsg": "reach max api daily quota limit"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=failed)

    with make_client(handler) as client, pytest.raises(RuntimeError) as excinfo:
        notify.upload_media(png_file, key="k", client=client)

    assert "企业微信素材上传失败" in str(excinfo.value)
    assert "45009" in str(excinfo.value)


def test_upload_media_raises_on_http_error(make_client: MakeClient, png_file: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    with make_client(handler) as client, pytest.raises(httpx.HTTPStatusError) as excinfo:
        notify.upload_media(png_file, key="k", client=client)

    assert excinfo.value.response.status_code == 403


def test_upload_media_missing_file(tmp_path: Path, make_client: MakeClient) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("不应该发出请求")

    with make_client(handler) as client, pytest.raises(FileNotFoundError):
        notify.upload_media(tmp_path / "missing.bin", key="k", client=client)


def test_upload_media_without_media_id_raises_runtime_error(
    make_client: MakeClient, png_file: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    with make_client(handler) as client, pytest.raises(RuntimeError, match="media_id"):
        notify.upload_media(png_file, key="k", client=client)


def test_send_file_uploads_then_sends(make_client: MakeClient, png_file: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/upload_media"):
            return httpx.Response(200, json={"errcode": 0, "media_id": "MEDIA_ID_9"})
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    with make_client(handler) as client:
        result = notify.send_file(png_file, key="k", client=client)

    assert [request.url.path for request in seen] == [
        "/cgi-bin/webhook/upload_media",
        "/cgi-bin/webhook/send",
    ]
    assert result == {"errcode": 0, "errmsg": "ok"}
    assert seen[0].url.params["type"] == "file"
    assert json.loads(seen[1].content) == {
        "msgtype": "file",
        "file": {"media_id": "MEDIA_ID_9"},
    }


def test_send_report_text_then_image(
    make_client: MakeClient, png_file: Path, report_date: date, webhook_key: str
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    with make_client(handler) as client:
        assert notify.send_report(report_date, png_file, client=client) is None

    assert len(seen) == 2
    assert [json.loads(request.content)["msgtype"] for request in seen] == ["text", "image"]
    assert [request.url.path for request in seen] == [
        "/cgi-bin/webhook/send",
        "/cgi-bin/webhook/send",
    ]
    assert [request.url.params["key"] for request in seen] == [webhook_key, webhook_key]

    text_body = json.loads(seen[0].content)
    assert text_body == {
        "msgtype": "text",
        "text": {
            "content": "2026-09-30 两融信息",
            "mentioned_list": ["longjunfan", "lijing"],
        },
    }
    image_body = json.loads(seen[1].content)
    assert image_body == {
        "msgtype": "image",
        "image": {"base64": EXPECTED_BASE64, "md5": EXPECTED_MD5},
    }


def test_send_report_missing_key_raises(
    make_client: MakeClient, png_file: Path, report_date: date, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(config.WECHAT_WEBHOOK_KEY_ENV, raising=False)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"errcode": 0})

    with make_client(handler) as client, pytest.raises(RuntimeError, match="WECHAT_WEBHOOK_KEY"):
        notify.send_report(report_date, png_file, client=client)

    assert seen == []


def test_send_message_creates_owned_client(
    monkeypatch: pytest.MonkeyPatch, make_client: MakeClient, webhook_key: str
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    injected = make_client(handler)

    def fake_client(**kwargs: object) -> httpx.Client:
        assert kwargs == {"timeout": config.WECHAT_TIMEOUT}
        return injected

    monkeypatch.setattr(notify, "httpx", SimpleNamespace(Client=fake_client))

    result = notify.send_message(_text_payload("hi"))

    assert result == {"errcode": 0, "errmsg": "ok"}
    assert len(seen) == 1
    assert seen[0].url.params["key"] == webhook_key
    assert json.loads(seen[0].content) == _text_payload("hi")


def test_upload_media_creates_owned_client(
    monkeypatch: pytest.MonkeyPatch, make_client: MakeClient, webhook_key: str, png_file: Path
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"errcode": 0, "media_id": "MEDIA_ID_3"})

    injected = make_client(handler)

    def fake_client(**kwargs: object) -> httpx.Client:
        assert kwargs == {"timeout": config.WECHAT_TIMEOUT}
        return injected

    monkeypatch.setattr(notify, "httpx", SimpleNamespace(Client=fake_client))

    assert notify.upload_media(png_file) == "MEDIA_ID_3"
    assert seen[0].url.params["key"] == webhook_key
    assert seen[0].url.params["type"] == "file"
    assert FAKE_PNG_BYTES in seen[0].content
