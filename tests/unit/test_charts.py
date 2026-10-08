"""charts 模块单元测试: 图表结构, 资源探测, 整页截图与渲染流程.

测试全部离线: 端口探测用 conftest 的 closed_port / listening_port 配 httpx.MockTransport,
浏览器部分把 ``eastmoneyrzrq.charts.sync_playwright`` 换成假的 context manager.
"""

import json
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from pyecharts.charts import Bar, Line, Page
from pyecharts.globals import CurrentConfig

from eastmoneyrzrq import charts, config

# 三行数据的日期, 与 chart_df 的 日期 列逐行对应
DATES = ["2026-09-28", "2026-09-29", "2026-09-30"]


@pytest.fixture
def chart_df() -> pl.DataFrame:
    """三行绘图数据, 金额取整方便手算期望的坐标轴边界."""
    return pl.DataFrame(
        {
            "日期": [date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30)],
            "融资融券余额": [2.5e12, 2.6e12, 2.7e12],
            "融资余额占流通市值比": [2.4, 2.45, 2.5],
            "融资买入额": [2.0e11, 1.9e11, 2.1e11],
            "融资偿还额": [1.95e11, 2.0e11, 1.9e11],
            "融资净买额": [5.0e9, 0.0, -3.0e10],
            "融券余额": [2.0e10, 2.1e10, 2.2e10],
            "融券净卖额": [1.0e7, 0.0, -2.0e7],
        }
    )


def _dump(chart: Any) -> dict[str, Any]:
    """把 pyecharts 对象 dump_options() 的 JSON 字符串解析成 dict."""
    return json.loads(chart.dump_options())


# --------------------------------------------------------------------------- #
# to_file_url
# --------------------------------------------------------------------------- #


def test_to_file_url_absolute_path(tmp_path: Path) -> None:
    target = tmp_path / "报告 页面.html"
    url = charts.to_file_url(target)
    assert url == f"file:///{target.resolve().as_posix()}"
    assert url.startswith("file:///")
    assert "\\" not in url


def test_to_file_url_relative_path() -> None:
    target = Path("docs") / "page.html"
    url = charts.to_file_url(target)
    assert url == f"file:///{(Path.cwd() / target).resolve().as_posix()}"
    assert url.startswith("file:///")
    assert "\\" not in url


# --------------------------------------------------------------------------- #
# scaled_values
# --------------------------------------------------------------------------- #


def test_scaled_values_divides_and_rounds_to_two_decimals() -> None:
    df = pl.DataFrame({"金额": [123456789.0, -987654321.0, 0.0]})
    result = charts.scaled_values(df, "金额", 10**8)
    assert result == [1.23, -9.88, 0.0]
    assert all(isinstance(value, float) for value in result)


def test_scaled_values_keeps_dataframe_order() -> None:
    df = pl.DataFrame({"v": [1.234, 2.346, -1.234, 10.0]})
    result = charts.scaled_values(df, "v", 1.0)
    # 顺序必须与 DataFrame 一致, 不能因为 round 或内部排序改变
    assert result == [1.23, 2.35, -1.23, 10.0]


# --------------------------------------------------------------------------- #
# axis_bounds
# --------------------------------------------------------------------------- #


def test_axis_bounds_relaxes_by_500_grid() -> None:
    assert charts.axis_bounds([1000.0, 2000.0]) == (-500, 2500)


def test_axis_bounds_single_value_and_large_scale() -> None:
    assert charts.axis_bounds([500.0]) == (-500, 500)
    assert charts.axis_bounds([1e10]) == (9499999500, 10500000500)


# --------------------------------------------------------------------------- #
# sign_colored_data
# --------------------------------------------------------------------------- #


def test_sign_colored_data_positive_red_negative_green_zero_red() -> None:
    data = charts.sign_colored_data([1.5, -2.5, 0.0], positive_is_up=True)
    assert data == [
        {"value": 1.5, "itemStyle": {"color": charts.UP_COLOR}},
        {"value": -2.5, "itemStyle": {"color": charts.DOWN_COLOR}},
        {"value": 0.0, "itemStyle": {"color": charts.UP_COLOR}},
    ]
    for item in data:
        assert set(item.keys()) == {"value", "itemStyle"}
        assert set(item["itemStyle"].keys()) == {"color"}


