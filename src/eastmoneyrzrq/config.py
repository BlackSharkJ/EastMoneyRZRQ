"""项目配置: 路径, 接口端点, 静态资源服务与环境变量.

写死的常量集中放在这里, 其他模块只读取常量或调用取配置的函数, 不自己拼路径, 也不自己读环境变量.
"""

import logging
import os
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

# src/eastmoneyrzrq/config.py 的上两级就是项目根目录, 所有产物路径都以它为基准, 避免受工作目录影响
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

HISTORY_DB_PATH: Final[Path] = PROJECT_ROOT / "rzrq.sqlite3"
REPORT_HTML_PATH: Final[Path] = PROJECT_ROOT / "rzrq_report.html"
REPORT_PNG_PATH: Final[Path] = PROJECT_ROOT / "rzrq_report.png"

# 绘图默认只取最近 50 个交易日; 库里可以累积更长的历史, 用 --days 放开
DEFAULT_PLOT_DAYS: Final[int] = 50

# 东方财富数据中心: 融资融券历史汇总
EASTMONEY_ENDPOINT: Final[str] = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EASTMONEY_REPORT_NAME: Final[str] = "RPTA_RZRQ_LSHJ"
EASTMONEY_JSONP_CALLBACK: Final[str] = "datatable2595798"
EASTMONEY_PAGE_SIZE: Final[int] = 50
EASTMONEY_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT: Final[float] = 10.0

# pyecharts 静态资源服务: 端口与目录必须同时可用, 否则渲染出的 HTML 缺少 JS, 截图会是空图.
# 这个服务通常由外部进程提供 (例如 QMTStrategy/xtquant_trader), 本项目默认只负责探测;
# 允许自举时, 探测失败会在同一个端口上临时起一个静态服务, 跑完自动关掉.
ASSETS_URL_PREFIX: Final[str] = "pyecharts_assets"
ASSETS_VERSION_DIR: Final[str] = "v5"
ASSETS_PATH: Final[str] = f"{ASSETS_URL_PREFIX}/{ASSETS_VERSION_DIR}/"
# 用一个真实存在的资源文件当探针, 既能验证端口存活, 也能验证目录存在
ASSETS_PROBE_FILE: Final[str] = "echarts.min.js"
ASSETS_HOST_ENV: Final[str] = "EASTMONEYRZRQ_ASSETS_HOST"
ASSETS_PORT_ENV: Final[str] = "EASTMONEYRZRQ_ASSETS_PORT"
ASSETS_DIR_ENV: Final[str] = "EASTMONEYRZRQ_ASSETS_DIR"
ALLOW_TEMP_ASSETS_SERVER_ENV: Final[str] = "EASTMONEYRZRQ_ALLOW_TEMP_ASSETS_SERVER"
DEFAULT_ASSETS_HOST: Final[str] = "127.0.0.1"
DEFAULT_ASSETS_PORT: Final[int] = 8888
# 默认允许自举: 忘了启动外部服务时不该直接失败, 但能用环境变量关掉
DEFAULT_ALLOW_TEMP_ASSETS_SERVER: Final[bool] = True
# 自举服务在本地目录里找资源, 按顺序取第一个命中的
ASSETS_DIR_NAME: Final[str] = "pyecharts-assets"
ASSETS_DIR_SUBDIR: Final[str] = "assets"

# 企业微信机器人: 只从环境变量或 .env 读取 key, 不允许写进代码
WECHAT_WEBHOOK_KEY_ENV: Final[str] = "WECHAT_WEBHOOK_KEY"
WECHAT_SEND_URL: Final[str] = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
WECHAT_UPLOAD_URL: Final[str] = "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media"
WECHAT_TIMEOUT: Final[float] = 10.0
WECHAT_MENTIONED_LIST: Final[tuple[str, ...]] = ("longjunfan", "lijing")

ENV_FILE_PATH: Final[Path] = PROJECT_ROOT / ".env"
LOG_FORMAT: Final[str] = "%(asctime)s | %(levelname)s | %(funcName)s | %(message)s"
LOG_DATE_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"


def assets_host() -> str:
    """读取 pyecharts 静态资源服务的主机名.

    Returns
    -------
    str
        环境变量 EASTMONEYRZRQ_ASSETS_HOST 的值, 未设置时返回 127.0.0.1.
    """
    return os.environ.get(ASSETS_HOST_ENV, DEFAULT_ASSETS_HOST)


