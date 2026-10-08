"""data 模块单元测试: 请求参数, JSONP 解析, 记录提取与 DataFrame 整理."""

import json
from collections.abc import Callable
from datetime import date
from typing import Any

import httpx
import polars as pl
import pytest

from eastmoneyrzrq import config, data

Handler = Callable[[httpx.Request], httpx.Response]
MakeClient = Callable[[Handler], httpx.Client]


def _jsonp_text(records: list[dict[str, Any]]) -> str:
    """把记录列表包成东财风格的 JSONP 文本."""
    body = json.dumps({"result": {"data": records}}, ensure_ascii=False)
    return f"{config.EASTMONEY_JSONP_CALLBACK}({body})"


def test_build_request_params_exact_mapping() -> None:
    params = data.build_request_params(1700000000000)
    assert params == {
        "callback": "datatable2595798",
        "reportName": "RPTA_RZRQ_LSHJ",
        "columns": "ALL",
        "source": "WEB",
        "sortColumns": "dim_date",
        "sortTypes": -1,
        "pageNumber": 1,
        "pageSize": 50,
        "filter": "",
        "pageNo": 1,
        "_": 1700000000000,
    }


def test_build_request_params_reads_config_constants() -> None:
    params = data.build_request_params(1)
    assert params["callback"] == config.EASTMONEY_JSONP_CALLBACK
    assert params["reportName"] == config.EASTMONEY_REPORT_NAME == "RPTA_RZRQ_LSHJ"
    assert params["pageSize"] == config.EASTMONEY_PAGE_SIZE == 50
    assert params["sortTypes"] == -1
    assert params["filter"] == ""
    assert params["_"] == 1


def test_build_request_params_returns_fresh_dict() -> None:
    first = data.build_request_params(1)
    second = data.build_request_params(1)
    assert first is not second
    first["pageSize"] = 999
    assert second["pageSize"] == 50
    assert data.build_request_params(1)["pageSize"] == 50


def test_parse_jsonp_strips_wrapper() -> None:
    text = 'datatable2595798({"result": {"data": [{"RZYE": 1.5}]}})'
    assert data.parse_jsonp(text) == {"result": {"data": [{"RZYE": 1.5}]}}


def test_parse_jsonp_accepts_fixture_payload(
    jsonp_payload: str, raw_records: list[dict[str, Any]]
) -> None:
    assert data.parse_jsonp(jsonp_payload) == {"result": {"data": raw_records}}


def test_parse_jsonp_ignores_surrounding_noise() -> None:
    text = 'prefix ; datatable2595798({ "errcode": 0 }) ; suffix'
    assert data.parse_jsonp(text) == {"errcode": 0}


@pytest.mark.parametrize(
    "text",
    [
        "datatable2595798()",
        "",
        "callback([])",
        "no braces at all",
    ],
)
def test_parse_jsonp_rejects_missing_body(text: str) -> None:
    with pytest.raises(ValueError, match="响应里没有 JSON 对象"):
        data.parse_jsonp(text)


@pytest.mark.parametrize("text", ["callback({not json})", "callback({1: 2})", "callback({,,})"])
def test_parse_jsonp_rejects_invalid_json(text: str) -> None:
    with pytest.raises(ValueError, match="响应不是合法 JSON") as excinfo:
        data.parse_jsonp(text)
    assert isinstance(excinfo.value.__cause__, json.JSONDecodeError)


def test_parse_jsonp_error_keeps_text_prefix() -> None:
    text = "a" * 200
    with pytest.raises(ValueError) as excinfo:
        data.parse_jsonp(text)
    assert "a" * 120 in str(excinfo.value)
    assert "a" * 121 not in str(excinfo.value)


def test_parse_jsonp_rejects_array_body() -> None:
    """顶层是数组时直接报错: 静默截出数组里的第一个对象会拿到不完整的数据."""
    with pytest.raises(ValueError, match="主体是数组"):
        data.parse_jsonp('callback([{"RZYE": 1}])')

    with pytest.raises(ValueError, match="主体是数组"):
        data.parse_jsonp("callback([{}])")


@pytest.mark.parametrize("text", ['callback({"result": 1)', "}{"])
def test_parse_jsonp_rejects_unclosed_object(text: str) -> None:
    """完全没有右花括号, 或右花括号出现在左花括号之前时, 命中闭合检查."""
    with pytest.raises(ValueError, match="没有闭合"):
        data.parse_jsonp(text)


def test_parse_jsonp_rejects_truncated_json() -> None:
    """有右花括号但结构被截断时, 交给 JSON 解析器报错."""
    with pytest.raises(ValueError, match="不是合法 JSON"):
        data.parse_jsonp('callback({"result": {"data": []})')


