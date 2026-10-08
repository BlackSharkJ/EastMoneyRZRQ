"""sqlite3 落库: 建表, 按日期 upsert, 按日期范围读取.

设计取舍
--------
- 表名 ``rzrq_daily``, 列名与顺序直接沿用 ``data.OUTPUT_COLUMNS``:
  读出来不用重排, 也不会出现「同一批列两种顺序」的漂移.
- 日期存 ``YYYY-MM-DD`` 文本: sqlite 没有原生日期类型, 这个格式既可直接比较排序, 又可读.
- 金额列存整数元, 不存已经除以 1e8 的「亿」: 接口给的就是整数元, 落库保持原值,
  只到绘图那一步才换算, 避免反复四舍五入. 出现非整数值时直接报错, 不悄悄丢精度.
- 主键是日期 + ``ON CONFLICT DO UPDATE``, 所以同一天重复跑不会产生重复行,
  接口回补旧数据时也能直接覆盖.
- 建表 SQL / INSERT / UPDATE 都由列定义生成, 少一处手写清单就少一处写歪的机会.
- 表结构版本号写在 ``PRAGMA user_version``, 便于以后加列时做迁移判断.
"""

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final

import polars as pl

from . import config, data

logger = logging.getLogger(__name__)

TABLE_NAME: Final[str] = "rzrq_daily"
SCHEMA_VERSION: Final[int] = 1

DATE_COLUMN: Final[str] = data.DATE_COLUMN
RATIO_COLUMN: Final[str] = "融资余额占流通市值比"
WRITTEN_AT_COLUMN: Final[str] = "写入时间"

# 入库列的顺序就是绘图列的顺序, 多出的写入时间放在最后
STORED_COLUMNS: Final[tuple[str, ...]] = (*data.OUTPUT_COLUMNS,)
# 金额列 = 绘图列里去掉日期与占比, 顺序沿用绘图列的顺序
INTEGER_COLUMNS: Final[tuple[str, ...]] = tuple(
    name for name in STORED_COLUMNS if name not in {DATE_COLUMN, RATIO_COLUMN}
)
REAL_COLUMNS: Final[tuple[str, ...]] = (RATIO_COLUMN,)

# 列类型也用同一份定义生成, 保证表结构与上面的清单一一对应
COLUMN_TYPES: Final[dict[str, str]] = {
    DATE_COLUMN: "TEXT PRIMARY KEY",
    RATIO_COLUMN: "REAL",
    WRITTEN_AT_COLUMN: "TEXT NOT NULL",
    **dict.fromkeys(INTEGER_COLUMNS, "INTEGER"),
}

ALL_COLUMNS: Final[tuple[str, ...]] = (*STORED_COLUMNS, WRITTEN_AT_COLUMN)

_DATE_FORMAT: Final[str] = "%Y-%m-%d"


def _quote(name: str) -> str:
    """给列名加双引号, 中文列名在 SQL 里必须包起来."""
    return f'"{name}"'


SCHEMA_SQL: Final[str] = (
    f"CREATE TABLE IF NOT EXISTS {TABLE_NAME} (\n"
    + ",\n".join(f"    {_quote(name)} {COLUMN_TYPES[name]}" for name in ALL_COLUMNS)
    + "\n)"
)

INSERT_SQL: Final[str] = (
    f"INSERT INTO {TABLE_NAME} (\n"
    f"    {', '.join(_quote(name) for name in ALL_COLUMNS)}\n"
    f")\nVALUES ({', '.join('?' for _ in ALL_COLUMNS)})\n"
    f"ON CONFLICT({_quote(DATE_COLUMN)}) DO UPDATE SET\n"
    + ",\n".join(
        f"    {_quote(name)} = excluded.{_quote(name)}"
        for name in (*STORED_COLUMNS[1:], WRITTEN_AT_COLUMN)
    )
)

SELECT_SQL: Final[str] = (
    f"SELECT {', '.join(_quote(name) for name in STORED_COLUMNS)}\n"
    f"FROM {TABLE_NAME}\n"
    f"ORDER BY {_quote(DATE_COLUMN)} DESC\n"
    f"LIMIT ?"
)


