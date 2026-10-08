"""图表构建与整页截图.

三张图都基于 pyecharts:
- 两融余额: 单折线
- 融资情况: 净买额柱状图 (红涨绿跌) + 买入额/偿还额/占流通市值比三条折线, 双 y 轴
- 融券情况: 净卖额柱状图 (绿涨红跌) + 融券余额折线, 双 y 轴

凡是混合图都让 Bar 当宿主图: ``Chart.overlap`` 只合并 legend 与 series, 另一张图的 yAxis 会被丢掉,
所以第二条 y 轴必须用宿主的 ``extend_axis`` 建出来.

静态资源服务的探活与临时自举在 ``assets`` 模块, 这里只管画图与截图.
"""

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final, TypedDict

import polars as pl
from playwright.sync_api import sync_playwright
from pyecharts import options as opts
from pyecharts.charts import Bar, Line, Page
from pyecharts.globals import CurrentConfig

from . import config

logger = logging.getLogger(__name__)

# A 股习惯: 红涨绿跌. 净买额为正代表买盘占优, 净卖额为正代表卖盘占优, 两者方向相反
UP_COLOR: Final[str] = "#c23531"
DOWN_COLOR: Final[str] = "#2f8f4e"

MONEY_UNIT: Final[float] = 10**8
SHORT_MONEY_UNIT: Final[float] = 10**7


class ViewportSize(TypedDict):
    """浏览器视口尺寸, 字段名要跟 playwright 对齐."""

    width: int
    height: int


PAGE_VIEWPORT: Final[ViewportSize] = {"width": 950, "height": 1080}
SCREENSHOT_EXTRA_HEIGHT: Final[int] = 100

# pyecharts 2.0.x 把 add_yaxis 的 y_axis 标成 Sequence[LineItem | dict], 漏了数值类型,
# 而运行时就是接受 list[float] 的. 这里用别名收口一次, 免得每个调用点都写 cast
type AxisSeries = Any


def update_assets_host() -> str:
    """把 pyecharts 的静态资源地址指向本地服务.

    Returns
    -------
    str
        设置后的静态资源基础 URL.

    Notes
    -----
    pyecharts 把 ``_CurrentConfig.ONLINE_HOST`` 声明成 Final, 静态类型检查会认为它不可写,
    但渲染引擎生成 ``<script>`` 标签时只认这个全局值, 所以只能覆盖它.
    """
    CurrentConfig.ONLINE_HOST = config.assets_base_url()  # type: ignore[reportAttributeAccessIssue]
    logger.debug(f"pyecharts 静态资源地址: {CurrentConfig.ONLINE_HOST}")
    return CurrentConfig.ONLINE_HOST


def scaled_values(df: pl.DataFrame, column: str, divisor: float) -> AxisSeries:
    """把金额列缩放后转成 pyecharts 需要的列表.

    Parameters
    ----------
    df : pl.DataFrame
        数据源.
    column : str
        列名.
    divisor : float
        除数, 例如 10**8 表示换算成亿.

    Returns
    -------
    AxisSeries
        保留两位小数后的数值列表.
    """
    return (df[column] / divisor).round(2).to_list()


def axis_bounds(values: Sequence[float]) -> tuple[int, int]:
    """按 500 的整格把坐标轴上下界放宽一点.

    Parameters
    ----------
    values : Sequence[float]
        轴上的数值序列.

    Returns
    -------
    tuple[int, int]
        (下界, 上界), 两者都是 500 的奇数倍, 保证轴标签落在整数格上.

    Notes
    -----
    上下界分别用 0.95 和 1.05 的系数放宽, 避免曲线贴边.
    """
    upper = int(max(values) * 1.05 / 1000) * 1000 + 500
    lower = int(min(values) * 0.95 / 1000) * 1000 - 500
    return lower, upper


def sign_colored_data(values: Sequence[float], *, positive_is_up: bool) -> list[dict[str, Any]]:
    """给每个数据点按正负上色.

    Parameters
    ----------
    values : Sequence[float]
        数值序列, 单位已经换算好.
    positive_is_up : bool
        正值是否用红色. 净买额传 True, 净卖额传 False.

    Returns
    -------
    list[dict[str, Any]]
        pyecharts 的柱状图数据, 每项形如 ``{"value": 7.5, "itemStyle": {"color": "#c23531"}}``.

    Notes
    -----
    逐点写 itemStyle 比 visualMap 更直接: 既不依赖 visualMap 的 dimension 推断,
    也不会顺带影响同一张图里的折线系列.
    """
    positive_color = UP_COLOR if positive_is_up else DOWN_COLOR
    negative_color = DOWN_COLOR if positive_is_up else UP_COLOR
    return [
        {
            "value": value,
            "itemStyle": {"color": positive_color if value >= 0 else negative_color},
        }
        for value in values
    ]


