"""cli 模块单元测试: 参数解析, 运行分支, 退出码与参数传递.

所有外部依赖 (环境文件, 日志, 静态资源探测与自举, 抓数, 落库, 读库, 渲染, 推送)
都用 monkeypatch 换成记录器, 只断言返回码, 参数与副作用,
不触碰真实网络, 浏览器与项目根目录的 rzrq.sqlite3.
"""

import argparse
import logging
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from eastmoneyrzrq import assets, cli, config

ReportDate = date


def _parse(*flags: str) -> argparse.Namespace:
    """按给定命令行参数解析出 Namespace."""
    return cli.build_parser().parse_args(list(flags))


def _fake_ensure_assets_server(
    source: str | None,
) -> Callable[[], AbstractContextManager[str | None]]:
    """造一个假的 ensure_assets_server: 只 yield 给定的来源, 不做真实探测."""

    @contextmanager
    def _context() -> Iterator[str | None]:
        yield source

    return _context


def _stub_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    flow_df: pl.DataFrame,
    png_path: Path,
    assets_source: str | None = assets.SOURCE_EXTERNAL,
) -> dict[str, list]:
    """把 run 依赖的每一步换成记录器, 返回各步骤的调用记录.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        用于替换依赖的 fixture.
    flow_df : pl.DataFrame
        read_daily (以及 fetch_dataframe) 返回的数据.
    png_path : Path
        render_report 返回的路径.
    assets_source : str | None, default=assets.SOURCE_EXTERNAL
        ensure_assets_server 交出的来源, 为 None 表示静态资源不可用.

    Returns
    -------
    dict[str, list]
        各步骤的调用记录, key 分别是 env / log / fetch / save / read /
        render / send / latest / count.
    """
    record: dict[str, list] = {
        "env": [],
        "log": [],
        "fetch": [],
        "save": [],
        "read": [],
        "render": [],
        "send": [],
        "latest": [],
        "count": [],
    }

    def load_env() -> bool:
        record["env"].append(True)
        return True

    def setup_logging(level: int) -> None:
        record["log"].append(level)

    def fetch() -> pl.DataFrame:
        record["fetch"].append(True)
        return flow_df

    def save_daily(df: pl.DataFrame) -> int:
        record["save"].append(df)
        return df.height

    def read_daily(days: int) -> pl.DataFrame:
        record["read"].append(days)
        return flow_df

    def render(df: pl.DataFrame) -> Path:
        record["render"].append(df)
        return png_path

    def send(report_date: ReportDate) -> None:
        record["send"].append(report_date)

    def latest() -> date:
        record["latest"].append(True)
        return date(2026, 9, 30)

    def count() -> int:
        record["count"].append(True)
        return flow_df.height

    monkeypatch.setattr(cli.config, "load_env_file", load_env)
    monkeypatch.setattr(cli.config, "setup_logging", setup_logging)
    monkeypatch.setattr(
        cli.assets, "ensure_assets_server", _fake_ensure_assets_server(assets_source)
    )
    monkeypatch.setattr(cli.data, "fetch_dataframe", fetch)
    monkeypatch.setattr(cli.storage, "save_daily", save_daily)
    monkeypatch.setattr(cli.storage, "read_daily", read_daily)
    monkeypatch.setattr(cli.storage, "latest_date", latest)
    monkeypatch.setattr(cli.storage, "count_rows", count)
    monkeypatch.setattr(cli.charts, "render_report", render)
    monkeypatch.setattr(cli.notify, "send_report", send)
    return record


# --------------------------------------------------------------------------- #
# build_parser
# --------------------------------------------------------------------------- #


def test_build_parser_defaults() -> None:
    """默认值: from_db False, no_send False, days 取配置, log_level 为 INFO."""
    args = _parse()
    assert args.from_db is False
    assert args.no_send is False
    assert args.days == config.DEFAULT_PLOT_DAYS == 50
    assert args.log_level == "INFO"


def test_build_parser_parses_all_flags() -> None:
    """全参数解析."""
    args = _parse("--from-db", "--no-send", "--days", "120", "--log-level", "DEBUG")
    assert args.from_db is True
    assert args.no_send is True
    assert args.days == 120
    assert args.log_level == "DEBUG"


def test_build_parser_rejects_unknown_log_level() -> None:
    """非法 log-level 抛 SystemExit(2)."""
    with pytest.raises(SystemExit) as excinfo:
        _parse("--log-level", "TRACE")
    assert excinfo.value.code == 2


def test_build_parser_rejects_non_integer_days() -> None:
    """--days 传非整数抛 SystemExit(2)."""
    with pytest.raises(SystemExit) as excinfo:
        _parse("--days", "abc")
    assert excinfo.value.code == 2


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


def test_run_stops_when_assets_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sample_df: pl.DataFrame
) -> None:
    """资源检查失败时返回 1, 且不抓数, 不落库, 不读库, 不推送."""
    record = _stub_pipeline(
        monkeypatch, flow_df=sample_df, png_path=tmp_path / "r.png", assets_source=None
    )

    exit_code = cli.run(_parse("--no-send"))

    assert exit_code == cli.EXIT_ASSETS_UNAVAILABLE == 1
    assert record["log"] == [logging.INFO]
    assert record["fetch"] == []
    assert record["save"] == []
    assert record["read"] == []
    assert record["render"] == []
    assert record["send"] == []


