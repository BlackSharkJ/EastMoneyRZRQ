"""storage 模块单元测试: 路径解析, 建表, upsert 落库, 读取与统计.

所有用例都只操作 tmp_path 下的临时库, 绝不触碰项目根目录的 rzrq.sqlite3,
也不访问网络与浏览器.
"""

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from eastmoneyrzrq import config, data, storage

# 每条样例数据对应的整数元金额, 方便断言库里的原值没有被除以 1e8
SAMPLE_DATE = "2026-09-30"
SAMPLE_BALANCE = 2_500_000_000_000
SAMPLE_BUY = 200_000_000_000
SAMPLE_NET_BUY = 5_000_000_000
SAMPLE_RATIO = 2.5


def _days(start: date, count: int) -> list[date]:
    """生成从 start 起连续递增的 count 个日期.

    Parameters
    ----------
    start : date
        起始日期.
    count : int
        天数.

    Returns
    -------
    list[date]
        连续日期列表.
    """
    return [start + timedelta(days=index) for index in range(count)]


def _build_frame(days: list[date], *, offset: int = 0) -> pl.DataFrame:
    """按 data.OUTPUT_COLUMNS 造一批整数元金额的测试数据.

    Parameters
    ----------
    days : list[date]
        日期列表, 长度即行数.
    offset : int, default=0
        在第 0 行基础上给所有金额列加的偏移, 便于区分两批数据.

    Returns
    -------
    pl.DataFrame
        列顺序与 data.OUTPUT_COLUMNS 一致的 DataFrame.
    """
    count = len(days)
    return pl.DataFrame(
        {
            "日期": days,
            "融资余额": [2_500_000_000_000 + offset + index for index in range(count)],
            "融资余额占流通市值比": [SAMPLE_RATIO] * count,
            "融资买入额": [200_000_000_000 + index for index in range(count)],
            "融资偿还额": [195_000_000_000 + index for index in range(count)],
            "融资净买额": [5_000_000_000 + index for index in range(count)],
            "融券余额": [20_000_000_000 + index for index in range(count)],
            "融券余量": [3_000_000_000 + index for index in range(count)],
            "融券偿还量": [80_000_000 + index for index in range(count)],
            "融券卖出量": [90_000_000 + index for index in range(count)],
            "融券净卖额": [10_000_000 + index for index in range(count)],
            "融资融券余额": [2_520_000_000_000 + offset + index for index in range(count)],
        }
    )


# --------------------------------------------------------------------------- #
# 列清单一致性
# --------------------------------------------------------------------------- #


def test_stored_columns_match_output_columns() -> None:
    """Storage 与 data 的列清单必须一致, 防止两处漂移."""
    assert set(storage.STORED_COLUMNS) == set(data.OUTPUT_COLUMNS)
    assert len(storage.STORED_COLUMNS) == len(data.OUTPUT_COLUMNS) == 12
    assert storage.DATE_COLUMN == data.DATE_COLUMN == "日期"


# --------------------------------------------------------------------------- #
# resolve_db_path
# --------------------------------------------------------------------------- #


def test_resolve_db_path_defaults_to_config() -> None:
    """不传路径时取 config.HISTORY_DB_PATH."""
    assert storage.resolve_db_path() == config.HISTORY_DB_PATH
    assert storage.resolve_db_path(None) == config.HISTORY_DB_PATH


def test_resolve_db_path_returns_explicit_path(tmp_path) -> None:
    """显式传入的路径原样返回."""
    explicit = tmp_path / "explicit.sqlite3"
    assert storage.resolve_db_path(explicit) == explicit
    assert storage.resolve_db_path(explicit) is explicit


def test_storage_uses_patched_config_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path, sample_df: pl.DataFrame
) -> None:
    """Monkeypatch 掉 config.HISTORY_DB_PATH 后, 各函数确实用到新路径.

    这正是把默认值写成 None 而不是在签名里写死常量的原因.
    """
    patched = tmp_path / "patched.sqlite3"
    monkeypatch.setattr(config, "HISTORY_DB_PATH", patched)

    assert storage.resolve_db_path() == patched
    assert storage.save_daily(sample_df) == 3
    assert patched.is_file()
    assert storage.count_rows() == 3
    assert storage.latest_date() == date(2026, 9, 30)
    subset = storage.read_daily(2)
    assert subset is not None
    assert subset.height == 2


