"""融资融券数据的获取与解析.

东财接口返回的是 JSONP (外面裹了一层 callback), 所以解析时要先剥壳.
所有对外函数都允许注入 httpx.Client, 方便测试里换成 MockTransport, 不需要真连网.
落库与读取由 storage 模块负责, 这里只管把网络上的数据变成 DataFrame.
"""

import json
import logging
import time
from datetime import date
from typing import Any, Final

import httpx
import polars as pl

from . import config

logger = logging.getLogger(__name__)

# 东财原始字段名 -> 中文列名
COLUMN_MAPPING: Final[dict[str, str]] = {
    "DIM_DATE": "日期",
    "NEW": "沪深300",
    "ZDF": "涨跌幅",
    "LTSZ": "流通市值",
    "ZDF3D": "涨跌幅3D",
    "ZDF5D": "涨跌幅5D",
    "ZDF10D": "涨跌幅10D",
    "RZYE": "融资余额",
    "RZYEZB": "融资余额占流通市值比",
    "RZMRE": "融资买入额",
    "RZMRE3D": "融资买入额3D",
    "RZMRE5D": "融资买入额5D",
    "RZMRE10D": "融资买入额10D",
    "RZCHE": "融资偿还额",
    "RZCHE3D": "融资偿还额3D",
    "RZCHE5D": "融资偿还额5D",
    "RZCHE10D": "融资偿还额10D",
    "RZJME": "融资净买额",
    "RZJME3D": "融资净买额3D",
    "RZJME5D": "融资净买额5D",
    "RZJME10D": "融资净买额10D",
    "RQYE": "融券余额",
    "RQYL": "融券余量",
    "RQCHL": "融券偿还量",
    "RQCHL3D": "融券偿还量3D",
    "RQCHL5D": "融券偿还量5D",
    "RQCHL10D": "融券偿还量10D",
    "RQMCL": "融券卖出量",
    "RQMCL3D": "融券卖出量3D",
    "RQMCL5D": "融券卖出量5D",
    "RQMCL10D": "融券卖出量10D",
    "RQJMG": "融券净卖额",
    "RQJMG3D": "融券净卖额3D",
    "RQJMG5D": "融券净卖额5D",
    "RQJMG10D": "融券净卖额10D",
    "RZRQYE": "融资融券余额",
    "RZRQYECZ": "融资融券余额差额",
}

# 绘图与落库需要的列, 顺序即 DataFrame 的列顺序
OUTPUT_COLUMNS: Final[tuple[str, ...]] = (
    "日期",
    "融资余额",
    "融资余额占流通市值比",
    "融资买入额",
    "融资偿还额",
    "融资净买额",
    "融券余额",
    "融券余量",
    "融券偿还量",
    "融券卖出量",
    "融券净卖额",
    "融资融券余额",
)

DATE_COLUMN: Final[str] = "日期"

# polars 的日期解析要显式给格式, 否则跨平台行为不一致
DATE_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"


def build_client(timeout: float = config.REQUEST_TIMEOUT) -> httpx.Client:
    """创建一个带默认超时的 httpx 客户端.

    Parameters
    ----------
    timeout : float, default=config.REQUEST_TIMEOUT
        单次请求的超时时间, 单位秒.

    Returns
    -------
    httpx.Client
        调用方负责关闭, 或者用 with 语句托管.
    """
    return httpx.Client(timeout=timeout)


def build_request_params(timestamp_ms: int) -> dict[str, Any]:
    """拼出东财接口的查询参数.

    Parameters
    ----------
    timestamp_ms : int
        毫秒时间戳, 作为缓存破坏参数传给接口.

    Returns
    -------
    dict[str, Any]
        查询参数字典, 按 dim_date 倒序取最近 config.EASTMONEY_PAGE_SIZE 条.
    """
    return {
        "callback": config.EASTMONEY_JSONP_CALLBACK,
        "reportName": config.EASTMONEY_REPORT_NAME,
        "columns": "ALL",
        "source": "WEB",
        "sortColumns": "dim_date",
        "sortTypes": -1,
        "pageNumber": 1,
        "pageSize": config.EASTMONEY_PAGE_SIZE,
        "filter": "",
        "pageNo": 1,
        "_": timestamp_ms,
    }