def test_run_fetch_save_draw_and_send(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sample_df: pl.DataFrame
) -> None:
    """正常抓数流程: 落库的是抓取结果, 绘图用读库结果, 推送用最新日期."""
    record = _stub_pipeline(monkeypatch, flow_df=sample_df, png_path=tmp_path / "r.png")

    exit_code = cli.run(_parse())

    assert exit_code == cli.EXIT_OK == 0
    assert record["env"] == [True]
    assert record["save"] == [sample_df]
    assert record["save"][0] is sample_df
    assert record["read"] == [config.DEFAULT_PLOT_DAYS]
    assert record["render"] == [sample_df]
    assert record["render"][0] is sample_df
    assert record["send"] == [cli.data.latest_date(sample_df)]
    assert record["send"] == [date(2026, 9, 30)]
    # 抓数分支会读一次诊断用的最新日期与总行数
    assert record["latest"] == [True]
    assert record["count"] == [True]


def test_run_passes_explicit_days_to_read_daily(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sample_df: pl.DataFrame
) -> None:
    """显式 --days 120 会原样传给 read_daily."""
    record = _stub_pipeline(monkeypatch, flow_df=sample_df, png_path=tmp_path / "r.png")

    assert cli.run(_parse("--days", "120")) == cli.EXIT_OK
    assert record["read"] == [120]


def test_run_no_send_skips_notify(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sample_df: pl.DataFrame
) -> None:
    """--no-send 时返回 0 且不推送, 但仍然抓数, 落库, 绘图."""
    record = _stub_pipeline(monkeypatch, flow_df=sample_df, png_path=tmp_path / "r.png")

    exit_code = cli.run(_parse("--no-send"))

    assert exit_code == cli.EXIT_OK
    assert record["send"] == []
    assert record["save"] == [sample_df]
    assert record["read"] == [config.DEFAULT_PLOT_DAYS]
    assert record["render"] == [sample_df]


def test_run_from_db_skips_fetch_and_save(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sample_df: pl.DataFrame
) -> None:
    """--from-db 时跳过抓数与落库, 直接从库读回绘图."""
    record = _stub_pipeline(monkeypatch, flow_df=sample_df, png_path=tmp_path / "r.png")

    exit_code = cli.run(_parse("--from-db"))

    assert exit_code == cli.EXIT_OK
    assert record["fetch"] == []
    assert record["save"] == []
    assert record["latest"] == []
    assert record["count"] == []
    assert record["read"] == [config.DEFAULT_PLOT_DAYS]
    assert record["render"] == [sample_df]
    assert record["send"] == [date(2026, 9, 30)]


def test_run_returns_data_missing_when_db_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sample_df: pl.DataFrame
) -> None:
    """read_daily 返回 None 时返回 2, 且不渲染, 不推送."""
    record = _stub_pipeline(monkeypatch, flow_df=sample_df, png_path=tmp_path / "r.png")
    monkeypatch.setattr(
        cli.storage,
        "read_daily",
        lambda days: (record["read"].append(days), None)[1],
    )

    exit_code = cli.run(_parse("--from-db"))

    assert exit_code == cli.EXIT_DATA_MISSING == 2
    assert record["render"] == []
    assert record["send"] == []


def test_run_configures_log_level_and_loads_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sample_df: pl.DataFrame
) -> None:
    """日志级别传成整数, 且 load_env_file 恰好被调用一次."""
    record = _stub_pipeline(monkeypatch, flow_df=sample_df, png_path=tmp_path / "r.png")

    exit_code = cli.run(_parse("--no-send", "--log-level", "DEBUG"))

    assert exit_code == cli.EXIT_OK
    assert record["log"] == [logging.DEBUG]
    assert record["log"] == [10]
    assert isinstance(record["log"][0], int)
    assert record["env"] == [True]
    assert len(record["env"]) == 1


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def test_main_equals_run_with_parsed_args(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sample_df: pl.DataFrame
) -> None:
    """main(argv) 等价于 run(parse_args(argv)), 并透传 days."""
    record = _stub_pipeline(monkeypatch, flow_df=sample_df, png_path=tmp_path / "r.png")
    argv = ["--no-send", "--days", "7"]

    expected = cli.run(cli.build_parser().parse_args(argv))
    actual = cli.main(argv)

    assert expected == actual == cli.EXIT_OK == 0
    assert record["read"] == [7, 7]
    assert record["send"] == []


# --------------------------------------------------------------------------- #
# 端到端: 真库 + 真实 storage
# --------------------------------------------------------------------------- #


def test_run_reads_real_sqlite_history(
    monkeypatch: pytest.MonkeyPatch,
    seeded_db: Path,
    tmp_path: Path,
    sample_df: pl.DataFrame,
) -> None:
    """把 HISTORY_DB_PATH 指向真库, 走完整 run 并断言读到了库里的 3 行."""
    rendered: list[pl.DataFrame] = []
    sent: list[date] = []

    monkeypatch.setattr(cli.config, "HISTORY_DB_PATH", seeded_db)
    monkeypatch.setattr(cli.config, "load_env_file", lambda: False)
    monkeypatch.setattr(cli.config, "setup_logging", lambda level: None)
    monkeypatch.setattr(
        cli.assets, "ensure_assets_server", _fake_ensure_assets_server(assets.SOURCE_EXTERNAL)
    )
    monkeypatch.setattr(
        cli.charts,
        "render_report",
        lambda df: (rendered.append(df), tmp_path / "r.png")[1],
    )
    monkeypatch.setattr(cli.notify, "send_report", sent.append)

    exit_code = cli.run(_parse("--from-db"))

    assert exit_code == cli.EXIT_OK
    assert len(rendered) == 1
    history = rendered[0]
    assert history.height == 3
    assert history["日期"].to_list() == [
        date(2026, 9, 28),
        date(2026, 9, 29),
        date(2026, 9, 30),
    ]
    assert list(history.columns) == list(cli.data.OUTPUT_COLUMNS)
    assert history["融资余额"][-1] == 2_500_000_000_000
    assert sent == [date(2026, 9, 30)]
