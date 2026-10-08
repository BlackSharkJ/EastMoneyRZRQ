# 东财两融日报 (EastMoneyRZRQ)

抓取东方财富的融资融券历史数据, 生成一页图表报告, 截图后推送到企业微信机器人.

## 项目概述

每天收盘后需要看一眼两融数据: 融资余额的绝对水平, 融资买入/偿还/净买额的方向与力度,
融券余额与净卖额的变化. 这个项目把「抓数据 → 画图 → 截图 → 推送」串成一条命令,
产物是一张可以直接发群的 PNG.

数据源是东方财富数据中心的两融历史汇总接口 (`RPTA_RZRQ_LSHJ`). 抓下来的数据按日期写入本地
sqlite3 库长期累积, 绘图默认取最近 50 个交易日.

推荐在每个交易日的早上8点50分后启动此项目, 东方财富每个交易日的早上8点50分更新上一个交易日的融资融券信息.

## 主要特性

- **一条命令跑完**: `Start.bat` 或 `uv run eastmoneyrzrq`, 抓数据与推送一步到位
- **红涨绿跌配色**: 净买额柱状图 (正红负绿) 与净卖额柱状图 (正绿负红), 按 A 股习惯区分方向
- **混合图双 y 轴**: 柱子挂金额轴, 占比线挂百分比轴, 同一张图看清水平与结构
- **静态资源前置检查 + 临时自举**: 先探活端口与资源目录; 端口没起且本地有 pyecharts-assets 时,
  自动在同一个端口临时起一个只读静态服务, 流程结束自动关掉 (可以用环境变量关掉这个行为)
- **只出图不推送**: `--no-send` 用于调整配色时快速看效果
- **sqlite3 长期累积**: 按日期 upsert, 重复运行不会产生重复行, 想画更长周期就加大 `--days`
- **精确数值**: 库里存的是接口原样的整数「元」, 只在绘图时才换算成「亿」, 不做多次舍入

## 快速开始

### 前置要求