def resolve_db_path(db_path: Path | None = None) -> Path:
    """把可选的路径参数解析成实际使用的路径.

    Parameters
    ----------
    db_path : Path | None, default=None
        显式指定的路径, 为 None 时取 config.HISTORY_DB_PATH.

    Returns
    -------
    Path
        实际要使用的数据库文件路径.

    Notes
    -----
    默认值不在函数签名里写死, 否则 config.HISTORY_DB_PATH 会在导入时被固化,
    测试里 monkeypatch 配置就不生效了.
    """
    return config.HISTORY_DB_PATH if db_path is None else db_path


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """打开数据库连接, 用完自动关闭.

    Parameters
    ----------
    db_path : Path | None, default=None
        数据库文件路径, 为 None 时取 config.HISTORY_DB_PATH, 父目录不存在会自动创建.

    Yields
    ------
    sqlite3.Connection
        已开启外键约束与写前日志的连接, 正常退出时自动提交, 抛异常时回滚.
    """
    path = resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        # WAL 模式下读写不互相阻塞, 比默认的 rollback journal 更适合「边写边看」
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def table_exists(connection: sqlite3.Connection) -> bool:
    """判断数据表是否已经建出来.

    Parameters
    ----------
    connection : sqlite3.Connection
        数据库连接.

    Returns
    -------
    bool
        表存在返回 True.
    """
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE_NAME,)
    ).fetchone()
    return row is not None


def get_schema_version(connection: sqlite3.Connection) -> int:
    """读取表结构版本号.

    Parameters
    ----------
    connection : sqlite3.Connection
        数据库连接.

    Returns
    -------
    int
        ``PRAGMA user_version`` 的值, 0 表示还没建过表.
    """
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def init_schema(connection: sqlite3.Connection) -> int:
    """建表并把表结构版本号写到库里.

    Parameters
    ----------
    connection : sqlite3.Connection
        数据库连接.

    Returns
    -------
    int
        建表后的表结构版本号.

    Raises
    ------
    RuntimeError
        库里的版本号比代码还新时抛出, 提示先升级程序而不是让旧代码去写新表.
    """
    current = get_schema_version(connection)
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"数据库表结构版本为 {current}, 高于程序支持的 {SCHEMA_VERSION}, 请先更新程序"
        )
    connection.execute(SCHEMA_SQL)
    if current < SCHEMA_VERSION:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        logger.info(f"已初始化表 {TABLE_NAME}, 表结构版本 {SCHEMA_VERSION}")
    return SCHEMA_VERSION


def _to_date_text(value: Any) -> str:
    """把日期值转成 ``YYYY-MM-DD`` 文本.

    Parameters
    ----------
    value : Any
        datetime.date, datetime.datetime 或可解析的字符串.

    Returns
    -------
    str
        ISO 日期文本.

    Raises
    ------
    ValueError
        值不是日期也无法解析时抛出.
    """
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10]).isoformat()
        except ValueError as exc:
            raise ValueError(f"{DATE_COLUMN} 不是可识别的日期: {value!r}") from exc
    raise ValueError(f"{DATE_COLUMN} 不是可识别的日期: {value!r}")


def _to_money(column: str, value: Any) -> int | None:
    """把金额值转成整数元.

    Parameters
    ----------
    column : str
        列名, 只用于报错信息.
    value : Any
        待转换的金额.

    Returns
    -------
    int | None
        整数元, 原值为 None 时返回 None.

    Raises
    ------
    ValueError
        值不是整数, 或是不支持的类型时抛出.

    Notes
    -----
    不依赖 sqlite 的列亲和性去「顺手」把 2500000000000.0 变成整数: 一旦接口回的是 2.5,
    亲和性会把它原样存成 REAL, 读取时整列 dtype 都会变, 属于悄悄换语义.
    这里显式校验, 宁可报错也不要静默丢精度.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{column} 出现布尔值: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise ValueError(f"{column} 出现非整数金额 {value!r}, 接口语义可能变了")
    raise ValueError(f"{column} 出现不支持的类型: {type(value).__name__}")


def _to_real(column: str, value: Any) -> float | None:
    """把占比值转成浮点数.

    Parameters
    ----------
    column : str
        列名, 只用于报错信息.
    value : Any
        待转换的数值.

    Returns
    -------
    float | None
        浮点值, 原值为 None 时返回 None.

    Raises
    ------
    ValueError
        值不是数值时抛出.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{column} 出现布尔值: {value!r}")
    if isinstance(value, int | float):
        return float(value)
    raise ValueError(f"{column} 出现不支持的类型: {type(value).__name__}")