# --------------------------------------------------------------------------- #
# get_schema_version / init_schema
# --------------------------------------------------------------------------- #


def test_get_schema_version_is_zero_on_new_db(db_path) -> None:
    """刚建出来的空库版本号是 0."""
    with storage.connect(db_path) as connection:
        assert storage.get_schema_version(connection) == 0


def test_init_schema_creates_table_and_sets_version(db_path) -> None:
    """init_schema 建表并把版本写成 SCHEMA_VERSION, 重复调用不报错."""
    with storage.connect(db_path) as connection:
        assert storage.init_schema(connection) == storage.SCHEMA_VERSION == 1
        assert storage.get_schema_version(connection) == storage.SCHEMA_VERSION

        table_names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        ]
        assert storage.TABLE_NAME in table_names

        # 连续 init 两次: 不报错且版本不变
        assert storage.init_schema(connection) == storage.SCHEMA_VERSION
        assert storage.get_schema_version(connection) == storage.SCHEMA_VERSION


def test_init_schema_rejects_newer_db(db_path) -> None:
    """库版本高于代码支持的版本时抛出 RuntimeError, 信息里含两个版本号."""
    with storage.connect(db_path) as connection:
        connection.execute("PRAGMA user_version = 99")

    with storage.connect(db_path) as connection:
        assert storage.get_schema_version(connection) == 99
        with pytest.raises(RuntimeError, match="版本为 99, 高于程序支持的 1"):
            storage.init_schema(connection)


# --------------------------------------------------------------------------- #
# save_daily
# --------------------------------------------------------------------------- #


def test_save_daily_writes_rows_with_sqlite_types(db_path, sample_df: pl.DataFrame) -> None:
    """sample_df 落库后行数为 3, 金额是 INTEGER 原值, 占比是 REAL, 日期是文本."""
    assert storage.save_daily(sample_df, db_path) == 3
    assert storage.count_rows(db_path) == 3

    with sqlite3.connect(db_path) as connection:
        declared = {
            row[1]: row[2] for row in connection.execute(f"PRAGMA table_info({storage.TABLE_NAME})")
        }
        row = connection.execute(
            f"""
            SELECT "融资余额", "融资买入额", "融资净买额",
                   "融资余额占流通市值比", "日期", "{storage.WRITTEN_AT_COLUMN}"
            FROM {storage.TABLE_NAME}
            WHERE "日期" = ?
            """,
            (SAMPLE_DATE,),
        ).fetchone()

    assert declared["融资余额"] == "INTEGER"
    assert declared["融资买入额"] == "INTEGER"
    assert declared["融资净买额"] == "INTEGER"
    assert declared["融资余额占流通市值比"] == "REAL"
    assert declared["日期"] == "TEXT"
    assert declared[storage.WRITTEN_AT_COLUMN] == "TEXT"

    assert row[0] == SAMPLE_BALANCE
    assert isinstance(row[0], int)
    assert row[1] == SAMPLE_BUY
    assert row[2] == SAMPLE_NET_BUY
    assert isinstance(row[2], int)
    assert row[3] == SAMPLE_RATIO
    assert isinstance(row[3], float)
    assert row[4] == SAMPLE_DATE
    assert isinstance(row[4], str)
    assert isinstance(row[5], str)
    assert len(row[5]) >= 19
    assert datetime.fromisoformat(row[5]).tzinfo is not None


def test_save_daily_is_idempotent_and_overwrites(db_path, sample_df: pl.DataFrame) -> None:
    """同一天重复写入不增行, 且新值覆盖旧值, 其他日期不受影响."""
    assert storage.save_daily(sample_df, db_path) == 3
    assert storage.save_daily(sample_df, db_path) == 3
    assert storage.count_rows(db_path) == 3

    dates = sample_df["日期"].to_list()
    values = [int(value) for value in sample_df["融资净买额"].to_list()]
    index = dates.index(date(2026, 9, 29))
    values[index] = 123_456_789
    updated = sample_df.with_columns(pl.Series("融资净买额", values, dtype=pl.Int64))

    assert storage.save_daily(updated, db_path) == 3
    assert storage.count_rows(db_path) == 3

    with storage.connect(db_path) as connection:
        rows = dict(
            connection.execute(
                f'SELECT "日期", "融资净买额" FROM {storage.TABLE_NAME} ORDER BY "日期"'
            ).fetchall()
        )

    assert rows == {
        "2026-09-28": -30_000_000_000,
        "2026-09-29": 123_456_789,
        "2026-09-30": SAMPLE_NET_BUY,
    }
    assert rows["2026-09-29"] == 123_456_789


