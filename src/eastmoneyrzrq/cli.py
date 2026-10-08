"""命令行入口: 检查静态资源 -> 抓数据 -> 落库 -> 渲染报告 -> 推送企业微信."""

import argparse
import logging
from collections.abc import Sequence

from . import assets, charts, config, data, notify, storage

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ASSETS_UNAVAILABLE = 1
EXIT_DATA_MISSING = 2


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器.

    Returns
    -------
    argparse.ArgumentParser
        支持 --from-db / --days / --no-send / --log-level 的参数解析器.
    """
    parser = argparse.ArgumentParser(
        prog="eastmoneyrzrq",
        description="抓取东方财富融资融券历史数据, 落库后生成日报图并推送到企业微信",
    )
    parser.add_argument(
        "--from-db",
        action="store_true",
        help="跳过抓取, 直接用库里的数据重画 (调配色时用这个, 不打搅接口)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=config.DEFAULT_PLOT_DAYS,
        help=f"绘图取最近多少个交易日, 默认 {config.DEFAULT_PLOT_DAYS}",
    )
    parser.add_argument(
        "--no-send",
        action="store_true",
        help="只生成图片, 不推送企业微信",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=sorted(logging.getLevelNamesMapping()),
        help="日志级别, 默认 INFO",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    """执行一次完整流程.

    Parameters
    ----------
    args : argparse.Namespace
        已解析的命令行参数.

    Returns
    -------
    int
        退出码: 0 成功, 1 静态资源不可用, 2 库里没有可用数据.

    Notes
    -----
    静态资源不可用时直接返回, 不继续抓数据和推送, 避免发出去一张空白图.
    外部服务没起时, ``assets.ensure_assets_server`` 会按配置临时自举一个服务,
    整个流程结束后自动关掉.
    抓下来的数据先落库, 再按 ``--days`` 从库里读回来绘图, 所以库里的历史有多长都能画.
    """
    config.load_env_file()
    config.setup_logging(logging.getLevelNamesMapping()[args.log_level])
    logger.info("开始运行两融日报")

    with assets.ensure_assets_server() as assets_source:
        if assets_source is None:
            logger.error("pyecharts 静态资源不可用, 已中止本次运行")
            return EXIT_ASSETS_UNAVAILABLE
        return _run_pipeline(args)


def _run_pipeline(args: argparse.Namespace) -> int:
    """执行抓取到推送的主体流程.

    Parameters
    ----------
    args : argparse.Namespace
        已解析的命令行参数.

    Returns
    -------
    int
        退出码: 0 成功, 2 库里没有可用数据.
    """
    if args.from_db:
        logger.info("已指定 --from-db, 跳过抓取")
    else:
        latest = data.fetch_dataframe()
        storage.save_daily(latest)
        logger.info(f"库内最新日期 {storage.latest_date()}, 累计 {storage.count_rows()} 行")

    history = storage.read_daily(args.days)
    if history is None:
        logger.error(f"数据库里没有可用数据: {config.HISTORY_DB_PATH}")
        return EXIT_DATA_MISSING

    report_date = data.latest_date(history)
    png_path = charts.render_report(history)
    logger.info(f"报告日期 {report_date}, 窗口 {history.height} 个交易日, 图片 {png_path}")

    if args.no_send:
        logger.info("已指定 --no-send, 跳过企业微信推送")
        return EXIT_OK

    notify.send_report(report_date)
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口.

    Parameters
    ----------
    argv : Sequence[str] | None, default=None
        命令行参数; 为 None 时取 sys.argv.

    Returns
    -------
    int
        进程退出码.
    """
    args = build_parser().parse_args(argv)
    return run(args)