- **Python 3.13** — 见 `.python-version`
- **uv** — 包管理器, 安装方式见 https://docs.astral.sh/uv/
- **[pyecharts 静态资源](https://github.com/pyecharts/pyecharts-assets.git)** — 默认连 `127.0.0.1:8888` 上的 `/pyecharts_assets/v5/`, 该服务通常由外部进程
  (例如 QMTStrategy/xtquant_trader) 提供. 端口没起时, 程序会尝试在同一个端口临时自举一个只读静态服务,
  资源目录按 `EASTMONEYRZRQ_ASSETS_DIR` → `./pyecharts-assets/assets` → `../pyecharts-assets/assets`
  的顺序查找; 找不到就报错退出. 地址用 `EASTMONEYRZRQ_ASSETS_HOST` / `EASTMONEYRZRQ_ASSETS_PORT`
  覆盖, 自举行为用 `EASTMONEYRZRQ_ALLOW_TEMP_ASSETS_SERVER=0` 关掉

### 安装

```bash
git clone <仓库地址>
cd EastMoneyRZRQ

# Windows: 一键对齐环境 (依赖 + chromium + pre-commit 钩子)
pwsh -File scripts/setup_tools.ps1

# 或者手动
uv sync
uv run playwright install chromium
```

### 配置密钥

企业微信机器人的 webhook key 不写在代码里, 只从环境变量或 `.env` 读取:

```bash
cp .env.example .env
# 编辑 .env, 填入 WECHAT_WEBHOOK_KEY (群机器人 -> 添加 -> 获取 Webhook 地址里 key= 后面那段)
```

`.env` 已在 `.gitignore` 里, 不会进版本库. 未配置 key 时程序会报错退出, 不会把消息发到别处.

### 运行

```bash
# Windows 双击即可, 等价于下面第一条命令
Start.bat

# 或者用 uv
uv run eastmoneyrzrq                 # 抓数据 + 落库 + 出图 + 推送
uv run eastmoneyrzrq --no-send       # 只出图, 不推送 (调配色时用这个)
uv run eastmoneyrzrq --from-db       # 跳过抓取, 只用库里的数据重画
uv run eastmoneyrzrq --days 250      # 画最近 250 个交易日
uv run eastmoneyrzrq --log-level DEBUG
```

## 项目结构

```
EastMoneyRZRQ/
├── src/eastmoneyrzrq/
│   ├── __init__.py
│   ├── __main__.py       # python -m eastmoneyrzrq 入口
│   ├── config.py         # 路径, 端点, 环境变量, 日志配置
│   ├── data.py           # 抓取 + JSONP 解析
│   ├── storage.py        # sqlite3 建表 / upsert / 按日期读
│   ├── charts.py         # 三张图 + 探活 + 整页截图
│   ├── notify.py         # 企业微信推送
│   └── cli.py            # 参数解析与流程编排
├── tests/
│   ├── conftest.py       # 假数据, MockTransport, 本地监听口
│   └── unit/             # 单元测试与属性测试
├── scripts/
│   └── setup_tools.ps1   # 一键对齐环境
├── docs/
│   └── architecture.md   # 数据流, 模块职责, 图表约定
├── pyproject.toml
├── .pre-commit-config.yaml
├── .env.example
├── Start.bat
└── uv.lock
```

## 产物

| 文件 | 说明 | 是否入库 |
|---|---|---|
| `rzrq_report.png` | 推送用的整页截图 | 否 |
| `rzrq_report.html` | 中间产物, 可手动打开交互 | 否 |
| `rzrq.sqlite3` | 历史数据累积库, 表 `rzrq_daily`, 一天一行 | 否 |

## 开发指南

```bash
uv sync --all-groups          # 安装含 dev 的全部依赖
uv run ruff format .          # 格式化
uv run ruff check --fix .     # 静态检查
uv run pyright                # 类型检查 (standard 模式)
uv run pytest                 # 全部测试 + 覆盖率门禁 (>= 90%)
uv run pytest tests/unit/test_charts.py -q --no-cov   # 单跑某个文件
uv run pre-commit install     # 提交前自动跑格式化与检查
uv run pre-commit run --all-files
```

### 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 成功, 包括 `--no-send` 只出图的情况 |
| 1 | pyecharts 静态资源服务不可用, 已中止 |
| 2 | 库里没有可用数据 (比如第一次跑就加了 `--from-db`) |

### 提交规范

遵循约定式提交, 用中文书写描述:

- `feat`: 新功能
- `fix`: 修复问题
- `docs`: 文档更新
- `style`: 格式调整
- `refactor`: 重构
- `test`: 测试相关
- `chore`: 构建或工具变动

示例: `feat: 净买额改为红涨绿跌的柱状图`

## 环境变量

| 变量名 | 说明 | 默认值 | 是否必需 |
|---|---|---|---|
| `WECHAT_WEBHOOK_KEY` | 企业微信机器人 key | 无 | 要推送时必需 |
| `EASTMONEYRZRQ_ASSETS_HOST` | pyecharts 静态资源服务主机 | `127.0.0.1` | 否 |
| `EASTMONEYRZRQ_ASSETS_PORT` | pyecharts 静态资源服务端口 | `8888` | 否 |

## 常见问题

**截图是空白图 / 只有标题没有曲线**
静态资源没加载上. 看日志里的 `ensure_assets_server`: `external` 表示外部服务正常,
`temporary` 表示本次是临时自举的, 两者都没有就是资源彻底不可用 (退出码 1).

**端口没起但也不想自举**
设 `EASTMONEYRZRQ_ALLOW_TEMP_ASSETS_SERVER=0`, 此时端口不可用会直接以退出码 1 中止.

**日志说找不到资源目录**
把 pyecharts-assets 克隆到项目旁边 (`../pyecharts-assets/assets`), 或者用
`EASTMONEYRZRQ_ASSETS_DIR` 直接指到那个目录 (该目录下要有 `v5/echarts.min.js`).

**想扩充历史长度**
库里一天只多一行, 所以历史是慢慢攒起来的: 第一次跑就有最近 50 个交易日, 之后每天多一天.
想直接看更长的窗口, 就用 `--days 250`; 库里没有那么多行时, 画出多少算多少.

**数据存在哪**
`rzrq.sqlite3` 是单文件库, 用任何 sqlite 客户端都能打开. 例如:

```bash
uv run python -c "import sqlite3; print(sqlite3.connect('rzrq.sqlite3').execute('SELECT COUNT(*), MAX(日期) FROM rzrq_daily').fetchall())"
```

**`uv run pytest` 单独跑一个文件就红**
`[tool.coverage.report] fail_under = 90` 是全局门禁, 单跑部分文件要加 `--no-cov`.

## 许可证

待定, 尚未选择许可证.

## 联系方式

- 维护者: blashark
- 邮箱: blashark@qq.com