def test_sign_colored_data_swaps_colors_when_positive_is_down() -> None:
    values = [3.0, -4.0, 0.0]
    up_colors = [
        item["itemStyle"]["color"] for item in charts.sign_colored_data(values, positive_is_up=True)
    ]
    down_colors = [
        item["itemStyle"]["color"]
        for item in charts.sign_colored_data(values, positive_is_up=False)
    ]
    assert down_colors == [charts.DOWN_COLOR, charts.UP_COLOR, charts.DOWN_COLOR]
    assert up_colors == [charts.UP_COLOR, charts.DOWN_COLOR, charts.UP_COLOR]
    # 两位次的颜色正好互为相反的映射
    assert down_colors == [
        charts.DOWN_COLOR if color == charts.UP_COLOR else charts.UP_COLOR for color in up_colors
    ]


# --------------------------------------------------------------------------- #
# check_assets_server
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# build_balance_chart
# --------------------------------------------------------------------------- #


def test_build_balance_chart_axis_and_hidden_legend(chart_df: pl.DataFrame) -> None:
    chart = charts.build_balance_chart(chart_df)
    assert isinstance(chart, Line)
    option = _dump(chart)
    assert len(option["series"]) == 1
    assert option["series"][0]["type"] == "line"
    assert option["series"][0]["name"] == "两融余额"
    assert option["series"][0]["data"] == [
        ["2026-09-28", 25000.0],
        ["2026-09-29", 26000.0],
        ["2026-09-30", 27000.0],
    ]
    yaxis = option["yAxis"][0]
    assert yaxis["name"] == "亿"
    assert yaxis["min"] == 22500
    assert yaxis["max"] == 28500
    assert yaxis["minInterval"] == 500
    assert yaxis["maxInterval"] == 500
    assert option["legend"][0]["show"] is False
    assert option["title"][0]["text"] == "两融余额"


# --------------------------------------------------------------------------- #
# build_margin_flow_chart
# --------------------------------------------------------------------------- #


def test_build_margin_flow_chart_series_and_colors(chart_df: pl.DataFrame) -> None:
    chart = charts.build_margin_flow_chart(chart_df)
    assert isinstance(chart, Bar)
    option = _dump(chart)

    assert len(option["series"]) == 4
    net_buy = option["series"][0]
    assert net_buy["name"] == "净买额"
    assert net_buy["type"] == "bar"
    assert net_buy["yAxisIndex"] == 0
    assert net_buy["data"] == [
        {"value": 50.0, "itemStyle": {"color": charts.UP_COLOR}},
        {"value": 0.0, "itemStyle": {"color": charts.UP_COLOR}},
        {"value": -300.0, "itemStyle": {"color": charts.DOWN_COLOR}},
    ]

    buy, repay, ratio = option["series"][1], option["series"][2], option["series"][3]
    assert (buy["name"], buy["type"], buy["yAxisIndex"]) == ("买入额", "line", 0)
    assert buy["data"] == [
        ["2026-09-28", 2000.0],
        ["2026-09-29", 1900.0],
        ["2026-09-30", 2100.0],
    ]
    assert (repay["name"], repay["type"], repay["yAxisIndex"]) == ("偿还额", "line", 0)
    assert repay["data"] == [
        ["2026-09-28", 1950.0],
        ["2026-09-29", 2000.0],
        ["2026-09-30", 1900.0],
    ]
    assert ratio["name"] == "融资余额占流通市值比"
    assert ratio["type"] == "line"
    assert ratio["yAxisIndex"] == 1
    assert ratio["data"] == [
        ["2026-09-28", 2.4],
        ["2026-09-29", 2.45],
        ["2026-09-30", 2.5],
    ]


def test_build_margin_flow_chart_two_axes(chart_df: pl.DataFrame) -> None:
    option = _dump(charts.build_margin_flow_chart(chart_df))
    yaxes = option["yAxis"]
    assert len(yaxes) == 2
    assert [axis["name"] for axis in yaxes] == ["亿", "%"]
    assert yaxes[0].get("type") != "value"
    assert yaxes[1]["type"] == "value"
    assert option["title"][0]["text"] == "融资情况"


# --------------------------------------------------------------------------- #
# build_short_flow_chart
# --------------------------------------------------------------------------- #


