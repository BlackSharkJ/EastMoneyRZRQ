# 东财两融日报 (EastMoneyRZRQ)

抓取东方财富的融资融券历史数据, 生成一页图表报告, 截图后推送到企业微信机器人.

## 项目概述

每天收盘后需要看一眼两融数据: 融资余额的绝对水平, 融资买入/偿还/净买额的方向与力度,
融券余额与净卖额的变化. 这个项目把「抓数据 → 画图 → 截图 → 推送」串成一条命令,
产物是一张可以直接发群的 PNG.

数据源是东方财富数据中心的两融历史汇总接口 (`RPTA_RZRQ_LSHJ`), 默认取最近 50 个交易日.

## 主要特性

- **一条命令跑完**: `Start.bat` 或 `uv run eastmoneyrzrq`, 抓数据与推送一步到位
- **红涨绿跌配色**: 净买额柱状图 (正红负绿) 与净卖额柱状图 (正绿负红), 按 A 股习惯区分方向
- **混合图双 y 轴**: 柱子挂金额轴, 占比线挂百分比轴, 同一张图看清水平与结构
- **静态资源前置检查**: 端口与资源目录都探活通过才继续, 避免推出去一张空白图
- **只出图不推送**: `--no-send` 用于调整配色时快速看效果
- **本地 CSV 缓存**: 每次运行都会写 `两融信息.csv` (带 UTF-8 BOM, Excel 直接打开不乱码)

## 快速开始

### 前置要求

- **Python 3.13** — 见 `.python-version`
- **uv** — 包管理器, 安装方式见 https://docs.astral.sh/uv/
- **pyecharts 静态资源服务** — 监听 `127.0.0.1:8888` 并提供 `/pyecharts_assets/v5/`,
  本项目只消费该服务, 不负责启动. 若换地址, 用 `EASTMONEYRZRQ_ASSETS_HOST` / `EASTMONEYRZRQ_ASSETS_PORT` 覆盖

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
# Windows 双击即可, 等价于下面那条命令
Start.bat

# 或者用 uv
uv run eastmoneyrzrq                 # 抓数据 + 出图 + 推送
uv run eastmoneyrzrq --no-send       # 只出图, 不推送 (调配色时用这个)
uv run eastmoneyrzrq --from-csv      # 用本地 CSV 渲染, 不请求东财接口
uv run eastmoneyrzrq --log-level DEBUG
```

## 项目结构

```
EastMoneyRZRQ/
├── src/eastmoneyrzrq/
│   ├── __init__.py
│   ├── __main__.py       # python -m eastmoneyrzrq 入口
│   ├── config.py         # 路径, 端点, 环境变量, 日志配置
│   ├── data.py           # 抓取 + JSONP 解析 + CSV 读写
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
| `两融信息.csv` | 最近 50 个交易日的历史数据 | 否 |

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
| 2 | 指定了 `--from-csv` 但本地 CSV 不存在 |

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
`127.0.0.1:8888` 上的静态资源服务没起来. 程序现在会直接以退出码 1 中止; 想绕过检查也可以,
但生成的 PNG 不可用.

**Excel 打开 `两融信息.csv` 中文乱码**
不该出现. 写入时用的是 `utf-8-sig` (带 BOM), 如果乱码说明文件被别的工具重写过.

**`uv run pytest` 单独跑一个文件就红**
`[tool.coverage.report] fail_under = 90` 是全局门禁, 单跑部分文件要加 `--no-cov`.

## 许可证

待定, 尚未选择许可证.

## 联系方式

- 维护者: blashark
- 邮箱: blashark@qq.com
