# etf-quant — A 股 ETF 动量轮动策略（TGAR）

面向 A 股 ETF 的量化选股与轮动回测工具：完整数据管道、四个可比策略、事件驱动回测引擎、
绩效归因与**系统性的自我证伪框架**。本仓库的重点不是"一个策略"，而是**怎么诚实地评估一个策略**。

<!-- exp:begin 由 scripts/sync_readme.py 生成，勿手改 -->
| 口径 | 值 |
|---|---|
| 回测年化（样本内上限） | 16.99% |
| **对外预期** | **11%~13%** |
| 锚点：滚动前向（纯样本外） | 12.62% |
| 最大回撤 / 夏普 | -28.9% / 0.68 |
| 基准：沪深300 全收益 | 年化 6.9%，回撤 -44.7% |
| 区间 / 标的池 | 2019-2026 / 全市场约 260 只 ETF |
<!-- exp:end -->

> **这个 17.45% 该怎么读**：29 个变体在同一段历史上比过大小，"留下的那套最好"本身带乐观偏差。
> 扣掉"挑参数"的代价后，可对外引用的是 **11%~13%**。推导过程见
> [docs/README_full.md §6.2](docs/README_full.md)。

## 策略：TGAR（趋势闸门自适应轮动）

- **选股**：全市场 ETF 剔除流动性差与高拥挤标的，按 20 日动量取 Top6 等权，月度调仓 + 排名缓冲带
- **择时**：市场状态闸门（价格趋势 + 宽度），恶化时降至 50% 敞口、配债券腿；叠加回撤熔断
- **成本口径**：双边 0.10%，年换手约 24 倍，换手成本约 4.8%/年已计入全部收益数字

## 为什么可以信一点（也只信一点）

这个仓库把"证明自己错了"当成一等公民：

- **16 组验证实验全部留档**（含 5 组负结果）：外部策略移植、元标签、第二排序键、残差动量……
  跑完没用的照实写进 [docs/README_full.md 第七章](docs/README_full.md)
- **随机对照**：同候选集、同敞口、同费率的 placebo 策略与之对比，选股增量风险调整后 ≈ 0——
  系统有效性来自"趋势过滤 + 闸门 + 低换手"的工程组合，不是选股魔法
- **Deflated Sharpe 重算**（独立口径 ~8.3%）、**预注册停止条件**、**试验登记簿**（`data/trials.json`）
- **全套审计自检**（`python -m etfquant audit`）：数据口径、记账一致性、**架构依赖方向**、
  文档数字与产物逐格核对

## 快速开始（Windows）

```bat
:: 环境（项目内置 venv，见 docs/README_full.md §0.3）
安装依赖.bat          :: 首次

:: 日常使用：双击"打开界面.bat"（Web 控制台），或命令行：
python scripts/build_universe.py     # 1. 扫描全市场 ETF（约 110 秒）
python scripts/download_history.py   # 2. 下载前复权日线（之后走本地缓存）
python scripts/run_backtest.py       # 3. 四策略回测 + 消融
python scripts/run_signal.py         # 4. 生成最新交易日选股信号
python scripts/make_report.py        # 5. 生成 HTML 研究报告
python -m etfquant audit             # 6. 19 项审计自检
```

**成品下载**（免安装）：到 [**Releases**](https://github.com/Hongred04/etf-quant/releases/latest) 下载——

- **etf-quant.exe**（65 MB）：Windows 便携 Web 控制台，双击即用，内置数据快照、只读展示
- **etf-quant.apk**（509 KB）：Android 离线 App，引擎与桌面端逐日对拍验证（见 [mobile/README.md](mobile/README.md)）

## 仓库导航

| 位置 | 内容 |
|---|---|
| [docs/README_full.md](docs/README_full.md) | **完整档案**：启动手册、实盘跟踪、全部 16 组实验记录（7.1~7.16） |
| [docs/strategy_dd.md](docs/strategy_dd.md) | 策略尽调清单（五道闸门） |
| [docs/github_strategies.md](docs/github_strategies.md) | 72 条开源策略调研（逐条判定） |
| [docs/pre_registration.md](docs/pre_registration.md) | 预注册：前向验证的停止条件 |
| [etfquant/](etfquant/) | 核心库：策略 / 回测 / 数据源（双主机容灾 + 覆盖度门）/ 诊断 |
| [scripts/](scripts/) | 刷新管道、审计（audit.py）、深度诊断（deep_check）、DSR、月报 |
| [data/trials.json](data/trials.json) | 试验登记簿：10 次实验 68 变体 |
| [output/](output/) | 回测产物、诊断快照、manifest 指纹、HTML 报告 |

## 数据来源

行情：腾讯行情接口（主）+ 百度股市通（备），前复权日线；已做双入口轮换、
当日覆盖度门（缺 20% 以上自动裁半天）、缓存原子写。不依赖 akshare/tushare。

## 免责声明

回测基于历史数据，存在过拟合风险，样本外表现已系统性折价（见口径表）。
本仓库仅供研究学习，不构成任何投资建议。