def test_build_short_flow_chart_series_and_colors(chart_df: pl.DataFrame) -> None:
    chart = charts.build_short_flow_chart(chart_df)
    assert isinstance(chart, Bar)
    option = _dump(chart)

    assert len(option["series"]) == 2
    net_sell = option["series"][0]
    assert net_sell["name"] == "融券净卖额"
    assert net_sell["type"] == "bar"
    assert net_sell["yAxisIndex"] == 1
    # positive_is_up=False: 正数为绿, 负数为红
    assert net_sell["data"] == [
        {"value": 1.0, "itemStyle": {"color": charts.DOWN_COLOR}},
        {"value": 0.0, "itemStyle": {"color": charts.DOWN_COLOR}},
        {"value": -2.0, "itemStyle": {"color": charts.UP_COLOR}},
    ]

    balance = option["series"][1]
    assert (balance["name"], balance["type"], balance["yAxisIndex"]) == ("融券余额", "line", 0)
    assert balance["data"] == [
        ["2026-09-28", 200.0],
        ["2026-09-29", 210.0],
        ["2026-09-30", 220.0],
    ]


def test_build_short_flow_chart_two_axes(chart_df: pl.DataFrame) -> None:
    option = _dump(charts.build_short_flow_chart(chart_df))
    yaxes = option["yAxis"]
    assert len(yaxes) == 2
    assert [axis["name"] for axis in yaxes] == ["亿", "千万"]
    assert yaxes[1]["type"] == "value"
    assert option["title"][0]["text"] == "融券情况"


# --------------------------------------------------------------------------- #
# build_report_page
# --------------------------------------------------------------------------- #


def test_build_report_page_contains_three_titles(chart_df: pl.DataFrame) -> None:
    page = charts.build_report_page(chart_df)
    assert isinstance(page, Page)
    titles = [_dump(chart)["title"][0]["text"] for chart in page._charts]
    assert titles == ["两融余额", "融资情况", "融券情况"]
    assert [type(chart) for chart in page._charts] == [Line, Bar, Bar]


# --------------------------------------------------------------------------- #
# snapshot_full_page
# --------------------------------------------------------------------------- #


class _FakePage:
    """记录调用的假 page, 按需让 evaluate 抛异常以验证 finally."""

    def __init__(
        self, log: list[tuple[Any, ...]], height: int, fail_evaluate: bool = False
    ) -> None:
        self._log = log
        self._height = height
        self._fail_evaluate = fail_evaluate

    def goto(self, url: str, wait_until: str | None = None) -> None:
        self._log.append(("goto", url, wait_until))

    def evaluate(self, script: str) -> int:
        self._log.append(("evaluate",))
        if self._fail_evaluate:
            raise RuntimeError("evaluate 失败")
        return self._height

    def set_viewport_size(self, size: dict[str, int]) -> None:
        self._log.append(("viewport", dict(size)))

    def wait_for_timeout(self, milliseconds: int) -> None:
        self._log.append(("timeout", milliseconds))

    def screenshot(self, path: str | None = None, full_page: bool = False) -> None:
        self._log.append(("screenshot", path, full_page))


class _FakeBrowser:
    """假 browser, close() 会被记录."""

    def __init__(self, log: list[tuple[Any, ...]], page: _FakePage) -> None:
        self._log = log
        self._page = page
        self.closed = False

    def new_page(self, viewport: dict[str, int] | None = None) -> _FakePage:
        self._log.append(("new_page", dict(viewport) if viewport else None))
        return self._page

    def close(self) -> None:
        self.closed = True
        self._log.append(("close",))


class _FakeChromium:
    def __init__(self, log: list[tuple[Any, ...]], browser: _FakeBrowser) -> None:
        self._log = log
        self._browser = browser

    def launch(self, headless: bool = False) -> _FakeBrowser:
        self._log.append(("launch", headless))
        return self._browser


class _FakePlaywright:
    def __init__(self, log: list[tuple[Any, ...]], browser: _FakeBrowser) -> None:
        self.chromium = _FakeChromium(log, browser)


class _FakePlaywrightContext:
    def __init__(self, playwright: _FakePlaywright) -> None:
        self._playwright = playwright

    def __enter__(self) -> _FakePlaywright:
        return self._playwright

    def __exit__(self, *exc_info: object) -> None:
        return None


