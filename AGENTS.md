# AGENTS.md — 给 AI Agent 的项目入口

> 一页纸：这是什么、跑什么命令、哪些不能动、哪些坑已经踩过。
> 人类读者：**只看结论** → `docs/summary_cn.md`（一页）；门面 → `README.md`；
> 完整档案 → `docs/README_full.md`（2269 行，带目录）；脚本索引 → `scripts/README.md`。

## 1. 这是什么

A 股 ETF 的量化轮动工具：260 只 ETF 横截面动量选 Top3（TGAR 策略）+ 趋势闸门控仓位。
配 Web 控制台、Android APK、回测/诊断/报告/月报全套流程。

**当前数字（2026-11-02）**

| 口径 | 值 |
|---|---|
| 样本内年化 | **16.99%**（回撤 −28.86%，夏普 0.68） |
| **对外预期** | **11%~13%**（锚点 12.62%，滚动前向样本外） |
| 95% 置信区间 | **[−2.74%, +42.20%]**（块自助法，7.25） |
| 亏钱概率 / 跑不过买指数 | **5.0% / 16.9%** |
| 最长水下 | 3.15 年（2021-09 → 2024-11） |

**一句话定性**：alpha 只在**风格/板块极端分化**的行情出现；普涨行情会跑输；
崩盘里少亏一半。不是全天候——2019-2025 七年超额仅 +2.9pp（t=0.37）。

## 2. 三条命令上手

```bash
.venv/Scripts/python.exe -m etfquant doctor     # 数据截止日 + 绩效 + 口径（先看这个）
.venv/Scripts/python.exe -m etfquant refresh    # 更新行情 → 回测 → 信号（= 界面按钮）
.venv/Scripts/python.exe -m etfquant test       # 271 项单测
```

Windows 双击 `打开界面.bat` 起 Web 控制台（`http://127.0.0.1:8800/`）。
服务是前台进程，窗口关闭即退出——不是 bug。

## 3. 目录地图

| 路径 | 作用 | 谁在用 |
|---|---|---|
| `etfquant/` | 核心库：数据、因子、策略、回测、指标、口径 | 全部 |
| `server.py` | Web API + 任务调度（约 1800 行，单文件） | 桌面端 |
| `web/` `mobile/` `android/` | 控制台 / App 静态页 / APK 工程 | 界面 |
| `scripts/` | 日常流程（23 个，见 `scripts/README.md`） | CLI、界面任务 |
| `scripts/research/` | 一次性实验（23 个），产物进 `output/experiments/` | 只在做对照实验时 |
| `output/` | **产物契约**见 `output/README.md`；顶层是 app 依赖，`experiments/` 可删 | 服务/报告 |
| `data/` | 行情缓存、标的池、归档信号、**净值库** | 全部 |
| `tests/` | 271 项单测 | 自检 |
| `docs/` | 完整档案、架构、尽调清单、自查、一页总结 | 人 + Agent |

## 4. 硬约束（改代码前必读）

1. **落盘必须原子写**：`etfquant/io_utils.atomic_to_csv / atomic_write_text`。直接 `to_csv` 会留半文件。
2. **信号必须确定性**：任何"排名取前 N"都要显式 tie-break（分数降序 → 同分按原始 R20 降序）。
3. **口径只有一个来源**：对外一律 **11%~13%**，16.99% 只能称"样本内"。换算在 `etfquant/expectation.py`，
   数据在 `output/overfit_summary.json`。**禁止在任何地方手写这两个数字。**
4. **结构类信号必须在线计算**（`etfquant/chan.py::online_states`）。全样本端点回放会虚增约 8 倍 CAGR。
5. **改动后必跑**：`test`（271）+ `audit`（32）；改了 `strategy.py` 还要 `verify-engine`（手机端对拍）。
6. **不要在这个仓库里调参**：7.5 的 PBO 43.2% 已判定样本内搜索期望收益为负。只能靠前向。
7. **依赖方向单向**：生产模块不得 import 研究模块；`etfquant` 不得 import `server`。
   新模块要登记 `docs/architecture.md` §1.5，否则 audit 的 L 节 FAIL。

## 5. 数据与外部依赖

- **行情**：腾讯（`qt.gtimg.cn` / `web.ifzq.gtimg.cn`），无 Token。限流：几百次连发会整段 501，
  单线程 + 间隔，别并发轰。
- **ETF 单位净值**：`data/etf_nav_history.csv`（2021-01 起，26 万行），来源**同花顺**
  （`akshare.fund_etf_spot_ths`，`fund.10jqka.com.cn` 可达；东财不可达）。用于算场内溢价（7.21）。
  抓取脚本 `scripts/download_etf_nav.py`，**分批落盘**（每 50 天），单次约 35 分钟。
