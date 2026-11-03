# 架构说明

一句话：**数据向下流，口径向上收**。任何一条新需求，先判断它属于哪一层，再决定改哪个文件。

## 1. 分层与依赖方向

```
┌──────────────────────────────────────────────────────────────┐
│ 界面层    web/（控制台）   mobile/（App）   android/（打包）  │
└───────────────▲──────────────────────────────┬───────────────┘
                │ HTTP/JSON                    │ 静态资源
┌───────────────┴──────────────────────────────▼───────────────┐
│ 接入层    server.py（API + 任务调度）  scripts/（CLI 脚本）  │
└───────────────▲──────────────────────────────┬───────────────┘
                │ 读产物 CSV/JSON              │ 调库
┌───────────────┴──────────────────────────────▼───────────────┐
│ 研究层    overfit / expectation / dual_mom / resid_mom /     │
│           macross / chan / monthly      （只被研究脚本调用）  │
└───────────────▲──────────────────────────────┬───────────────┘
                │                              │
┌───────────────┴──────────────────────────────▼───────────────┐
│ 核心层    pipeline → universe → factors → strategy → backtest│
│           → metrics → report        （纯计算，不碰 IO）       │
└───────────────▲──────────────────────────────────────────────┘
                │
┌───────────────┴──────────────────────────────────────────────┐
│ 基础设施  datafeed（腾讯行情） io_utils（原子写） rebalance   │
└──────────────────────────────────────────────────────────────┘
```

**规则**：箭头只能向下调用。核心层不许 import `server.py`；研究层不许被核心层 import；
脚本负责"读文件 → 调库 → 写文件"，不把业务逻辑写在脚本里（这样测试才能覆盖）。

> 这条规则**不是君子协定**：`python -m etfquant audit` 的 L 节会用 AST 扫描实际 import，
> 违反即 FAIL；新增模块如果没在下表登记，同样 FAIL。改结构时先改表，再改代码。

## 1.5 模块地图

三层归属决定"改这里会不会影响日常刷新/回测"：

| 层 | 含义 | 含义之外的代价 |
|---|---|---|
| **生产** | server 与日常脚本会调用，改动直接影响界面和刷新 | 改错会让界面/信号当天不可用 |
| **研究** | 只有 `scripts/research/` 调用，结论已写进 README 第七章 | 可以放心重写，不影响日常 |
| **工具** | 验证与报告（体检/月报/诊断），按需运行 | 改动只影响对应的检查产出 |

| 模块 | 层 | 职责 | 调用方 |
|---|---|---|---|
| `datafeed` | 生产 | 行情抓取（腾讯双主机轮换 + 当日覆盖度门 + 原子缓存） | pipeline、scripts |
| `universe` | 生产 | 标的池：全市场扫描、分类、流动性过滤 | pipeline、scripts |
| `pipeline` | 生产 | 面板组装（缓存 → Panel），刷新链的入口 | scripts、server |
| `factors` | 生产 | 因子计算：动量、波动、趋势、拥挤度、广度 | strategy |
| `strategy` | 生产 | 四策略：S1 动量基线 / S2 双动量 / S3 多因子 / S4 TGAR | pipeline、backtest |
| `backtest` | 生产 | 事件驱动回测：成本、换手、逐日净值 | scripts、track |
| `metrics` | 生产 | 绩效指标（CAGR 用日历天数，波动/夏普按 252） | 全部报告类 |
| `report` | 生产 | HTML 研究报告生成（内联 SVG） | scripts |
| `holdings` | 生产 | 实盘持仓：校验、整手建仓、浮动盈亏 | server |
| `track` | 生产 | 实盘跟踪：信号归档 → 纸上净值 | server、scripts |
| `expectation` | 生产 | 对外口径换算（样本内 → 9%~11%） | server、scripts、report |
| `risk` | 生产 | 风险预算：目标权重整体缩放（默认 1.0，环境变量/参数开启） | run_signal、run_backtest |
| `combo` | 生产 | 核心-卫星：红利低波核心（默认 50%）+ 动量卫星，目标权重混合 | run_signal、track、combo_report |
| `combo` | 生产 | 核心-卫星：红利低波核心（默认 50%）+ 动量卫星，目标权重混合 | run_signal、track、combo_report |
| `secondary` | 生产 | 第二排序键（默认关闭，负结果留档） | strategy |
| `rebalance` | 生产 | 调仓日历（解释清单为何不每天变） | server |
| `io_utils` | 生产 | 原子写（CSV/文本） | 全部落盘处 |
| `cli` | 生产 | 命令行入口（`python -m etfquant <cmd>`） | 人 |
| `__main__` | 生产 | `python -m etfquant` 的引导（转 cli） | 人 |
| `diagnostics` | 工具 | 深度诊断：因子回归（alpha vs mkt/mom/smb）、执行缺口、容量、bootstrap、IC | deep_check、audit |
| `attribution` | 工具 | 会计式收益拆解：Beta / 择时 / 选股 / 债券 / 成本，依赖 `BacktestResult.daily` | attribution 脚本 |
| `overfit` | 工具 | 过拟合体检：PBO / CSCV / 滚动前向 | overfit_check、dsr |
| `monthly` | 工具 | 前向验证月报（对着预注册停止条件打分） | monthly_report |
| `placebo` | 研究 | 随机对照：同候选集/敞口/费率的安慰剂策略 | research |
| `chan` | 研究 | 缠论笔交易（含未来函数自查，负结果） | research |
| `dual_mom` | 研究 | 双动量（绝对 + 相对） | research |
| `resid_mom` | 研究 | 残差动量（负结果） | research |
| `macross` | 研究 | 均线金叉死叉波段（负结果） | research |
| `meta` | 研究 | 元标签：模型决定仓位（负结果） | research |
| `shares` | 研究 | ETF 份额（申赎）采集 | research |
| `alpha_track` | 研究 | alpha 随时间衰减的跟踪 | research |
| `slow_gate` | 研究 | 慢变量仓位上限：股债相对强弱 5 年分位（试验 7.17） | research |

