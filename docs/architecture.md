# 架构说明

## 一句话概括

抓东方财富的两融历史数据 → 用 pyecharts 画三张图拼成一页 → chromium 整页截图 → 推送企业微信机器人.

## 数据流

```
东财接口 (JSONP)
   │  data.fetch_dataframe()
   ▼
polars DataFrame ──┬── data.write_history_csv()  → 两融信息.csv (带 BOM, 供 Excel 看)
                   │
                   └── charts.render_report()
                          │  build_report_page()
                          │    ├── build_balance_chart()      两融余额 (单折线)
                          │    ├── build_margin_flow_chart()  融资情况 (柱 + 3 折线, 双 y 轴)
                          │    └── build_short_flow_chart()   融券情况 (柱 + 1 折线, 双 y 轴)
                          ▼
                       rzrq_report.html ── snapshot_full_page() ──► rzrq_report.png
                          │
                          └── notify.send_report() ──► 企业微信机器人 (文本 + 图片)
```

## 模块职责

| 模块 | 职责 | 不做什么 |
|---|---|---|
| `config.py` | 路径, 接口端点, 静态资源地址, 环境变量, 日志配置 | 不发起请求, 不画图 |
| `data.py` | 抓取, 解析 JSONP, 列名映射, 本地 CSV 读写 | 不画图, 不推送 |
| `charts.py` | 图表构建, 静态资源探活, 整页截图 | 不抓数据, 不推送 |
| `notify.py` | 企业微信文本/图片/文件消息 | 不画图 |
| `cli.py` | 参数解析与流程编排, 退出码 | 不放业务细节 |

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

### 3. 静态资源服务是外部依赖

`http://127.0.0.1:8888/pyecharts_assets/v5/` 不是本项目启动的 (通常来自 QMTStrategy/xtquant_trader).
服务没起来时 pyecharts 生成的 HTML 缺少 echarts.min.js, 截图会是空白. 所以 `cli.run()` 先调
`charts.check_assets_server()` 做 TCP + HTTP 两层探测, 不通过就直接退出, 不抓数据也不推送.

## 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 成功 (含 `--no-send` 只出图的情况) |
| 1 | pyecharts 静态资源服务不可用 |
| 2 | `--from-csv` 但本地 CSV 不存在 |

## 测试策略

- `tests/unit/test_config.py` 环境变量, 路径拼接, webhook key 校验
- `tests/unit/test_data.py` JSONP 解析, 列映射与排序, CSV BOM 往返
- `tests/unit/test_charts.py` 图表 option 结构 (柱子类型/轴索引/逐点颜色), 探活三层分支, 截图调用参数
- `tests/unit/test_notify.py` 消息体结构, base64/md5, 上传与发送顺序
- `tests/unit/test_cli.py` 参数解析, 四个退出码路径与副作用
- `tests/unit/test_properties.py` hypothesis 属性: 颜色映射与轴界计算的性质

外部依赖一律拦在边界上: HTTP 用 `httpx.MockTransport`, 端口探活用真实的本地监听口 (`listening_port`),
浏览器用 monkeypatch 掉的假 `sync_playwright`. 测试全程不联网, 不开浏览器.

单跑部分测试时要加 `--no-cov`, 否则 `[tool.coverage.report] fail_under = 90` 会把未覆盖的模块算进来:

```bash
uv run pytest tests/unit/test_charts.py -q --no-cov
```

## 加一张新图要改哪里

1. `charts.py` 里写一个 `build_xxx_chart(df) -> Chart`, 需要双轴就按上面的宿主图约定.
2. 加进 `build_report_page()`, 顺序就是页面上的上下顺序.
3. `tests/unit/test_charts.py` 里补 option 结构断言 (系列数量, 轴索引, 颜色).
4. 如果涉及新的数据列, 记得同时改 `data.OUTPUT_COLUMNS`, 否则列会被 `select` 丢掉.