def to_file_url(path: str | Path) -> str:
    """把本地路径转成 file:// 协议地址, 供浏览器访问.

    Parameters
    ----------
    path : str | Path
        本地文件路径, 可以是相对路径.

    Returns
    -------
    str
        形如 ``file:///C:/dir/page.html`` 的地址.
    """
    return f"file:///{Path(path).resolve().as_posix()}"


def build_balance_chart(df: pl.DataFrame) -> Line:
    """构建「两融余额」折线图.

    Parameters
    ----------
    df : pl.DataFrame
        融资融券历史数据.

    Returns
    -------
    Line
        已配置好的折线图对象.
    """
    x = df["日期"].to_list()
    balance = scaled_values(df, "融资融券余额", MONEY_UNIT)
    lower, upper = axis_bounds(balance)

    return (
        Line(
            init_opts=opts.InitOpts(
                bg_color="white", animation_opts=opts.AnimationOpts(animation=False)
            )
        )
        .add_xaxis(x)
        .add_yaxis(series_name="两融余额", y_axis=balance, is_symbol_show=False)
        .set_series_opts(label_opts=opts.LabelOpts(is_show=False))
        .set_global_opts(
            legend_opts=opts.LegendOpts(is_show=False),
            title_opts=opts.TitleOpts(title="两融余额"),
            yaxis_opts=opts.AxisOpts(
                name="亿",
                min_=lower,
                max_=upper,
                min_interval=500,
                max_interval=500,
            ),
        )
    )


def build_margin_flow_chart(df: pl.DataFrame) -> Bar:
    """构建「融资情况」图: 净买额柱状图 + 三条折线, 双 y 轴.

    Parameters
    ----------
    df : pl.DataFrame
        融资融券历史数据.

    Returns
    -------
    Bar
        宿主图为柱状图的混合图, 柱子在左侧「亿」轴上, 占比线在右侧「%」轴上.
    """
    x = df["日期"].to_list()
    buy = scaled_values(df, "融资买入额", MONEY_UNIT)
    repay = scaled_values(df, "融资偿还额", MONEY_UNIT)
    net_buy = scaled_values(df, "融资净买额", MONEY_UNIT)
    ratio = df["融资余额占流通市值比"].round(2).to_list()

    # 净买额为正说明买盘占优, 用红柱; 为负说明在净偿还, 用绿柱
    net_buy_data = sign_colored_data(net_buy, positive_is_up=True)

    bar = (
        Bar(
            init_opts=opts.InitOpts(
                bg_color="white", animation_opts=opts.AnimationOpts(animation=False)
            )
        )
        .add_xaxis(x)
        .add_yaxis(
            series_name="净买额",
            y_axis=net_buy_data,
            # 图例色块跟随净买入的红色, 柱子本身由逐点 itemStyle 决定
            itemstyle_opts=opts.ItemStyleOpts(color=UP_COLOR),
            label_opts={"is_show": False, "text_width": 0, "overflow": "truncate"},
            yaxis_index=0,
        )
        .extend_axis(
            yaxis=opts.AxisOpts(
                name="%",
                type_="value",
            )
        )
        .set_series_opts(label_opts=opts.LabelOpts(is_show=False))
        .set_global_opts(
            title_opts=opts.TitleOpts(title="融资情况"),
            yaxis_opts=opts.AxisOpts(name="亿"),
        )
    )

    # 折线图只贡献 series, 坐标轴沿用上面的柱状图, 否则双 y 轴会丢失
    line = (
        Line()
        .add_xaxis(x)
        .add_yaxis(
            series_name="买入额",
            y_axis=buy,
            is_symbol_show=False,
            label_opts={"is_show": False, "text_width": 0, "overflow": "truncate"},
            yaxis_index=0,
        )
        .add_yaxis(
            series_name="偿还额",
            y_axis=repay,
            is_symbol_show=False,
            label_opts={"is_show": False, "text_width": 0, "overflow": "truncate"},
            yaxis_index=0,
        )
        .add_yaxis(
            series_name="融资余额占流通市值比",
            y_axis=ratio,
            is_symbol_show=False,
            label_opts={"is_show": False, "text_width": 0, "overflow": "truncate"},
            yaxis_index=1,
        )
        .set_series_opts(label_opts=opts.LabelOpts(is_show=False))
    )

    return bar.overlap(line)


