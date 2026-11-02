# AGENTS.md — 给 AI Agent 的项目入口

> 一页纸说明"这是什么、跑什么命令、哪些不能动"。人类读者：门面看 `README.md`，
> 完整档案（全部实验记录）看 `docs/README_full.md`；
> 结构细节看 `docs/architecture.md`；脚本索引看 `scripts/README.md`。

## 1. 这是什么

A 股 ETF 的量化轮动工具：260 只 ETF 的横截面动量选股（TGAR 策略）+ 趋势闸门控仓位，
配 Web 控制台、Android APK、回测/诊断/报告/月报全套流程。
**核心结论**：样本内年化 17.3%（2019-01 ~ 最新），对外口径 **11%~13%**。

## 2. 三条命令上手

```bash
.venv/Scripts/python.exe -m etfquant doctor     # 数据截止日 + 绩效 + 口径（先看这个）
.venv/Scripts/python.exe -m etfquant refresh    # 更新行情 → 回测 → 信号（= 界面按钮）
.venv/Scripts/python.exe -m etfquant test       # 271 项单测
```

Windows 上双击 `打开界面.bat` 启动 Web 控制台（`http://127.0.0.1:8800/`）。
**服务是前台进程**：会话结束/窗口关闭即退出，不是 bug，重启即可。

## 3. 目录地图

| 路径 | 作用 | 谁在用 |
|---|---|---|
| `etfquant/` | 核心库：数据、因子、策略、回测、指标、口径 | 全部 |
| `server.py` | Web API + 任务调度 | 桌面端 |
| `web/` `mobile/` `android/` | 控制台 / App 静态页 / APK 工程 | 界面 |
| `scripts/` | 日常流程脚本（16 个，见 `scripts/README.md`） | CLI、界面任务 |
| `scripts/research/` | 一次性实验（11 个），产物进 `output/experiments/` | 只在做对照实验时 |
| `output/` | **产物契约**见 `output/README.md`：顶层是 app 依赖，`experiments/` 可随时删 | 服务/报告 |
| `data/` | 行情缓存（`cache/kline_qfq/*.csv`）、标的池、归档信号 | 全部 |
| `tests/` | 271 项单测 | CI 式自检 |
| `docs/` | 架构、外部策略尽调清单、代码评审 | 人 + Agent |

## 4. 硬约束（改代码前必读）

1. **落盘必须原子写**：`etfquant/io_utils.atomic_to_csv / atomic_write_text`。
   直接 `to_csv` 覆盖会留下半文件，下游读到的是坏数据且不报错。
2. **信号必须确定性**：任何"排名取前 N"都要显式 tie-break（本项目：分数降序、同分按原始 R20 降序）。
   pandas 默认 quicksort 对并列不稳定，会导致同一份数据两次运行选出不同标的。
3. **口径只有一个来源**：对外收益一律说 **11%~13%**，回测的 17.3% 只能称"样本内上限"。
   换算在 `etfquant/expectation.py`，数据在 `output/overfit_summary.json`。
   **禁止在任何地方手写这两个数字。**
4. **结构类信号必须在线计算**：笔/线段/中枢/形态一律用逐日状态机（`etfquant/chan.py::online_states`），
   并配因果性单测。用全样本端点回放会虚增约 8 倍 CAGR（README 7.9 有实测）。
5. **策略/因子改动后**：跑 `python -m etfquant test` + `python -m etfquant audit`；
   改了 `strategy.py` 还要跑 `python -m etfquant verify-engine`（手机端逐点对拍）。
6. **不要在这个仓库里调参**：7.5 的过拟合体检（PBO 43.2%）已判定样本内搜索的期望收益
   低于风险，下一步只能靠前向跟踪（`data/signal_archive.csv` + 月报）。
7. **依赖方向单向**：生产模块不得 import 研究模块（`chan/macross/meta/...`），
   `etfquant` 不得 import `server`。新增模块要同时登记到 `docs/architecture.md` §1.5 的
   模块地图，否则 `audit` 的 L 节直接 FAIL。这条规则是可执行的，不是文档约定。

## 5. 数据与外部依赖

- 行情：腾讯（`qt.gtimg.cn` / `web.ifzq.gtimg.cn`），**无 Token**，无需 akshare/tushare。
- **限流**：连续请求几百次会整段返回 HTTP 501。下载脚本已做增量与限速；
  自己写探测时单线程 + 间隔，别并发轰。
- 交易日判定：`etfquant/datafeed.is_market_closed()` = "此刻不可能存在未完成的当日K线"
  （交易日 0:00~9:30 也算已收盘，否则早盘更新是 no-op——这个坑踩过一次）。