def test_extract_records_returns_list() -> None:
    records = [{"RZYE": 1.0}, {"RZYE": 2.0}]
    assert data.extract_records({"result": {"data": records}}) == records


def test_extract_records_accepts_empty_list() -> None:
    assert data.extract_records({"result": {"data": []}}) == []
    assert data.extract_records({"result": {"data": [], "count": 0}}) == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"result": None},
        {"result": []},
        {"result": 7},
        {"result": "x"},
    ],
)
def test_extract_records_rejects_missing_result(payload: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="回包里没有 result 对象") as excinfo:
        data.extract_records(payload)
    assert repr(payload) in str(excinfo.value)


@pytest.mark.parametrize(
    "data_value",
    [None, {}, "x", 1, 1.5, True],
)
def test_extract_records_rejects_non_list_data(data_value: Any) -> None:
    with pytest.raises(ValueError, match=r"result\.data 不是列表") as excinfo:
        data.extract_records({"result": {"data": data_value}})
    assert type(data_value).__name__ in str(excinfo.value)


def test_to_dataframe_maps_columns_and_sorts(raw_records: list[dict[str, Any]]) -> None:
    df = data.to_dataframe(raw_records)

    assert df.columns == list(data.OUTPUT_COLUMNS)
    assert df.height == 3
    assert df.schema[data.DATE_COLUMN] == pl.Date
    assert df[data.DATE_COLUMN].to_list() == [
        date(2026, 9, 28),
        date(2026, 9, 29),
        date(2026, 9, 30),
    ]
    assert df["融资净买额"].to_list() == [-30000000000.0, 0.0, 5000000000.0]
    assert df["融券净卖额"].to_list() == [-20000000.0, 0.0, 10000000.0]
    assert df["融资余额"].to_list() == [2500000000000.0] * 3
    assert df["融资余额占流通市值比"].to_list() == [2.5, 2.5, 2.5]
    assert df["融资融券余额"].to_list() == [2520000000000.0] * 3
    assert df["融资偿还额"].to_list() == [195000000000.0] * 3
    assert df["融券余量"].to_list() == [3000000000.0] * 3


def test_to_dataframe_drops_columns_outside_output(raw_records: list[dict[str, Any]]) -> None:
    df = data.to_dataframe(raw_records)
    assert "涨跌幅" not in df.columns
    assert "沪深300" not in df.columns
    assert "融资融券余额差额" not in df.columns
    assert set(data.OUTPUT_COLUMNS).issubset(set(data.COLUMN_MAPPING.values()))
    assert list(data.OUTPUT_COLUMNS) == [
        data.COLUMN_MAPPING[key]
        for key in (
            "DIM_DATE",
            "RZYE",
            "RZYEZB",
            "RZMRE",
            "RZCHE",
            "RZJME",
            "RQYE",
            "RQYL",
            "RQCHL",
            "RQMCL",
            "RQJMG",
            "RZRQYE",
        )
    ]


def test_to_dataframe_single_row_date_dtype() -> None:
    record: dict[str, Any] = dict.fromkeys(data.COLUMN_MAPPING, 1.0)
    record["DIM_DATE"] = "2026-01-05 00:00:00"
    df = data.to_dataframe([record])
    assert df.height == 1
    assert df[data.DATE_COLUMN].to_list() == [date(2026, 1, 5)]
    assert df.schema[data.DATE_COLUMN] == pl.Date
    assert df["融资余额"].to_list() == [1.0]


def test_to_dataframe_accepts_records_missing_unused_columns(
    raw_records: list[dict[str, Any]],
) -> None:
    """接口只回必要字段时不应因为 COLUMN_MAPPING 里其他键缺失而整体失败."""
    assert set(raw_records[0]) < set(data.COLUMN_MAPPING)
    assert "ZDF" not in raw_records[0]

    df = data.to_dataframe(raw_records)

    assert df.height == 3
    assert df.columns == list(data.OUTPUT_COLUMNS)


@pytest.mark.parametrize(
    ("missing_key", "chinese_name"),
    [
        ("RZYE", "融资余额"),
        ("RQJMG", "融券净卖额"),
        ("DIM_DATE", "日期"),
    ],
)
def test_to_dataframe_rejects_missing_required_column(
    raw_records: list[dict[str, Any]], missing_key: str, chinese_name: str
) -> None:
    """绘图真正依赖的字段缺失时给明确的 ValueError, 而不是 polars 的 ColumnNotFoundError."""
    record = {key: value for key, value in raw_records[0].items() if key != missing_key}
    assert missing_key not in record

    with pytest.raises(ValueError, match="缺少绘图需要的字段") as excinfo:
        data.to_dataframe([record])

    assert chinese_name in str(excinfo.value)