def test_save_daily_appends_disjoint_dates(db_path) -> None:
    """两批日期不重叠的数据会追加, 总行数变成 60."""
    first = _build_frame(_days(date(2026, 1, 1), 30))
    second = _build_frame(_days(date(2026, 2, 1), 30), offset=1000)

    assert storage.save_daily(first, db_path) == 30
    assert storage.count_rows(db_path) == 30
    assert storage.save_daily(second, db_path) == 30
    assert storage.count_rows(db_path) == 60
    assert storage.latest_date(db_path) == date(2026, 3, 2)


def test_save_daily_rejects_missing_column(db_path, sample_df: pl.DataFrame) -> None:
    """缺少入库列时抛 ValueError, 信息里含列名, 且不落任何数据."""
    broken = sample_df.drop("融资净买额")
    with pytest.raises(ValueError, match="融资净买额"):
        storage.save_daily(broken, db_path)
    assert storage.count_rows(db_path) == 0


# --------------------------------------------------------------------------- #
# read_daily
# --------------------------------------------------------------------------- #


def test_read_daily_returns_latest_rows_in_ascending_order(db_path) -> None:
    """写入 60 天后, days=50 取最新 50 天且日期升序, days=100 取全部."""
    storage.save_daily(_build_frame(_days(date(2026, 1, 1), 60)), db_path)

    latest = storage.read_daily(50, db_path)
    assert latest is not None
    assert latest.height == 50
    assert latest.schema["日期"] == pl.Date
    assert latest["日期"].to_list() == _days(date(2026, 1, 11), 50)
    assert latest["日期"][-1] == date(2026, 3, 1)
    assert latest["日期"][0] == date(2026, 1, 11)

    # 列顺序必须等于 data.OUTPUT_COLUMNS, 而不是库里的物理顺序
    assert list(latest.columns) == list(data.OUTPUT_COLUMNS)
    assert latest["融资余额"][0] == 2_500_000_000_000 + 10
    assert isinstance(latest["融资余额"][0], int)
    assert latest["融资余额占流通市值比"][0] == SAMPLE_RATIO
    assert isinstance(latest["融资余额占流通市值比"][0], float)

    full = storage.read_daily(100, db_path)
    assert full is not None
    assert full.height == 60
    assert full["日期"][0] == date(2026, 1, 1)


def test_read_daily_default_days_uses_config(db_path) -> None:
    """不传 days 时按 config.DEFAULT_PLOT_DAYS 取数."""
    storage.save_daily(_build_frame(_days(date(2026, 1, 1), 60)), db_path)

    result = storage.read_daily(db_path=db_path)
    assert config.DEFAULT_PLOT_DAYS == 50
    defaults = storage.read_daily.__defaults__
    assert defaults is not None
    assert defaults[0] == config.DEFAULT_PLOT_DAYS
    assert result is not None
    assert result.height == config.DEFAULT_PLOT_DAYS
    assert result["日期"][-1] == date(2026, 3, 1)


@pytest.mark.parametrize("days", [0, -1, -50])
def test_read_daily_rejects_non_positive_days(db_path, days: int) -> None:
    """Days 不是正数时抛 ValueError, 信息里含天数."""
    with pytest.raises(ValueError, match=rf"当前为 {days}"):
        storage.read_daily(days, db_path)


def test_read_daily_missing_file_returns_none(db_path) -> None:
    """库文件不存在时返回 None."""
    assert not db_path.exists()
    assert storage.read_daily(5, db_path) is None


def test_read_daily_empty_table_returns_none(db_path) -> None:
    """建了表但没数据时返回 None."""
    with storage.connect(db_path) as connection:
        storage.init_schema(connection)
    assert storage.read_daily(5, db_path) is None


# --------------------------------------------------------------------------- #
# count_rows / latest_date
# --------------------------------------------------------------------------- #


