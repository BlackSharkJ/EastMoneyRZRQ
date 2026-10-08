"""cli 模块单元测试: 参数解析, 运行分支与退出码.

所有外部依赖 (环境文件, 日志, 静态资源探测, 抓数, 渲染, 推送) 都用 monkeypatch 换成记录器,
断言返回码与副作用, 不触碰真实网络与文件.
"""

import argparse
import logging
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from eastmoneyrzrq import cli


@pytest.fixture
def cli_df() -> pl.DataFrame:
    """两行最小数据, 最后一行日期用来断言推送的 report_date."""
    return pl.DataFrame(
        {
            "日期": [date(2026, 9, 28), date(2026, 9, 30)],
            "融资净买额": [1.0, 2.0],
        }
    )


def _parse(*flags: str) -> argparse.Namespace:
    """按给定命令行参数解析出 Namespace."""
    return cli.build_parser().parse_args(list(flags))


# --------------------------------------------------------------------------- #
# build_parser
# --------------------------------------------------------------------------- #


def test_build_parser_defaults() -> None:
    args = _parse()
    assert args.from_csv is False
    assert args.no_send is False
    assert args.log_level == "INFO"


def test_build_parser_parses_all_flags() -> None:
    args = _parse("--no-send", "--from-csv", "--log-level", "DEBUG")
    assert args.no_send is True
    assert args.from_csv is True
    assert args.log_level == "DEBUG"


def test_build_parser_rejects_unknown_log_level() -> None:
    with pytest.raises(SystemExit) as excinfo:
        _parse("--log-level", "TRACE")
    assert excinfo.value.code == 2


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


def test_run_stops_when_assets_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    levels: list[int] = []
    fetch_calls: list[bool] = []
    send_calls: list[bool] = []
    render_calls: list[bool] = []
    monkeypatch.setattr(cli.config, "load_env_file", lambda: True)
    monkeypatch.setattr(cli.config, "setup_logging", levels.append)
    monkeypatch.setattr(cli.charts, "check_assets_server", lambda: False)
    monkeypatch.setattr(cli.data, "fetch_dataframe", lambda: fetch_calls.append(True))
    monkeypatch.setattr(cli.charts, "render_report", lambda df: render_calls.append(True))
    monkeypatch.setattr(cli.notify, "send_report", lambda d: send_calls.append(True))

    exit_code = cli.run(_parse("--log-level", "DEBUG"))

    assert exit_code == cli.EXIT_ASSETS_UNAVAILABLE == 1
    assert levels == [logging.DEBUG]
    assert fetch_calls == []
    assert render_calls == []
    assert send_calls == []


def test_run_no_send_renders_but_skips_notify(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cli_df: pl.DataFrame
) -> None:
    levels: list[int] = []
    render_calls: list[pl.DataFrame] = []
    send_calls: list[date] = []
    monkeypatch.setattr(cli.config, "load_env_file", lambda: True)
    monkeypatch.setattr(cli.config, "setup_logging", levels.append)
    monkeypatch.setattr(cli.charts, "check_assets_server", lambda: True)
    monkeypatch.setattr(cli.data, "fetch_dataframe", lambda: cli_df)
    monkeypatch.setattr(cli.data, "write_history_csv", lambda df: tmp_path / "history.csv")
    monkeypatch.setattr(cli.charts, "render_report", lambda df: render_calls.append(df))
    monkeypatch.setattr(cli.notify, "send_report", lambda d: send_calls.append(d))

    exit_code = cli.run(_parse("--no-send"))

    assert exit_code == cli.EXIT_OK == 0
    assert levels == [logging.INFO]
    assert render_calls == [cli_df]
    assert send_calls == []