def test_to_dataframe_rejects_empty_records() -> None:
    """空列表不能静默返回空 DataFrame, 否则后面取 date 时会炸在更远的地方."""
    with pytest.raises(ValueError, match="缺少绘图需要的字段"):
        data.to_dataframe([])


def test_fetch_raw_payload_parses_jsonp(
    make_client: MakeClient, jsonp_payload: str, raw_records: list[dict[str, Any]]
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=jsonp_payload)

    with make_client(handler) as client:
        payload = data.fetch_raw_payload(client=client)

    assert payload == {"result": {"data": raw_records}}
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "GET"
    assert request.url.scheme == "https"
    assert request.url.host == "datacenter-web.eastmoney.com"
    assert request.url.path == "/api/data/v1/get"
    assert str(request.url).startswith(config.EASTMONEY_ENDPOINT)
    assert request.url.params["callback"] == config.EASTMONEY_JSONP_CALLBACK
    assert request.url.params["reportName"] == config.EASTMONEY_REPORT_NAME
    assert request.url.params["columns"] == "ALL"
    assert request.url.params["source"] == "WEB"
    assert request.url.params["sortColumns"] == "dim_date"
    assert request.url.params["sortTypes"] == "-1"
    assert request.url.params["pageNumber"] == "1"
    assert request.url.params["pageNo"] == "1"
    assert request.url.params["pageSize"] == str(config.EASTMONEY_PAGE_SIZE)
    assert request.url.params["filter"] == ""
    assert int(request.url.params["_"]) > 1_000_000_000_000
    assert request.headers["user-agent"] == config.EASTMONEY_USER_AGENT


def test_fetch_raw_payload_raises_on_http_500(make_client: MakeClient) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with make_client(handler) as client, pytest.raises(httpx.HTTPStatusError) as excinfo:
        data.fetch_raw_payload(client=client)

    assert excinfo.value.response.status_code == 500
    assert excinfo.value.response.text == "boom"
    assert str(excinfo.value.request.url).startswith(config.EASTMONEY_ENDPOINT)


def test_fetch_raw_payload_raises_on_http_404(make_client: MakeClient) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "not found"})

    with make_client(handler) as client, pytest.raises(httpx.HTTPStatusError) as excinfo:
        data.fetch_raw_payload(client=client)

    assert excinfo.value.response.status_code == 404
    assert excinfo.value.response.json() == {"message": "not found"}


def test_fetch_dataframe_end_to_end(
    make_client: MakeClient, raw_records: list[dict[str, Any]]
) -> None:
    records = raw_records

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_jsonp_text(records))

    with make_client(handler) as client:
        df = data.fetch_dataframe(client=client)

    assert df.height == 3
    assert df.columns == list(data.OUTPUT_COLUMNS)
    assert df[data.DATE_COLUMN].to_list() == [
        date(2026, 9, 28),
        date(2026, 9, 29),
        date(2026, 9, 30),
    ]
    assert df["融资融券余额"].to_list() == [2520000000000.0] * 3
    assert df.schema[data.DATE_COLUMN] == pl.Date


def test_fetch_dataframe_propagates_bad_payload(make_client: MakeClient) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='callback({"result": {"data": "oops"}})')

    with make_client(handler) as client, pytest.raises(ValueError, match=r"result\.data 不是列表"):
        data.fetch_dataframe(client=client)


def test_latest_date_returns_newest(raw_records: list[dict[str, Any]]) -> None:
    df = data.to_dataframe(raw_records)
    assert data.latest_date(df) == date(2026, 9, 30)


def test_config_paths_are_under_project_root() -> None:
    assert config.HISTORY_DB_PATH == config.PROJECT_ROOT / "rzrq.sqlite3"
    assert config.ENV_FILE_PATH == config.PROJECT_ROOT / ".env"


def test_build_client_uses_config_default_timeout() -> None:
    client = data.build_client()
    try:
        assert client.timeout == httpx.Timeout(config.REQUEST_TIMEOUT)
        assert client.is_closed is False
    finally:
        client.close()


def test_build_client_accepts_custom_timeout() -> None:
    client = data.build_client(2.5)
    try:
        assert client.timeout == httpx.Timeout(2.5)
    finally:
        client.close()


def test_fetch_raw_payload_creates_owned_client(
    monkeypatch: pytest.MonkeyPatch,
    make_client: MakeClient,
    jsonp_payload: str,
    raw_records: list[dict[str, Any]],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=jsonp_payload)

    injected = make_client(handler)
    timeouts: list[float] = []

    def fake_build_client(timeout: float = config.REQUEST_TIMEOUT) -> httpx.Client:
        timeouts.append(timeout)
        return injected

    monkeypatch.setattr(data, "build_client", fake_build_client)

    assert data.fetch_raw_payload() == {"result": {"data": raw_records}}
    assert timeouts == [config.REQUEST_TIMEOUT]
    assert injected.is_closed is True