def test_count_rows_missing_file_is_zero(db_path) -> None:
    """库不存在时返回 0."""
    assert storage.count_rows(db_path) == 0


def test_count_rows_returns_written_rows(db_path, sample_df: pl.DataFrame) -> None:
    """写完返回实际行数."""
    assert storage.save_daily(sample_df, db_path) == 3
    assert storage.count_rows(db_path) == 3


def test_latest_date_missing_file_returns_none(db_path) -> None:
    """库不存在时返回 None."""
    assert storage.latest_date(db_path) is None


def test_latest_date_returns_max_date(db_path, sample_df: pl.DataFrame) -> None:
    """有数据时返回最大日期的 datetime.date."""
    storage.save_daily(sample_df, db_path)
    latest = storage.latest_date(db_path)
    assert latest == date(2026, 9, 30)
    assert isinstance(latest, date)


def test_latest_date_empty_table_returns_none(db_path) -> None:
    """空表返回 None."""
    with storage.connect(db_path) as connection:
        storage.init_schema(connection)
    assert storage.latest_date(db_path) is None


# --------------------------------------------------------------------------- #
# connect
# --------------------------------------------------------------------------- #


def test_connect_rolls_back_on_error(db_path) -> None:
    """语句抛异常时回滚, 表里不应留下写一半的数据."""
    with storage.connect(db_path) as connection:
        storage.init_schema(connection)

    with pytest.raises(RuntimeError, match="模拟失败"), storage.connect(db_path) as connection:
        connection.execute(
            f"""
                INSERT INTO {storage.TABLE_NAME} ("日期", "融资余额", "{storage.WRITTEN_AT_COLUMN}")
                VALUES (?, ?, ?)
                """,
            ("2026-01-01", 1, "now"),
        )
        raise RuntimeError("模拟失败")

    assert storage.count_rows(db_path) == 0
    with storage.connect(db_path) as connection:
        remaining = connection.execute(f"SELECT COUNT(*) FROM {storage.TABLE_NAME}").fetchone()
    assert remaining == (0,)


def test_connect_creates_parent_directory(tmp_path) -> None:
    """父目录不存在时自动创建."""
    path = tmp_path / "sub" / "nested" / "x.sqlite3"
    assert not path.parent.exists()

    with storage.connect(path) as connection:
        assert storage.get_schema_version(connection) == 0

    assert path.parent.is_dir()
    assert path.is_file()


def test_connect_closes_connection_after_use(db_path) -> None:
    """退出 with 之后连接已关闭, 再 execute 抛 ProgrammingError."""
    with storage.connect(db_path) as connection:
        assert connection.execute("SELECT 1").fetchone() == (1,)

    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


# --------------------------------------------------------------------------- #
# 值类型校验: 落库不依赖 sqlite 的列亲和性
# --------------------------------------------------------------------------- #


def _day_frame(day: object, **overrides: object) -> pl.DataFrame:
    """造一行数据, 只改需要覆盖的列.

    Parameters
    ----------
    day : object
        日期值, 故意放宽类型以便测试非法输入.
    **overrides : object
        需要覆盖的列值.

    Returns
    -------
    pl.DataFrame
        一行数据的 DataFrame.
    """
    values: dict[str, object] = dict.fromkeys(storage.STORED_COLUMNS, 1)
    values[storage.DATE_COLUMN] = day
    values[storage.RATIO_COLUMN] = 2.5
    values.update(overrides)
    return pl.DataFrame({name: [value] for name, value in values.items()})


def test_save_daily_accepts_integral_float_money(db_path: Path) -> None:
    """整数形式的浮点金额要转成 INTEGER 落库, 而不是靠 sqlite 亲和性顺手转."""
    frame = _day_frame("2026-09-30", 融资余额=2500000000000.0)

    storage.save_daily(frame, db_path)

    with sqlite3.connect(db_path) as connection:
        value, value_type = connection.execute(
            'SELECT "融资余额", typeof("融资余额") FROM rzrq_daily'
        ).fetchone()
    assert value == 2500000000000
    assert value_type == "integer"


def test_save_daily_rejects_fractional_money(db_path: Path) -> None:
    """非整数金额要报错, 不要静默丢精度."""
    frame = _day_frame("2026-09-30", 融资净买额=2.5)

    with pytest.raises(ValueError, match="非整数金额") as excinfo:
        storage.save_daily(frame, db_path)

    assert "融资净买额" in str(excinfo.value)
    assert storage.count_rows(db_path) == 0


