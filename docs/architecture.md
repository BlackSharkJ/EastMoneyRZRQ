# 架构说明

## 一句话概括

抓东方财富的两融历史数据 → 按日期 upsert 进 sqlite3 → 从库里读最近 N 个交易日 → pyecharts 画三张图拼成一页 → chromium 整页截图 → 推送企业微信机器人.

## 数据流

```
assets.ensure_assets_server()   外部服务探活, 不行就临时自举, 退出时关闭
   │
东财接口 (JSONP)
   │  data.fetch_dataframe()
   ▼
polars DataFrame
   │  storage.save_daily()           按日期 upsert, 幂等
   ▼
rzrq.sqlite3 (表 rzrq_daily)
   │  storage.read_daily(days=50)    默认取最近 50 个交易日
   ▼
polars DataFrame ── charts.render_report()
                       │  build_report_page()
                       │    ├── build_balance_chart()      两融余额 (单折线)
                       │    ├── build_margin_flow_chart()  融资情况 (柱 + 3 折线, 双 y 轴)
                       │    └── build_short_flow_chart()   融券情况 (柱 + 1 折线, 双 y 轴)
                       ▼
                    rzrq_report.html ── snapshot_full_page() ──► rzrq_report.png
                       │
                       └── notify.send_report() ──► 企业微信机器人 (文本 + 图片)
```

数据库是唯一的数据源: 抓下来的数据先落库, 再按 `--days` 从库里读回来绘图. 所以库里攒了多久,
图上就能画多久, 不用改任何抓取逻辑.

## 模块职责

| 模块 | 职责 | 不做什么 |
|---|---|---|
| `config.py` | 路径, 接口端点, 静态资源地址, 环境变量, 日志配置 | 不发起请求, 不画图 |
| `data.py` | 抓取, 解析 JSONP, 列名映射 | 不落库, 不画图, 不推送 |
| `storage.py` | sqlite3 建表 / upsert / 按日期范围读 | 不碰网络, 不画图 |
| `assets.py` | 静态资源探活, 临时自举本地静态服务并保证退出时关闭 | 不画图 |
| `charts.py` | 图表构建, 整页截图 | 不抓数据, 不推送 |
| `notify.py` | 企业微信文本/图片/文件消息 | 不画图 |
| `cli.py` | 参数解析与流程编排, 退出码 | 不放业务细节 |

## 数据库约定

表 `rzrq_daily`, 一天一行, 主键是日期:

```sql
CREATE TABLE IF NOT EXISTS rzrq_daily (
    "日期" TEXT PRIMARY KEY,        -- YYYY-MM-DD
    "融资余额" INTEGER,             -- 单位: 元, 存接口原值
    "融资余额占流通市值比" REAL,
    ... 其余 9 个金额列同为 INTEGER,
    "写入时间" TEXT NOT NULL
);
```

三个取舍:

1. **列名用中文**: 和 DataFrame / 图表 / 日志里的列名完全一致, 读出来直接喂给 `build_*_chart()`,
   不需要再多一层映射, 也少一处会写错的地方.
2. **金额存整数「元」**: 接口给的就是整数元, 落库保持原值. 除以 `1e8` 只在绘图那一步做,
   避免反复四舍五入导致同一份数据在不同环节得到不同的数.
3. **主键 + `ON CONFLICT DO UPDATE`**: 同一天重复运行不会产生重复行, 接口回补历史数据时直接覆盖,
   所以这个流程可以随便重跑.

表结构版本写在 `PRAGMA user_version`. 以后加列要: 提高 `storage.SCHEMA_VERSION`, 在 `init_schema`
里按版本做迁移, 并保留「库版本比代码新就报错」的检查, 免得旧代码写坏新表.

## 三个关键约定

### 1. 混合图必须让 `Bar` 当宿主图

`Chart.overlap()` 只合并 `legend.data` 与 `series` (见 `pyecharts/charts/chart.py`), 被合并那张图的 `yAxis`
会被丢掉. 所以第二条 y 轴一定要用宿主的 `extend_axis()` 建出来:

```python
bar = (
    Bar(...)
    .add_xaxis(x)
    .add_yaxis("净买额", data, yaxis_index=0)
    .extend_axis(yaxis=opts.AxisOpts(name="%", type_="value"))   # 第二条轴挂宿主
)
line = Line().add_xaxis(x).add_yaxis("占比", ratio, yaxis_index=1)  # 只贡献 series
bar.overlap(line)
```