- 缓存：`data/cache/kline_qfq/<code>.csv`，前复权，列 `date,open,close,high,low,volume`。
- **ETF 份额**：`data/etf_shares.csv`（日度快照，`python -m etfquant shares` 采集，已挂在 `refresh` 末尾）。两交所只给**最新一日**，历史不可得——别想着用它回测，它现在的用途是攒到 120 个交易日后再测（README 7.14）。
- **当日 K 线分批落库**：2026-10-30 15:30 实测 260 只里只有 165 只有当日 bar。`download_panel` 有覆盖度门（`PARTIAL_DAY_MIN_COVERAGE = 0.8`）：低于门槛就整天丢弃、把已写入的那天回写裁掉，并让 `download_history` 以**退出码 3** 结束（`refresh` 因此中止）。看到「更新成功但数字没变」先查这条，不要当成 bug 去改回归逻辑。

## 6. 常见任务 → 该改哪里

| 想做 | 改这里 |
|---|---|
| 加一个因子 | `etfquant/factors.py` + `etfquant/strategy.py::FactorBank`，A/B 用 `scripts/research/` 下模板 |
| 加一个策略 | `etfquant/strategy.py`（继承 `RotationBase`）+ `scripts/run_backtest.py` 注册 |
| 改界面 | `web/app.js`（桌面）/ `mobile/app/assets/www/js/app.js`（手机，改完要对拍） |
| 加一个外部策略对照 | 复制 `scripts/research/ab_macross.py` 作模板，先查 `docs/github_strategies.md` 是否已测过，再读 `docs/strategy_dd.md` |
| 早期数据 | 放 `data/cache_early/`，**不要并进 `data/cache/kline_qfq`**——会改变 2019 年初的满 250 日判定 |
| 改数据源 | `etfquant/datafeed.py`（唯一出口），别在脚本里直接 requests；**新增入口必须同厂商**（复权因子要一致），且登记进 `KLINE_HOSTS` 以参与轮换 |
| 改产物格式 | 先看 `output/README.md` 的契约，server/报告都依赖它 |

## 7. 当前状态（便于判断上下文是否过期）

- 数据截止：见 `python -m etfquant doctor`；
- 归档：20 期（1 期真实 live + 19 期回填），纸上净值与同期回测偏差 0.00pp；
- 已完成五轮外部策略对照（README 7.3、7.6~7.9），结论均为否；
- **7.16 外部验证三件套**：`data/trials.json`（68 次试验登记）→ `dsr` 重算 DSR 90.4%（网格口径）~8.3%（独立口径）；`manifest` 复现清单 + `docs/pre_registration.md` 预注册（停止条件），三者都被 `audit` 守；
- **7.15 随机对照**：同候选集/闸门/成本，300 组随机选股 → 实际毛收益 **97 分位**、净收益 100 分位，但**夏普只有 60 分位**。选股的风险调整增量≈0；收益主要来自趋势过滤 + 闸门 + 低换手。`scripts/research/placebo.py`；
- **7.15 随机对照**：同候选集/闸门/成本，300 组随机选股 → 实际毛收益 **97 分位**、净收益 100 分位，但**夏普只有 60 分位**。选股的风险调整增量≈0；收益主要来自趋势过滤 + 闸门 + 低换手。`scripts/research/placebo.py`；
- **7.14 ETF 份额**：两交所都只给最新一日（日期参数无效）→ 无法回测；已建日度采集（`data/etf_shares.csv`，首日 1691 条 / 池内覆盖 260/260），满 120 个交易日后按预注册判据测；
- **7.13 元标签（仓位系数）**：三种映射全部跑输（夏普 0.69→0.60~0.65），元模型 AUC 0.442、特征与标签相关≈0 → 仓位层面没有可预测性；`meta_exposure` 默认关闭；
- **7.12 第二排序键（52 周高点距离）**：两档权重均未通过判据（5d t 1.57→1.20；2020-22 夏普 0.53→0.49），`secondary` 开关默认关闭；行业扩散度因同组相关性 0.73 未测；
- **7.11 压力测试（2015 股灾，时间外样本）**：闸门把股灾段回撤从 −53.6% 压到 −33.6%，
  但 2016-02 之后四年净值为 +0.5%（那时代没有主题 ETF 可轮）——闸门管风险，不管收益；
- **已做深度诊断（README 7.10）**：CAGR 17.3% 的加法分解是
  `大盘 6.1 + 动量 4.1 + 小盘 1.0 + alpha 9.6(t=1.38) − 波动拖累 3.5`。
  **alpha 不显著、top3−全池 t=1.57、CAGR 95% CI [−1.9%, 40.9%]、
  历史回撤 −28.9% 只是幸运路径（同分布中位 −39%）**。
  分区间：**没有衰减**（趋势 t=0.43），但尾部效应只存在于 2023-2026
  （2019-22 为 −2.7bp、2023-26 为 +62.5bp）——**是 regime bet，不是全天候 alpha**。
  引用这条策略时不要只说"年化 17%"——见 README 7.10 与 6.2 的口径说明；
- 单测 217 项、审计 23 项全过；APK v1.5.0。