def test_save_daily_rejects_bool_and_str_money(db_path: Path) -> None:
    """布尔值和不支持的类型都要报错."""
    with pytest.raises(ValueError, match="布尔值"):
        storage.save_daily(_day_frame("2026-09-30", 融资余额=True), db_path)

    with pytest.raises(ValueError, match="不支持的类型") as excinfo:
        storage.save_daily(_day_frame("2026-09-30", 融资买入额="abc"), db_path)

    assert "融资买入额" in str(excinfo.value)
    assert "str" in str(excinfo.value)


def test_save_daily_keeps_none_as_null(db_path: Path) -> None:
    """缺失值存 NULL, 不要被写成 0."""
    storage.save_daily(_day_frame("2026-09-30", 融券净卖额=None), db_path)

    with sqlite3.connect(db_path) as connection:
        value = connection.execute('SELECT "融券净卖额" FROM rzrq_daily').fetchone()[0]
    assert value is None


def test_save_daily_rejects_bool_and_str_ratio(db_path: Path) -> None:
    """占比列的布尔值与字符串也要报错."""
    with pytest.raises(ValueError, match="布尔值"):
        storage.save_daily(_day_frame("2026-09-30", 融资余额占流通市值比=True), db_path)

    with pytest.raises(ValueError, match="不支持的类型") as excinfo:
        storage.save_daily(_day_frame("2026-09-30", 融资余额占流通市值比="2.5"), db_path)

    assert storage.RATIO_COLUMN in str(excinfo.value)


def test_save_daily_rejects_unparsable_date(db_path: Path) -> None:
    """日期列不是日期也不是可解析字符串时报错."""
    with pytest.raises(ValueError, match="不是可识别的日期"):
        storage.save_daily(_day_frame("2026-09"), db_path)


def test_save_daily_accepts_datetime_and_iso_text(db_path: Path) -> None:
    """日期可以是 datetime 或 ISO 文本, 落库统一成 YYYY-MM-DD."""
    frame = pl.concat(
        [
            _day_frame("2026-09-30 00:00:00", 融资余额=10),
            _day_frame("2026-10-01", 融资余额=20),
        ]
    )
    frame = frame.with_columns(pl.col(storage.DATE_COLUMN).str.to_datetime())

    assert storage.save_daily(frame, db_path) == 2
    with sqlite3.connect(db_path) as connection:
        days = [
            row[0]
            for row in connection.execute(
                f"SELECT {storage._quote(storage.DATE_COLUMN)} FROM rzrq_daily ORDER BY 1"
            )
        ]
    assert days == ["2026-09-30", "2026-10-01"]


def test_read_daily_returns_none_when_table_missing(db_path: Path) -> None:
    """库文件存在但表还没建时, 读数据返回 None 而不是抛 OperationalError."""
    with storage.connect(db_path) as connection:
        assert storage.table_exists(connection) is False

    assert storage.read_daily(db_path=db_path) is None
    assert storage.count_rows(db_path) == 0
    assert storage.latest_date(db_path) is None


def test_latest_date_returns_none_for_empty_table(db_path: Path) -> None:
    """建了表但没有数据时, latest_date 返回 None."""
    with storage.connect(db_path) as connection:
        storage.init_schema(connection)

    assert storage.latest_date(db_path) is None


def test_save_daily_rejects_non_date_value(db_path: Path) -> None:
    """日期列既不是日期也不是字符串时报错."""
    with pytest.raises(ValueError, match="不是可识别的日期") as excinfo:
        storage.save_daily(_day_frame(12345), db_path)

    assert storage.DATE_COLUMN in str(excinfo.value)


def test_save_daily_keeps_none_ratio_as_null(db_path: Path) -> None:
    """占比缺失时存 NULL, 不要被写成 0.0."""
    storage.save_daily(_day_frame("2026-09-30", 融资余额占流通市值比=None), db_path)

    with sqlite3.connect(db_path) as connection:
        value = connection.execute(f'SELECT "{storage.RATIO_COLUMN}" FROM rzrq_daily').fetchone()[0]
    assert value is None