def assets_port() -> int:
    """读取 pyecharts 静态资源服务的端口.

    Returns
    -------
    int
        环境变量 EASTMONEYRZRQ_ASSETS_PORT 的值, 未设置时返回 8888.

    Raises
    ------
    ValueError
        环境变量的值不是合法端口号时抛出.
    """
    raw_port = os.environ.get(ASSETS_PORT_ENV, str(DEFAULT_ASSETS_PORT))
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError(f"{ASSETS_PORT_ENV} 必须是整数, 当前为 {raw_port!r}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"{ASSETS_PORT_ENV} 必须在 1~65535 之间, 当前为 {port}")
    return port


def assets_base_url() -> str:
    """拼出 pyecharts 静态资源目录的基础 URL.

    Returns
    -------
    str
        形如 http://127.0.0.1:8888/pyecharts_assets/v5/ 的地址, 末尾带斜杠.
    """
    return f"http://{assets_host()}:{assets_port()}/{ASSETS_PATH}"


def assets_probe_url() -> str:
    """拼出用于探活的静态资源文件 URL.

    Returns
    -------
    str
        真实存在的资源文件地址, HTTP 200 才能证明端口存活且目录存在.
    """
    return f"{assets_base_url()}{ASSETS_PROBE_FILE}"


def asset_dir_candidates() -> tuple[Path, ...]:
    """列出可能放着 pyecharts 静态资源的本地目录.

    Returns
    -------
    tuple[Path, ...]
        按优先级排列的候选目录, 每个都指向包含 ``v5/`` 的那一层.

    Notes
    -----
    除了仓库内, 也看一下同级目录: 常见的做法是把 pyecharts-assets 克隆在项目旁边.
    """
    return (
        PROJECT_ROOT / ASSETS_DIR_NAME / ASSETS_DIR_SUBDIR,
        PROJECT_ROOT.parent / ASSETS_DIR_NAME / ASSETS_DIR_SUBDIR,
    )


def is_assets_dir(path: Path) -> bool:
    """判断目录里是否真有探针文件.

    Parameters
    ----------
    path : Path
        候选目录.

    Returns
    -------
    bool
        目录下有 ``v5/echarts.min.js`` 时返回 True.
    """
    return (path / ASSETS_VERSION_DIR / ASSETS_PROBE_FILE).is_file()


def assets_dir() -> Path | None:
    """找出可用的 pyecharts 静态资源目录.

    Returns
    -------
    Path | None
        环境变量指定的目录 (优先), 否则是第一个含探针文件的候选目录;
        都没有时返回 None.

    Raises
    ------
    ValueError
        环境变量指向的目录不存在或里面没有资源文件时抛出.
    """
    override = os.environ.get(ASSETS_DIR_ENV, "").strip()
    if override:
        path = Path(override).expanduser()
        if not is_assets_dir(path):
            raise ValueError(
                f"{ASSETS_DIR_ENV} 指向的目录里没有 "
                f"{ASSETS_VERSION_DIR}/{ASSETS_PROBE_FILE}: {path}"
            )
        return path
    for candidate in asset_dir_candidates():
        if is_assets_dir(candidate):
            return candidate
    return None


def allow_temporary_assets_server() -> bool:
    """读取「允许临时自举静态资源服务」开关.

    Returns
    -------
    bool
        环境变量没设置时返回 DEFAULT_ALLOW_TEMP_ASSETS_SERVER.

    Raises
    ------
    ValueError
        环境变量的值不是能识别的布尔写法时抛出.
    """
    raw = os.environ.get(ALLOW_TEMP_ASSETS_SERVER_ENV, "").strip().lower()
    if not raw:
        return DEFAULT_ALLOW_TEMP_ASSETS_SERVER
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{ALLOW_TEMP_ASSETS_SERVER_ENV} 不是合法布尔值: {raw!r}")


def load_env_file() -> bool:
    """加载项目根目录下的 .env 文件.

    .env 不存在时静默跳过, 这样在 CI 或纯环境变量场景下也能正常跑.

    Returns
    -------
    bool
        成功加载了 .env 时返回 True, 文件不存在时返回 False.
    """
    if not ENV_FILE_PATH.is_file():
        return False
    # override=False: 已经存在的环境变量优先, 方便临时用命令行覆盖 .env
    load_dotenv(ENV_FILE_PATH, override=False)
    return True


def get_wechat_webhook_key() -> str:
    """读取企业微信机器人 webhook key.

    Returns
    -------
    str
        去掉首尾空白后的 key.

    Raises
    ------
    RuntimeError
        没有配置 WECHAT_WEBHOOK_KEY 时抛出, 避免消息被发到错误的群.
    """
    key = os.environ.get(WECHAT_WEBHOOK_KEY_ENV, "").strip()
    if not key:
        raise RuntimeError(
            f"未配置 {WECHAT_WEBHOOK_KEY_ENV}: 请在项目根目录的 .env 里填写, 或直接设置同名环境变量"
        )
    return key


def setup_logging(level: int = logging.INFO) -> None:
    """配置全局日志格式.

    Parameters
    ----------
    level : int, default=logging.INFO
        日志级别, 例如 logging.DEBUG 或 logging.WARNING.
    """
    logging.basicConfig(
        level=level,
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
    )