def build_short_flow_chart(df: pl.DataFrame) -> Bar:
    """构建「融券情况」图: 净卖额柱状图 + 融券余额折线, 双 y 轴.

    Parameters
    ----------
    df : pl.DataFrame
        融资融券历史数据.

    Returns
    -------
    Bar
        宿主图为柱状图的混合图, 柱子在右侧「千万」轴上, 余额线在左侧「亿」轴上.
    """
    x = df["日期"].to_list()
    balance = scaled_values(df, "融券余额", MONEY_UNIT)
    net_sell = scaled_values(df, "融券净卖额", SHORT_MONEY_UNIT)

    # 净卖额为正说明卖盘占优, 用绿柱; 为负说明空头在偿还, 用红柱
    net_sell_data = sign_colored_data(net_sell, positive_is_up=False)

    bar = (
        Bar(
            init_opts=opts.InitOpts(
                bg_color="white", animation_opts=opts.AnimationOpts(animation=False)
            )
        )
        .add_xaxis(x)
        .add_yaxis(
            series_name="融券净卖额",
            y_axis=net_sell_data,
            # 图例色块跟随净卖出的绿色, 柱子本身由逐点 itemStyle 决定
            itemstyle_opts=opts.ItemStyleOpts(color=DOWN_COLOR),
            yaxis_index=1,
        )
        .extend_axis(
            yaxis=opts.AxisOpts(
                name="千万",
                type_="value",
            )
        )
        .set_series_opts(label_opts=opts.LabelOpts(is_show=False))
        .set_global_opts(
            title_opts=opts.TitleOpts(title="融券情况"),
            yaxis_opts=opts.AxisOpts(name="亿"),
        )
    )

    # 折线图只贡献 series, 坐标轴沿用上面的柱状图
    line = (
        Line()
        .add_xaxis(x)
        .add_yaxis(series_name="融券余额", y_axis=balance, is_symbol_show=False, yaxis_index=0)
        .set_series_opts(label_opts=opts.LabelOpts(is_show=False))
    )

    return bar.overlap(line)


def build_report_page(df: pl.DataFrame) -> Page:
    """把三张图拼成一个页面.

    Parameters
    ----------
    df : pl.DataFrame
        融资融券历史数据.

    Returns
    -------
    Page
        从上到下依次是两融余额, 融资情况, 融券情况.
    """
    return (
        Page()
        .add(build_balance_chart(df))
        .add(build_margin_flow_chart(df))
        .add(build_short_flow_chart(df))
    )


def snapshot_full_page(html_path: str | Path, png_path: str | Path) -> Path:
    """用 chromium 打开本地 HTML 并整页截图.

    Parameters
    ----------
    html_path : str | Path
        待截图的本地 HTML 路径.
    png_path : str | Path
        截图输出路径.

    Returns
    -------
    Path
        截图输出路径.

    Notes
    -----
    视口高度先量一遍页面真实高度再设置, 否则 pyecharts 的 canvas 会被截断.
    """
    output = Path(png_path)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport=PAGE_VIEWPORT)
        try:
            page.goto(to_file_url(html_path), wait_until="networkidle")

            required_height = page.evaluate("""() => {
                return Math.max(
                    document.body.scrollHeight, document.documentElement.scrollHeight,
                    document.body.offsetHeight, document.documentElement.offsetHeight,
                    document.body.clientHeight, document.documentElement.clientHeight
                );
            }""")
            page.set_viewport_size(
                {
                    "width": PAGE_VIEWPORT["width"],
                    "height": required_height + SCREENSHOT_EXTRA_HEIGHT,
                }
            )
            page.wait_for_timeout(500)
            page.screenshot(path=str(output), full_page=True)
        finally:
            browser.close()
    logger.info(f"已生成整页截图: {output}")
    return output


def render_report(
    df: pl.DataFrame,
    *,
    html_path: Path = config.REPORT_HTML_PATH,
    png_path: Path = config.REPORT_PNG_PATH,
    snapshot: bool = True,
) -> Path:
    """渲染报告页面并按需截图.

    Parameters
    ----------
    df : pl.DataFrame
        融资融券历史数据.
    html_path : Path, default=config.REPORT_HTML_PATH
        HTML 输出路径.
    png_path : Path, default=config.REPORT_PNG_PATH
        截图输出路径.
    snapshot : bool, default=True
        是否调用 chromium 截图, 单测里通常关掉.

    Returns
    -------
    Path
        截图路径; snapshot 为 False 时返回 HTML 路径.
    """
    update_assets_host()
    html_file = Path(build_report_page(df).render(str(html_path)))
    logger.info(f"已渲染报告页面: {html_file}")
    if not snapshot:
        return html_file
    return snapshot_full_page(html_file, png_path)