## 2. 数据流（一次更新的完整路径）

```
腾讯行情 ──download_history.py──▶ data/cache/kline_qfq/*.csv
                                        │
                            pipeline.load_panel()
                                        ▼
                                  Panel(close/open/high/low/volume/amount)
                                        │
                     strategy.TrendGatedAdaptive.generate()
                                        ▼
                              {调仓日: {代码: 权重}}
                                        │
                                backtest.Backtester
                                        ▼
              output/nav_main.csv · perf_main.csv · weights_*.csv · trades_*.csv
                       │                              │
        server.py / make_report.py / sync_readme.py    └── run_signal.py ──▶ output/signal_*.csv
                       │                                              └──▶ data/signal_archive.csv
                       ▼
                web/ · output/ETF量化策略研究报告.html · 手机 data.js
```

**下载这一步有两道时间门槛**：`drop_unfinished_today`（盘中那根未完成的 bar 直接丢）与 `strip_partial_day`（当日只落库不到 80% 就整天丢弃，并把已写入的回写裁掉）。两道都是「宁可少一天，也不要半天」——半天数据会让 `build_price_panel` 对其余标的 ffill，横截面排名与波动率全错且不报错。

## 3. 五个"只允许有一个实现"的地方

| 概念 | 唯一实现 | 为什么不能有第二个 |
|---|---|---|
| 调仓日期 | `etfquant/rebalance.py` | 界面/信号/归档各算一遍会得出不同日期，实测导致 −0.39pp 偏差 |
| 收盘判定 | `datafeed.is_market_closed` | 语义是"当日K线是否定型"，写错会让早盘更新变成 no-op |
| 对外口径 | `etfquant/expectation.py` | 五处显示、一处换算，否则 README 与界面互相矛盾 |
| 横截面 z 值 | `factors.zscore_cs(winsorize_cs(x))` | 缩尾范围不一致会让两个因子不可加 |
| 排名 tie-break | 分数降序 → 原始 R20 降序 | quicksort 对并列不稳定，同一份数据会选出不同标的 |
| 当日完整性 | `datafeed.strip_partial_day` | 半天的日线会被面板 ffill，收益与波动全错，且没有任何报错 |

## 4. 扩展指南

**加一个因子**：`factors.py` 里写纯函数 → `strategy.FactorBank` 预计算并缓存 →
`strategy.TrendGatedAdaptive` 加一个 `score_mode` 分支（默认关闭）→
用 `scripts/research/ab_resid.py` 的模板做 A/B（同起点、两段子区间、邻域扫描）。

**加一个策略**：继承 `strategy.RotationBase`，只实现 `target_weights()`；在
`STRATEGY_REGISTRY` 注册；`scripts/run_backtest.py` 里加入对比列表。

**加一个页面**：`web/app.js` 里 `renderXxx()` + `NAV` 配置；数据从 `server.py` 新增的
`api_xxx()` 取；**不要在前端算口径类数字**。

**加一个外部策略对照**：复制 `scripts/research/ab_macross.py`；先读 `docs/strategy_dd.md`
的五道闸门；产物写 `output/experiments/`，结论进 README 第七章。

## 5. 目录约定

- `output/` 顶层 = **应用依赖的产物**（删了界面会坏）；`output/experiments/` = 研究产物（可随时删）。
  契约见 `output/README.md`。
- `scripts/` 只放日常流程；一次性实验放 `scripts/research/`，并保证可以被 `python -m etfquant research <name>` 调用。
- `docs/` 放"给人和 Agent 看的东西"：本文、`strategy_dd.md`（尽调清单）、`code_review.md`。
- 命名：模块名说明"算什么"，脚本名说明"做什么"，产物名与脚本名一致（`ab_momvol.py → output/experiments/ab_momvol.csv`）。
