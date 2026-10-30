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
