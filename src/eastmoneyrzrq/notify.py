"""企业微信机器人推送.

webhook key 必须从环境变量或 .env 读取, 不允许写进代码.
所有对外函数都允许注入 httpx.Client, 单测里用 MockTransport 拦住真实请求.
"""

import base64
import hashlib
import logging
from datetime import date
from pathlib import Path
from typing import Any, Final

import httpx

from . import config

logger = logging.getLogger(__name__)

MESSAGE_TYPE_TEXT: Final[str] = "text"
MESSAGE_TYPE_IMAGE: Final[str] = "image"
MESSAGE_TYPE_FILE: Final[str] = "file"


def resolve_webhook_key() -> str:
    """读取企业微信机器人 key.

    Returns
    -------
    str
        webhook key.

    Raises
    ------
    RuntimeError
        没有配置 WECHAT_WEBHOOK_KEY 时抛出.
    """
    return config.get_wechat_webhook_key()


def path_to_base64(path: str | Path) -> str:
    """把文件读成 base64 字符串.

    Parameters
    ----------
    path : str | Path
        文件路径.

    Returns
    -------
    str
        base64 编码结果, 用于企业微信图片消息.
    """
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


def file_md5(path: str | Path) -> str:
    """算文件的 MD5.

    Parameters
    ----------
    path : str | Path
        文件路径.

    Returns
    -------
    str
        32 位十六进制 MD5, 用于企业微信图片消息.
    """
    return hashlib.md5(Path(path).read_bytes()).hexdigest()


def build_text_payload(content: str, mentioned_list: list[str] | None = None) -> dict[str, Any]:
    """拼出文本消息体.

    Parameters
    ----------
    content : str
        文本内容.
    mentioned_list : list[str] | None, default=None
        需要 @ 的企业微信账号, 为 None 时用 config.WECHAT_MENTIONED_LIST.

    Returns
    -------
    dict[str, Any]
        可直接作为 json 提交的消息体.
    """
    mentioned = list(config.WECHAT_MENTIONED_LIST if mentioned_list is None else mentioned_list)
    return {
        "msgtype": MESSAGE_TYPE_TEXT,
        "text": {"content": content, "mentioned_list": mentioned},
    }


def build_image_payload(image_path: str | Path) -> dict[str, Any]:
    """拼出图片消息体.

    Parameters
    ----------
    image_path : str | Path
        本地图片路径.

    Returns
    -------
    dict[str, Any]
        base64 与 md5 都预先算好的消息体.
    """
    return {
        "msgtype": MESSAGE_TYPE_IMAGE,
        "image": {
            "base64": path_to_base64(image_path),
            "md5": file_md5(image_path),
        },
    }


def build_file_payload(media_id: str) -> dict[str, Any]:
    """拼出文件消息体.

    Parameters
    ----------
    media_id : str
        上传素材后拿到的 media_id.

    Returns
    -------
    dict[str, Any]
        文件消息体.
    """
    return {"msgtype": MESSAGE_TYPE_FILE, "file": {"media_id": media_id}}


def build_report_text(report_date: date) -> str:
    """拼出报告文本.

    Parameters
    ----------
    report_date : date
        数据日期.

    Returns
    -------
    str
        形如 ``2026-09-30 两融信息`` 的文本.
    """
    return f"{report_date} 两融信息"


def send_message(
    payload: dict[str, Any],
    *,
    key: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """发送一条企业微信消息.

    Parameters
    ----------
    payload : dict[str, Any]
        消息体.
    key : str | None, default=None
        webhook key, 为 None 时从配置读取.
    client : httpx.Client | None, default=None
        复用外部客户端; 为 None 时临时创建一个.

    Returns
    -------
    dict[str, Any]
        企业微信返回的 JSON, errcode 为 0 表示成功.

    Raises
    ------
    httpx.HTTPStatusError
        HTTP 状态码非 2xx 时由 raise_for_status 抛出.
    """
    webhook_key = key or resolve_webhook_key()
    params = {"key": webhook_key}
    headers = {"Content-Type": "application/json", "charset": "utf-8"}

    if client is not None:
        response = client.post(config.WECHAT_SEND_URL, params=params, json=payload, headers=headers)
    else:
        with httpx.Client(timeout=config.WECHAT_TIMEOUT) as owned_client:
            response = owned_client.post(
                config.WECHAT_SEND_URL, params=params, json=payload, headers=headers
            )
    response.raise_for_status()
    result: dict[str, Any] = response.json()
    if result.get("errcode") != 0:
        logger.error(f"企业微信发送失败: {result}")
    return result


def upload_media(
    file_path: str | Path,
    *,
    key: str | None = None,
    client: httpx.Client | None = None,
) -> str:
    """上传文件素材, 拿到 media_id.

    Parameters
    ----------
    file_path : str | Path
        待上传的本地文件.
    key : str | None, default=None
        webhook key, 为 None 时从配置读取.
    client : httpx.Client | None, default=None
        复用外部客户端; 为 None 时临时创建一个.

    Returns
    -------
    str
        上传成功后返回的 media_id.

    Raises
    ------
    RuntimeError
        企业微信返回 errcode 非 0, 或回包里没有 media_id 时抛出.
    """
    webhook_key = key or resolve_webhook_key()
    path = Path(file_path)
    params = {"key": webhook_key, "type": "file"}

    with path.open("rb") as handle:
        files = {"file": (path.name, handle)}
        if client is not None:
            response = client.post(config.WECHAT_UPLOAD_URL, params=params, files=files)
        else:
            with httpx.Client(timeout=config.WECHAT_TIMEOUT) as owned_client:
                response = owned_client.post(config.WECHAT_UPLOAD_URL, params=params, files=files)
    response.raise_for_status()
    result: dict[str, Any] = response.json()
    if result.get("errcode") != 0:
        raise RuntimeError(f"企业微信素材上传失败: {result}")
    media_id = result.get("media_id")
    if not media_id:
        raise RuntimeError(f"企业微信素材上传成功但没返回 media_id: {result}")
    return str(media_id)


def send_file(
    file_path: str | Path,
    *,
    key: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """把本地文件当附件发出去.

    Parameters
    ----------
    file_path : str | Path
        待发送的本地文件.
    key : str | None, default=None
        webhook key, 为 None 时从配置读取.
    client : httpx.Client | None, default=None
        复用外部客户端.

    Returns
    -------
    dict[str, Any]
        发送文件消息后的返回结果.
    """
    media_id = upload_media(file_path, key=key, client=client)
    return send_message(build_file_payload(media_id), key=key, client=client)


def send_report(
    report_date: date,
    image_path: str | Path = config.REPORT_PNG_PATH,
    *,
    key: str | None = None,
    client: httpx.Client | None = None,
) -> None:
    """发送文本 + 图片一条龙.

    Parameters
    ----------
    report_date : date
        数据日期, 会拼进文本消息.
    image_path : str | Path, default=config.REPORT_PNG_PATH
        要发送的图片路径.
    key : str | None, default=None
        webhook key, 为 None 时从配置读取.
    client : httpx.Client | None, default=None
        复用外部客户端.
    """
    send_message(build_text_payload(build_report_text(report_date)), key=key, client=client)
    send_message(build_image_payload(image_path), key=key, client=client)
    logger.info(f"已推送 {report_date} 的两融报告")