def _install_fake_playwright(
    monkeypatch: pytest.MonkeyPatch,
    log: list[tuple[Any, ...]],
    height: int,
    fail_evaluate: bool = False,
) -> _FakeBrowser:
    """把 charts.sync_playwright 换成假实现, 返回假 browser 便于断言 close()."""
    page = _FakePage(log, height, fail_evaluate)
    browser = _FakeBrowser(log, page)
    playwright = _FakePlaywright(log, browser)

    def factory() -> _FakePlaywrightContext:
        return _FakePlaywrightContext(playwright)

    monkeypatch.setattr(charts, "sync_playwright", factory)
    return browser


def test_snapshot_full_page_records_browser_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[tuple[Any, ...]] = []
    browser = _install_fake_playwright(monkeypatch, log, height=1234)
    html_path = tmp_path / "report.html"
    png_path = tmp_path / "report.png"

    result = charts.snapshot_full_page(html_path, png_path)

    assert result == Path(png_path)
    assert ("launch", True) in log
    assert ("goto", charts.to_file_url(html_path), "networkidle") in log
    assert ("new_page", dict(charts.PAGE_VIEWPORT)) in log
    assert ("viewport", {"width": 950, "height": 1234 + charts.SCREENSHOT_EXTRA_HEIGHT}) in log
    assert ("screenshot", str(png_path), True) in log
    assert ("close",) in log
    assert browser.closed is True


def test_snapshot_full_page_closes_browser_when_evaluate_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[tuple[Any, ...]] = []
    browser = _install_fake_playwright(monkeypatch, log, height=100, fail_evaluate=True)

    with pytest.raises(RuntimeError, match="evaluate 失败"):
        charts.snapshot_full_page(tmp_path / "a.html", tmp_path / "a.png")

    assert browser.closed is True
    assert ("close",) in log
    assert not any(entry[0] == "screenshot" for entry in log)


# --------------------------------------------------------------------------- #
# render_report
# --------------------------------------------------------------------------- #


def test_render_report_without_snapshot(
    tmp_path: Path, chart_df: pl.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    html_path = tmp_path / "report.html"
    png_path = tmp_path / "report.png"
    snapshot_calls: list[tuple[Path, Path]] = []
    monkeypatch.setattr(charts, "snapshot_full_page", lambda *a: snapshot_calls.append(a))
    monkeypatch.setenv(config.ASSETS_HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(config.ASSETS_PORT_ENV, "9527")
    monkeypatch.setattr(CurrentConfig, "ONLINE_HOST", "https://stale.invalid/")

    result = charts.render_report(chart_df, html_path=html_path, png_path=png_path, snapshot=False)

    assert result == html_path
    assert html_path.is_file()
    assert snapshot_calls == []
    assert config.assets_base_url() == CurrentConfig.ONLINE_HOST
    assert CurrentConfig.ONLINE_HOST == "http://127.0.0.1:9527/pyecharts_assets/v5/"


def test_render_report_with_snapshot(
    tmp_path: Path, chart_df: pl.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    html_path = tmp_path / "report.html"
    png_path = tmp_path / "report.png"
    recorded: dict[str, Path] = {}

    def fake_snapshot(html: Path, png: Path) -> Path:
        recorded["html"] = Path(html)
        recorded["png"] = Path(png)
        Path(png).write_bytes(b"fake-png")
        return Path(png)

    monkeypatch.setattr(charts, "snapshot_full_page", fake_snapshot)
    monkeypatch.setenv(config.ASSETS_HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(config.ASSETS_PORT_ENV, "9527")
    monkeypatch.setattr(CurrentConfig, "ONLINE_HOST", "https://stale.invalid/")

    result = charts.render_report(chart_df, html_path=html_path, png_path=png_path, snapshot=True)

    assert result == png_path
    assert recorded == {"html": html_path, "png": png_path}
    assert config.assets_base_url() == CurrentConfig.ONLINE_HOST


# --------------------------------------------------------------------------- #
# update_assets_host
# --------------------------------------------------------------------------- #


def test_update_assets_host_sets_pyecharts_online_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(config.ASSETS_HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(config.ASSETS_PORT_ENV, "9999")
    url = charts.update_assets_host()
    assert url == "http://127.0.0.1:9999/pyecharts_assets/v5/"
    assert url == config.assets_base_url()
    assert url == CurrentConfig.ONLINE_HOST
