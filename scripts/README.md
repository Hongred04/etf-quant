# scripts 索引

**约定**：`scripts/` 只放**日常流程**（界面按钮会调、每次都该能跑）。
一次性实验放 `scripts/research/`，产物进 `output/experiments/`。
所有脚本都能用统一入口调用：`python -m etfquant <子命令>`（见下表括号）。

## 一、日常流程（22 个）

| 脚本 | 子命令 | 作用 | 读 | 写 |
|---|---|---|---|---|
| `build_universe.py` | `universe` | 扫描全市场 ETF，落盘标的池 | 腾讯快照 | `data/etf_pool.csv` |
| `download_history.py` | `download` | 历史日线增量更新（限速、断点续传） | 腾讯 K 线 | `data/cache/kline_qfq/*.csv` |
| `run_backtest.py` | `backtest` | 四策略回测 + 绩效汇总 | 缓存、池 | `output/nav_main.csv` 等 |
| `run_signal.py` | `signal` | 出调仓清单（钉在最近调仓日）并归档 | 缓存、策略 | `output/signal_*.csv`、`data/signal_archive.csv` |
| `make_report.py` | `report` | HTML 研究报告 | 回测产物 | `output/ETF量化策略研究报告.html` |
| `rebalance_status.py` | `status` | 今天该不该动手、下次哪天换（可 `--json`/`--check`） | 归档 | 终端 |
| `monthly_report.py` | `monthly` | 前向验证月报（含 alpha 残差跟踪与误差棒） | 归档、纸上净值、`diagnostics.json` | `output/reports/monthly_*.md` |
| `audit.py` | `audit` | 32 项口径自检（记账、成本、日期、JSON 合法、三端一致） | 全部产物 | 终端 |
| `test_runner.py` | `test` | 单元测试（276 项） | `tests/` | 终端 |
| `export_mobile_data.py` | `mobile-data` | 导出 App 的 `data.js` | `server.build_mobile_payload` | `mobile/app/assets/www/data.js` |
| `verify_mobile_engine.py` | `verify-engine` | 手机端 JS 与 Python 引擎逐点对拍 | 缓存、`engine.js` | 终端 |
| `backfill_archive.py` | `backfill` | 回填历史归档（`source=backfill`） | 缓存、策略 | `data/signal_archive.csv` |
| `sync_readme.py` | `sync-readme` | 把 README 数字同步为最新产物 | 产物 CSV | `README.md` |
| `overfit_check.py` | `overfit` | PBO / DSR / 滚动前向（慢，约 5 分钟） | 缓存 | `output/overfit_*` |
| `ablation.py` | — | 模块消融实验 | 缓存 | `output/ablation.csv`（界面"效果验证"页要用） |
| `deep_check.py` | `deep` | 深度诊断：IC / 因子归因 / 执行缺口 / 容量 / 误差棒（`--sweep` 加集中度扫描） | 缓存、回测产物 | `output/diagnostics.json` |
| `collect_shares.py` | `shares` | 采集 ETF 份额日快照（两交所均只给最新一日） | 交易所 API | `data/etf_shares.csv` |
| `download_etf_nav.py` | — | **下载 ETF 历史单位净值**（同花顺源，用于算场内溢价；约 35 分钟，分批落盘） | 同花顺基金中心 | `data/etf_nav_history.csv` |
| `dsr.py` | `dsr` | 按 `data/trials.json` 重算 Deflated Sharpe | `nav_main.csv`、`overfit_grid.csv` | `output/dsr.json` |
| `manifest.py` | `manifest` | 生成复现清单（输入/输出 SHA256） | `data/`、`output/` | `output/manifest.json` |
| `grid2.py` | — | 增量归因 + 子样本 | 缓存 | `output/ladder.csv`、`subsample2.csv`（界面要用） |

> `ablation.py` 和 `grid2.py` 虽然叫"实验"，但它们产出的表是**界面「效果验证」页的数据源**，
> 所以留在 `scripts/` 根目录，不要挪走。

## 二、一次性实验（`scripts/research/`，24 个）

产物统一写 `output/experiments/`，**可以整目录删除**，不影响应用。
运行方式：`python -m etfquant research <名字> [参数]`。

| 脚本 | 对应结论 | 一句话 |
|---|---|---|
| `ab_test.py` | README 7.3 | GitHub 三条改进（去重/错峰/波动目标）逐条挂到 TGAR 上 |
| `ab_pool.py` | README 7.4 | 标的池范围与闸门判据 |
| `rolling_gate.py` | README 7.4 | 闸门判据的滚动窗口稳定性 |
| `ab_dual_mom.py` | README 7.6 | 双因子行业动量（12 月 / 月度 / Top3） |
| `ab_resid.py` | README 7.6 ⑦ | 残差因子接入 5 日框架（含邻域扫描） |
| `ab_macross.py` | README 7.7 | 均线金叉死叉波段 |
| `ab_momvol.py` | README 7.8 | 动量 + 低波月度 Top3 |
| `ab_chan.py` | README 7.9 | 缠论按笔（含"离线 vs 在线"的未来函数对照） |
| `ab_early_era.py` | 2015-2016 压力测试（已跑通，见 README 7.11） | 早期数据在 `data/cache_early`，不动生产缓存 |
| `candidates.py` | — | 从增量归因里锁定候选配置 |
| `diagnose.py` | — | 年度收益、状态分布、仓位归因的临时诊断 |
| `ab_premium_filter.py` | README 7.21 | 加仓时溢价超阈值跳过（V1/V2） |
| `premium_audit.py` | README 7.21 | 策略实际买入/卖出时的溢价率审计 |
| `cb_premium_sensitivity.py` | README 7.21 | 跨境 ETF 溢价损耗的压力测试 |
| `signal_decay.py` | README 7.24 | R20 的 Rank IC 随持有期衰减 |
| `bootstrap_ci.py` | README 7.25 | 块自助法估 CAGR/卡玛/回撤的置信区间 |
| `stop_loss_diagnostic.py` | README 7.18 | 个券止损有效性诊断（中位 vs 均值） |
| `startpoint_sensitivity.py` | README 7.17 | 起点敏感性 |
| `build_wide_pool.py` | README 7.19 | 生成扩池候选（不覆盖 `etf_pool.csv`） |

## 三、新增脚本的模板选择

| 要验证的东西 | 抄这个 |
|---|---|
| 横截面选股 / 月度调仓 | `research/ab_dual_mom.py` |
| 单标的择时 / 全仓进出 | `research/ab_macross.py` |
| 多因子打分合成 | `research/ab_momvol.py` |
| 形态 / 结构类信号 | `research/ab_chan.py`（必须用在线状态机） |
| 往主策略里加因子开关 | `research/ab_resid.py` |

写之前先读 `docs/strategy_dd.md` 的四道闸门——它能在写第一行代码前挡掉大部分无效工作。
