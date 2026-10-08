"""命令行入口: 抓数据 -> 渲染报告 -> 推送企业微信."""

import argparse
import logging
from collections.abc import Sequence

from . import charts, config, data, notify

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ASSETS_UNAVAILABLE = 1
EXIT_DATA_MISSING = 2


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器.

    Returns
    -------
    argparse.ArgumentParser
        支持 --from-csv / --no-send / --log-level 的参数解析器.
    """
    parser = argparse.ArgumentParser(
        prog="eastmoneyrzrq",
        description="抓取东方财富融资融券历史数据, 生成日报图并推送到企业微信",
    )
    parser.add_argument(
        "--from-csv",
        action="store_true",
        help="直接用本地 两融信息.csv 渲染, 不请求东财接口",
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
        退出码: 0 成功, 1 静态资源不可用, 2 本地数据缺失.

    Notes
    -----
    静态资源服务不可用时直接返回, 不继续抓数据和推送, 避免发出去一张空白图.
    """
    config.load_env_file()
    config.setup_logging(logging.getLevelNamesMapping()[args.log_level])
    logger.info("开始运行两融日报")

    if not charts.check_assets_server():
        logger.error("pyecharts 静态资源不可用, 已中止本次运行")
        return EXIT_ASSETS_UNAVAILABLE

    if args.from_csv:
        df = data.read_history_csv()
        if df is None:
            logger.error(f"本地数据不存在: {config.HISTORY_CSV_PATH}")
            return EXIT_DATA_MISSING
    else:
        df = data.fetch_dataframe()
        data.write_history_csv(df)

    report_date = data.latest_date(df)
    png_path = charts.render_report(df)
    logger.info(f"报告日期 {report_date}, 图片 {png_path}")

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