def test_run_fetches_writes_history_and_sends_latest_date(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cli_df: pl.DataFrame
) -> None:
    written: list[pl.DataFrame] = []
    render_calls: list[pl.DataFrame] = []
    sent: list[date] = []
    monkeypatch.setattr(cli.config, "load_env_file", lambda: True)
    monkeypatch.setattr(cli.config, "setup_logging", lambda level: None)
    monkeypatch.setattr(cli.charts, "check_assets_server", lambda: True)
    monkeypatch.setattr(cli.data, "fetch_dataframe", lambda: cli_df)
    monkeypatch.setattr(
        cli.data, "write_history_csv", lambda df: (written.append(df), tmp_path / "h.csv")[1]
    )
    monkeypatch.setattr(
        cli.charts, "render_report", lambda df: (render_calls.append(df), tmp_path / "r.png")[1]
    )
    monkeypatch.setattr(cli.notify, "send_report", sent.append)

    exit_code = cli.run(_parse())

    assert exit_code == cli.EXIT_OK
    assert written == [cli_df]
    assert written[0] is cli_df
    assert render_calls == [cli_df]
    assert render_calls[0] is cli_df
    assert sent == [date(2026, 9, 30)]
    assert sent == [cli.data.latest_date(cli_df)]


def test_run_from_csv_missing_returns_data_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fetch_calls: list[bool] = []
    render_calls: list[bool] = []
    monkeypatch.setattr(cli.config, "load_env_file", lambda: True)
    monkeypatch.setattr(cli.config, "setup_logging", lambda level: None)
    monkeypatch.setattr(cli.charts, "check_assets_server", lambda: True)
    monkeypatch.setattr(cli.data, "read_history_csv", lambda: None)
    monkeypatch.setattr(cli.data, "fetch_dataframe", lambda: fetch_calls.append(True))
    monkeypatch.setattr(cli.charts, "render_report", lambda df: render_calls.append(True))

    exit_code = cli.run(_parse("--from-csv"))

    assert exit_code == cli.EXIT_DATA_MISSING == 2
    assert fetch_calls == []
    assert render_calls == []


def test_run_from_csv_existing_skips_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cli_df: pl.DataFrame
) -> None:
    render_calls: list[pl.DataFrame] = []
    sent: list[date] = []

    def fail_fetch() -> pl.DataFrame:
        raise AssertionError("--from-csv 时不应请求东财接口")

    monkeypatch.setattr(cli.config, "load_env_file", lambda: True)
    monkeypatch.setattr(cli.config, "setup_logging", lambda level: None)
    monkeypatch.setattr(cli.charts, "check_assets_server", lambda: True)
    monkeypatch.setattr(cli.data, "read_history_csv", lambda: cli_df)
    monkeypatch.setattr(cli.data, "fetch_dataframe", fail_fetch)
    monkeypatch.setattr(
        cli.charts, "render_report", lambda df: (render_calls.append(df), tmp_path / "r.png")[1]
    )
    monkeypatch.setattr(cli.notify, "send_report", sent.append)

    exit_code = cli.run(_parse("--from-csv"))

    assert exit_code == cli.EXIT_OK
    assert render_calls == [cli_df]
    assert sent == [date(2026, 9, 30)]


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def test_main_returns_run_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cli_df: pl.DataFrame
) -> None:
    levels: list[int] = []
    send_calls: list[date] = []
    monkeypatch.setattr(cli.config, "load_env_file", lambda: True)
    monkeypatch.setattr(cli.config, "setup_logging", levels.append)
    monkeypatch.setattr(cli.charts, "check_assets_server", lambda: True)
    monkeypatch.setattr(cli.data, "fetch_dataframe", lambda: cli_df)
    monkeypatch.setattr(cli.data, "write_history_csv", lambda df: tmp_path / "h.csv")
    monkeypatch.setattr(cli.charts, "render_report", lambda df: tmp_path / "r.png")
    monkeypatch.setattr(cli.notify, "send_report", send_calls.append)

    exit_code = cli.main(["--no-send", "--log-level", "DEBUG"])

    assert exit_code == cli.EXIT_OK == 0
    assert levels == [logging.DEBUG]
    assert isinstance(levels[0], int)
    assert send_calls == []


def test_main_returns_assets_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.config, "load_env_file", lambda: True)
    monkeypatch.setattr(cli.config, "setup_logging", lambda level: None)
    monkeypatch.setattr(cli.charts, "check_assets_server", lambda: False)

    assert cli.main(["--no-send"]) == cli.EXIT_ASSETS_UNAVAILABLE