def _convert_row(row: tuple[Any, ...]) -> tuple[Any, ...]:
    """把一行 DataFrame 数据转成 sqlite 参数.

    Parameters
    ----------
    row : tuple[Any, ...]
        按 STORED_COLUMNS 顺序取出的值.

    Returns
    -------
    tuple[Any, ...]
        类型已规范化的参数元组.
    """
    values: list[Any] = []
    for name, value in zip(STORED_COLUMNS, row, strict=True):
        if name == DATE_COLUMN:
            values.append(_to_date_text(value))
        elif name in INTEGER_COLUMNS:
            values.append(_to_money(name, value))
        else:
            values.append(_to_real(name, value))
    return tuple(values)


def save_daily(df: pl.DataFrame, db_path: Path | None = None) -> int:
    """把 DataFrame 按日期 upsert 进库.

    Parameters
    ----------
    df : pl.DataFrame
        含 STORED_COLUMNS 的行情数据.
    db_path : Path | None, default=None
        数据库文件路径, 为 None 时取 config.HISTORY_DB_PATH.

    Returns
    -------
    int
        本次提交的行数 (含更新已有日期的行).

    Raises
    ------
    ValueError
        DataFrame 缺少入库需要的列, 或列里出现不能落库的值时抛出.
    """
    missing = [name for name in STORED_COLUMNS if name not in df.columns]
    if missing:
        raise ValueError(f"DataFrame 缺少入库需要的列: {missing}")

    path = resolve_db_path(db_path)
    written_at = datetime.now(UTC).astimezone().isoformat(timespec="seconds")
    rows = [(*_convert_row(row), written_at) for row in df.select(list(STORED_COLUMNS)).iter_rows()]

    with connect(path) as connection:
        init_schema(connection)
        connection.executemany(INSERT_SQL, rows)

    logger.info(f"已写入 {TABLE_NAME}: {len(rows)} 行, 共 {count_rows(path)} 行")
    return len(rows)


def read_daily(
    days: int = config.DEFAULT_PLOT_DAYS, db_path: Path | None = None
) -> pl.DataFrame | None:
    """读取最近若干个交易日.

    Parameters
    ----------
    days : int, default=config.DEFAULT_PLOT_DAYS
        取多少行, 按日期倒序取完再翻回升序.
    db_path : Path | None, default=None
        数据库文件路径, 为 None 时取 config.HISTORY_DB_PATH.

    Returns
    -------
    pl.DataFrame | None
        按日期升序排列、列顺序等于 data.OUTPUT_COLUMNS 的 DataFrame;
        库文件不存在、表还没建或一条数据都没有时返回 None.

    Raises
    ------
    ValueError
        days 不是正数时抛出.
    """
    if days <= 0:
        raise ValueError(f"days 必须是正数, 当前为 {days}")
    path = resolve_db_path(db_path)
    if not path.is_file():
        logger.info(f"本地数据库不存在: {path}")
        return None

    with connect(path) as connection:
        if not table_exists(connection):
            logger.info(f"数据表还没建出来: {TABLE_NAME}")
            return None
        rows = connection.execute(SELECT_SQL, (days,)).fetchall()
    if not rows:
        logger.info(f"数据库里还没有数据: {path}")
        return None

    frame = pl.DataFrame(rows, schema=list(STORED_COLUMNS), orient="row")
    frame = frame.with_columns(pl.col(DATE_COLUMN).str.to_date(_DATE_FORMAT))
    return frame.sort(DATE_COLUMN)


def count_rows(db_path: Path | None = None) -> int:
    """统计库里有多少行.

    Parameters
    ----------
    db_path : Path | None, default=None
        数据库文件路径, 为 None 时取 config.HISTORY_DB_PATH.

    Returns
    -------
    int
        行数; 库文件或数据表不存在时返回 0.
    """
    path = resolve_db_path(db_path)
    if not path.is_file():
        return 0
    with connect(path) as connection:
        if not table_exists(connection):
            return 0
        row = connection.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}").fetchone()
    return int(row[0]) if row else 0


def latest_date(db_path: Path | None = None) -> date | None:
    """取库里的最新日期, 用来判断数据是否落后.

    Parameters
    ----------
    db_path : Path | None, default=None
        数据库文件路径, 为 None 时取 config.HISTORY_DB_PATH.

    Returns
    -------
    date | None
        最新日期; 库文件或数据表不存在、表里没有数据时返回 None.
    """
    path = resolve_db_path(db_path)
    if not path.is_file():
        return None
    with connect(path) as connection:
        if not table_exists(connection):
            return None
        row = connection.execute(f"SELECT MAX({_quote(DATE_COLUMN)}) FROM {TABLE_NAME}").fetchone()
    if not row or row[0] is None:
        return None
    return date.fromisoformat(str(row[0]))