- **交易日判定**：`etfquant/datafeed.is_market_closed()`（交易日 0:00~9:30 也算已收盘）。
- **缓存**：`data/cache/kline_qfq/<code>.csv`，前复权，列 `date,open,close,high,low,volume`。
- **⚠ 缓存只补尾部，不补头部**：`download_panel` 发现缓存已覆盖到最新日就直接返回，
  **不会回头补更早的历史**。要扩样本必须传 `refresh=True`（7.27 踩过：以为标的没历史，
  实际是当初只从 2018-01 下载）。
- **ETF 份额**：`data/etf_shares.csv`，两交所只给最新一日，历史不可得，别用来回测（7.14）。
- **当日 K 线分批落库**：`PARTIAL_DAY_MIN_COVERAGE = 0.8` 门槛，低于就整天丢弃并让
  `download_history` 以**退出码 3** 结束。看到"更新成功但数字没变"先查这条。

## 6. 常见任务 → 该改哪里

| 想做 | 改这里 |
|---|---|
| 加因子 | `etfquant/factors.py` + `strategy.py::FactorBank`，A/B 用 `scripts/research/` 模板 |
| 加策略 | `etfquant/strategy.py`（继承 `RotationBase`）+ `scripts/run_backtest.py` 注册 |
| 改界面 | `web/app.js`（桌面）/ `mobile/.../app.js`（手机，改完要对拍） |
| 加外部策略对照 | 先查 `docs/github_strategies.md` 是否已测过，再读 `docs/strategy_dd.md` 五道闸门 |
| 扩样本 / 加老标的 | 传 `refresh=True` 重下；新池另存 `data/etf_pool_*.csv`，**别改 `etf_pool.csv`** |
| 改数据源 | `etfquant/datafeed.py`（唯一出口）；新增入口须同厂商（复权因子一致）且登记 `KLINE_HOSTS` |
| 改产物格式 | 先看 `output/README.md` 契约，server 与报告都依赖 |

## 7. 分析陷阱（本周踩过五次，全部是"没查口径"）

写任何结论前，先过这份清单。**每次都是算对了、口径错了。**

| 陷阱 | 症状 | 正确做法 |
|---|---|---|
| 日频快照当成交事件 | 对 `weights_*.csv` 做 diff，权重随价格漂移天天变 → 事件数虚高 5 倍 | 用策略生成的**调仓日 targets**（7.21） |
| 归一化净值当价格 | `nav_main.csv` 的基准列从 1.0 起、2019 起，算 MA200 前 200 天是 NaN → 整年误判 | 用 `strategy._bench_series()`（原始价格、2018 起）（7.22） |
| 小样本当规律 | "10 次里 7 次"看着像规律，二项 p=0.172 | 先做显著性检验，再注意重叠窗口会虚增样本 |
| **负收益区间用夏普** | 亏得少、波动低的策略夏普反而更难看 | **先报三项原始量**（累计/年化、波动、最大回撤），派生指标只作辅助（闸门 6） |
| 算术年化当几何 | 日均×242 与 `(1+累计)^(242/n)−1` 差 2~3pp | 年化一律用几何 |

**规则已写进 `docs/strategy_dd.md` 闸门 6。**

## 8. 当前状态（判断上下文是否过期）

- 数据截止：`python -m etfquant doctor`；
- 归档：20 期（1 期真实 live + 19 期回填），纸上净值与同期回测偏差 0.00pp；
- **试验登记 73 次**（`data/trials.json`），DSR 保守口径 7.5%；
- **271 项单测、32 项审计**全过；
- **实验记录 7.1–7.27**，全部结论见 `docs/README_full.md` 第七章。近期关键：
  - 7.23 执行三约束：开盘成交→17.0%，**收盘成交→3.9%**；容量 100–500 万；0.30%/边失效
  - 7.24 信号衰减：IC 0.017–0.022，**20 日归零、40 日转负**
  - 7.25 置信区间：[−2.74%, +42.20%]，P(亏钱)=5.0%
  - 7.26 HMM 概率化 regime：否，样本外预测方向反了
  - 7.27 扩样本到 2013：**2015 股灾 −29.4% vs 基准 −52.6%**，闸门有效；
    但 2019-2025 仍无 alpha → alpha 依赖"分化行情"
- APK v1.5.0（`dist/` 里的二进制是旧前端，按用户要求不自动打包）
