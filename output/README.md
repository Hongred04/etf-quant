# output 目录契约

**规则**：顶层是**应用依赖的产物**——删了界面、报告、月报会坏；`experiments/` 是研究残留，
可以整目录删除。新增文件时先想清楚属于哪一类，再决定放哪。

## 一、顶层（应用依赖，勿删）

| 文件 | 谁读 | 内容 |
|---|---|---|
| `nav_main.csv` | server、报告、sync_readme | 四策略 + 基准的日净值 |
| `perf_main.csv` | server、报告、`etfquant doctor` | 绩效汇总（CAGR/夏普/回撤/卡玛/换手…） |
| `weights_*_main.csv` | server「回测持仓」 | 每日持仓权重快照 |
| `trades_*_main.csv` | server「回测持仓」 | 逐笔调仓记录 |
| `holdings_*_main.csv` | server | 各策略当前持仓 |
| `ladder.csv` / `subsample2.csv` / `ablation.csv` | server「效果验证」 | 增量归因 / 子样本 / 消融 |
| `overfit_summary.json` | server、`expectation.py` | 过拟合体检 + **对外口径**（`expectation` 字段） |
| `diagnostics.json` | README 7.10 | 深度诊断结论（IC / 归因 / 执行 / 容量 / 误差棒 / 集中度），`python -m etfquant deep` 生成 |
| `overfit_walk.csv` | server「实验四」 | 滚动前向逐窗明细 |
| `overfit_*.csv` | 研究 | CSCV/网格/收益矩阵（重跑 `overfit_check` 可重生） |
| `signal_<调仓日>.csv` | 界面「调仓清单」 | 当前这一期的下单清单 |
| `signal_meta.json` | server | 信号元数据（数据日/调仓日/下次换仓） |
| `ETF量化策略研究报告.html` | 界面「完整报告」 | HTML 研报 |
| `reports/monthly_*.md` | 人 | 月报归档 |
| `preview/` | 界面 | 预览缓存 |
| `_stale_signals/` | 无 | 过期信号留档（非调仓日生成的历史文件） |

## 二、`experiments/`（研究产物，可删）

一次性对照实验的输出，由 `scripts/research/*.py` 写入，对应结论记录在 README 第七章。
文件名与脚本同名（`ab_momvol.py → ab_momvol.csv`），便于追溯。
`_old_etfs.json` 是 2018 年前上市 ETF 的探测缓存（受腾讯限流影响，压力测试尚未跑完）。

## 三、口径提醒

对外引用收益时**只能用 `11%~13%`**，`perf_main.csv` 里的 17.3% 是样本内上限。
换算见 `etfquant/expectation.py`，不要在这里手写数字。