def parse_jsonp(text: str) -> dict[str, Any]:
    """剥掉 JSONP 外壳, 解析出 JSON 主体.

    Parameters
    ----------
    text : str
        形如 ``datatable2595798({...})`` 的响应文本.

    Returns
    -------
    dict[str, Any]
        解析后的 JSON 对象.

    Raises
    ------
    ValueError
        找不到 JSON 主体, 主体是数组而不是对象, 或主体不是合法 JSON 时抛出.
    """
    # 用首尾花括号定位主体, 比正则更稳, 也不会被字符串里的括号骗到.
    # 先比一下方括号的位置: 顶层是数组时直接报错, 不要静默截出数组里的第一个对象
    object_start = text.find("{")
    array_start = text.find("[")
    if object_start == -1:
        raise ValueError(f"响应里没有 JSON 对象: {text[:120]!r}")
    if array_start != -1 and array_start < object_start:
        raise ValueError(f"接口回包主体是数组, 期望对象: {text[:120]!r}")

    object_end = text.rfind("}")
    if object_end <= object_start:
        raise ValueError(f"JSON 对象没有闭合: {text[:120]!r}")
    try:
        payload = json.loads(text[object_start : object_end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"响应不是合法 JSON: {exc}") from exc
    return payload


def extract_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """从接口回包里取出数据列表.

    Parameters
    ----------
    payload : dict[str, Any]
        解析后的接口回包.

    Returns
    -------
    list[dict[str, Any]]
        原始字段名组成的记录列表.

    Raises
    ------
    ValueError
        回包结构不符合预期 (result.data 不是列表) 时抛出.
    """
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ValueError(f"回包里没有 result 对象: {payload!r}")
    records = result.get("data")
    if not isinstance(records, list):
        raise ValueError(f"result.data 不是列表: {type(records).__name__}")
    return records


def fetch_raw_payload(*, client: httpx.Client | None = None) -> dict[str, Any]:
    """请求东财接口并解析回包.

    Parameters
    ----------
    client : httpx.Client | None, default=None
        复用外部客户端 (测试里注入 MockTransport); 为 None 时临时创建一个.

    Returns
    -------
    dict[str, Any]
        解析后的接口回包.
    """
    params = build_request_params(int(time.time() * 1000))
    headers = {"user-agent": config.EASTMONEY_USER_AGENT}
    if client is not None:
        response = client.get(config.EASTMONEY_ENDPOINT, params=params, headers=headers)
    else:
        with build_client() as owned_client:
            response = owned_client.get(config.EASTMONEY_ENDPOINT, params=params, headers=headers)
    response.raise_for_status()
    return parse_jsonp(response.text)


def to_dataframe(records: list[dict[str, Any]]) -> pl.DataFrame:
    """把原始记录整理成绘图用的 DataFrame.

    Parameters
    ----------
    records : list[dict[str, Any]]
        接口返回的原始记录, 字段是英文大写名.

    Returns
    -------
    pl.DataFrame
        只保留 OUTPUT_COLUMNS 里的列, 日期已转成 pl.Date, 并按日期升序排列.

    Raises
    ------
    ValueError
        记录里缺少 OUTPUT_COLUMNS 需要的字段时抛出.
    """
    df = pl.DataFrame(records)
    # 接口用 columns=ALL 会多给一堆用不到的字段, 字段也可能增减,
    # 所以只映射真实存在的列, 列名严格模式在这里反而会让整次运行失败
    mapping = {
        english: chinese for english, chinese in COLUMN_MAPPING.items() if english in df.columns
    }
    df = df.rename(mapping)

    # 绘图真正依赖的列必须齐全, 缺了就直接报错, 不要带着空列画下去
    missing = [name for name in OUTPUT_COLUMNS if name not in df.columns]
    if missing:
        raise ValueError(f"接口回包缺少绘图需要的字段: {missing}")

    df = df.with_columns(pl.col(DATE_COLUMN).str.to_date(DATE_FORMAT))
    return df.select(list(OUTPUT_COLUMNS)).sort(DATE_COLUMN)


def fetch_dataframe(*, client: httpx.Client | None = None) -> pl.DataFrame:
    """抓取最新的融资融券数据.

    Parameters
    ----------
    client : httpx.Client | None, default=None
        复用外部客户端, 为 None 时内部临时创建一个.

    Returns
    -------
    pl.DataFrame
        按日期升序排列的 DataFrame.
    """
    payload = fetch_raw_payload(client=client)
    df = to_dataframe(extract_records(payload))
    logger.info(f"抓取到 {df.height} 行数据, 最新日期 {df[DATE_COLUMN][-1]}")
    return df


def latest_date(df: pl.DataFrame) -> date:
    """取 DataFrame 里最新的一条日期.

    Parameters
    ----------
    df : pl.DataFrame
        已经按日期升序排列的 DataFrame.

    Returns
    -------
    date
        最新日期.
    """
    return df[DATE_COLUMN][-1]