顺序上柱子在先 (画在下层), 折线在后 (画在上层), 这样 50 个柱子的密集区域不会把折线盖住.

### 2. 逐点上色用 `itemStyle`, 不要用 `visualMap`

净买额/净卖额要按正负分色, 做法是每个数据点写 `{"value": v, "itemStyle": {"color": ...}}`.
不用 `VisualMapOpts` 的原因: 它依赖 `dimension` 推断, 而且默认会作用到同一张图的所有系列,
容易把折线一起染色. 系列级 `itemstyle_opts` 只用来让图例色块跟柱子的主色一致.

副作用要留意: 一旦柱子声明了显式颜色, echarts 就不再把它算进调色板, 后面的折线颜色会整体前移一格.

### 3. 静态资源服务可以借, 也可以临时自举

`http://127.0.0.1:8888/pyecharts_assets/v5/` 正常由外部进程提供 (通常来自 QMTStrategy/xtquant_trader).
服务没起来时 pyecharts 生成的 HTML 缺少 echarts.min.js, 截图会是空白. 处理顺序是:

```
ensure_assets_server()
  ├─ check_assets_server()   TCP 通 + HTTP 200 → 用外部服务 (external)
  ├─ 不允许自举 (环境变量关掉) → None → 退出码 1
  ├─ 找不到本地资源目录       → None → 退出码 1
  ├─ 端口被别的进程占着       → None → 退出码 1
  └─ 其余情况 → temporary_assets_server() 起临时服务 → temporary
        └─ 流程结束后 finally 里 shutdown + server_close
```

临时服务用标准库的 `ThreadingHTTPServer`: 绑定 `127.0.0.1` 与配置的端口, 只读本地资源目录,
只响应带 `/pyecharts_assets/` 前缀的具体文件路径 (不列目录, HEAD 同样守前缀), 请求日志降到 debug.
它和外部服务在同一个 URL 下提供同样的文件, 所以 `CurrentConfig.ONLINE_HOST` 不需要变.

## 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 成功 (含 `--no-send` 只出图的情况) |
| 1 | pyecharts 静态资源服务不可用 |
| 2 | 库里没有可用数据 |

## 测试策略

- `tests/unit/test_config.py` 环境变量, 路径拼接, webhook key 校验
- `tests/unit/test_data.py` JSONP 解析, 列映射与排序
- `tests/unit/test_storage.py` 建表与版本号, upsert 幂等, 按天数读取, 整数元不被浮点化
- `tests/unit/test_charts.py` 图表 option 结构 (柱子类型/轴索引/逐点颜色), 截图调用参数
- `tests/unit/test_assets.py` 资源目录解析与环境变量开关, 探活分支, 临时服务的启动/关闭/路由/错误路径
- `tests/unit/test_notify.py` 消息体结构, base64/md5, 上传与发送顺序
- `tests/unit/test_cli.py` 参数解析, 三个退出码路径与副作用, 与真库的接缝
- `tests/unit/test_properties.py` hypothesis 属性: 颜色映射与轴界计算的性质

外部依赖一律拦在边界上: 抓东财的 HTTP 用 `httpx.MockTransport`, 端口探活用真实的本地监听口
(`listening_port`) 与一个确定没人用的口 (`closed_port`), 临时静态服务是真的起在本地端口上并收发 HTTP,
浏览器用 monkeypatch 掉的假 `sync_playwright`. 测试全程不联网, 不开浏览器.

单跑部分测试时要加 `--no-cov`, 否则 `[tool.coverage.report] fail_under = 90` 会把未覆盖的模块算进来:

```bash
uv run pytest tests/unit/test_charts.py -q --no-cov
```

## 加一张新图要改哪里

1. `charts.py` 里写一个 `build_xxx_chart(df) -> Chart`, 需要双轴就按上面的宿主图约定.
2. 加进 `build_report_page()`, 顺序就是页面上的上下顺序.
3. `tests/unit/test_charts.py` 里补 option 结构断言 (系列数量, 轴索引, 颜色).
4. 如果涉及新的数据列, 要同时改 `data.OUTPUT_COLUMNS`, `storage.STORED_COLUMNS` 与建表 SQL,
   否则列会被 `select` 丢掉或根本落不了库. `tests/unit/test_storage.py` 里有一条断言会卡住这个漂移.
