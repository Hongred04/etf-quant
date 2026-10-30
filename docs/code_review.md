# ETF 量化工具 · 代码审查报告

审查日期：2026-10-24
审查范围：`etfquant/`（9 模块）、`scripts/`（11 脚本）、`server.py`、`web/`、`launcher.py`、`tests/`
审查维度：逻辑正确性、边界条件、异常处理、空值/类型、并发与资源管理

---

## 0. 结论摘要

**整体判断：核心记账逻辑可信，但"静默失败"是系统性缺陷。**

关于"当前结果是否可信"：可以。回测引擎的逐日收益可由权重序列严格复现（最大误差 1.67e-16），
无前视偏差（篡改未来价格不影响历史信号），15 项审计检查与 9 项单元测试全部通过。

关于"代码是否健壮"：不够。本次共发现 **34 项问题**（P0 3 项 / P1 3 项 / P2 8 项 / P3 20 项），其中高优先级 6 项。这些问题的共同特征是
**不报错、不告警、数字看起来正常**：

- 数据下载缺一段 → 静默跳过，回测照常跑完；
- 策略某日算不出权重 → `except: continue`，少调仓一次；
- 单标候选 → z-score 变 NaN → 空仓；
- 接口输出 `NaN` → 前端整页崩；
- 两个任务并发跑 → 互相读到写了一半的 CSV。

因此风险不在"现在的数字错了"，而在"下次更新数据或改参数时可能悄悄错，并且没人会发现"。

**修复优先级建议：P0 数据完整性与并发 → P1 异常与空值 → P2 口径 → P3 代码整洁。**

---

## 1. 高优先级问题（P0 / P1）

### P0-1　数据下载中断静默跳过，形成无声的历史缺口

- **位置**：`etfquant/datafeed.py:186-202`（`fetch_kline` 的 `except RuntimeError: continue`）
- **触发条件**：某个月窗口连续 3 次 HTTP 失败（腾讯接口限流、切换网络、代理抖动）。
- **影响**：该窗口的数据被直接跳过，`fetch_kline` 返回的序列中间缺一段（可能长达 2 年）。
  缺口处的收益率被算成 0，动量因子、200 日均线、回测净值全部失真，
  并且 `download_panel` 会把这个残缺结果写入缓存，**下次运行还会命中缓存继续用**。
- **实测**：当前 261 只标的缓存完整，尚未发生；属于"下次网络抖动就会踩"的隐患。
- **修复**：

```python
# datafeed.py  fetch_kline
def fetch_kline(ticker, start, end, adjust="qfq"):
    frames, failed = [], []
    for s, e in _month_windows(start, end):
        try:
            part = fetch_kline_window(ticker, s, e, adjust)
        except RuntimeError as exc:
            failed.append((s, e, str(exc)))      # 记录而不是丢弃
            continue
        if not part.empty:
            frames.append(part)
        else:
            failed.append((s, e, "空返回"))
    if failed:
        # 空返回可能是区间内确实无交易日，需要区分；至少把"请求失败"暴露出来
        raise RuntimeError(f"{ticker} 有 {len(failed)} 个窗口抓取失败: {failed[:2]}")
    ...
```

同时建议在 `download_panel` 汇总"成功/失败标的数"，失败标的打印名单而不是混在成功里。

---

### P0-2　不同任务可并发执行，互相读写同一批产物

- **位置**：`server.py:626-632`（只拦同 action 的运行中任务）、`server.py:82-121`（`start_task` 无全局互斥）
- **触发条件**：在网页点「运行回测」后立刻切到另一页点「生成信号」；或打开两个浏览器标签各点一次。
- **影响**：`run_signal.py` 会在 `run_backtest.py` 写 `nav_main.csv` / `weights_*.csv` 的中途读取它们，
  得到半新半旧的数据；`make_report.py` 更敏感，会把不同批次的结果拼进同一份报告。
  由于所有写操作都是 `to_csv` 直接覆盖（非原子），读到半个文件还会抛 `ParserError`。
- **修复**（两层）：

```python
# 1) 全局任务锁：同一时刻只允许一个任务
_TASK_RUNNING = threading.Lock()

def start_task(action):
    if not _TASK_RUNNING.acquire(blocking=False):
        raise RuntimeError("已有任务在执行，请等待完成")
    ...

# 2) 产物原子写：先写 .tmp 再 os.replace
def _atomic_to_csv(df, fp, **kw):
    tmp = fp.with_suffix(fp.suffix + ".tmp")
    df.to_csv(tmp, **kw)
    os.replace(tmp, fp)     # Windows/POSIX 均原子
```

`scripts/run_backtest.py`、`scripts/make_report.py`、`scripts/run_signal.py` 的落盘都建议改成原子写。

---

### P0-3　接口可能输出 `NaN`，导致前端整页白屏

- **位置**：`server.py:553`（`json.dumps(obj, ensure_ascii=False)` 使用默认 `allow_nan=True`）；
  数据源在 `server.py:185-200`（`float(row["夏普比率"])` 等直接转换，未过滤 NaN）
- **触发条件**：`perf_main.csv` 中任一指标为 NaN。典型场景：基准数据不足 30 个共同收益日
  （`metrics.py:154` 的 `if n > 30` 不成立时 `Beta`/`信息比率` 缺失）、或某策略净值恒定导致
  `信息比率` 为 NaN（`metrics.py:161-166` 会显式返回 `np.nan`）。
- **影响**：`json.dumps(float('nan'))` 输出字面量 `NaN`。实测：

  ```
  json.dumps({"x": nan}) -> {"x": NaN}
  Python json.loads 可读回，但浏览器 JSON.parse 抛 SyntaxError
  ```

  后果是 `loadPage` 进入 catch，整页显示「加载失败」，且 `NaN` 出现在 JSON 中时
  连 `state.overview` 都拿不到，用户无法定位原因。
  当前接口实测无 NaN（5 个接口 grep 结果为 0），属**潜伏缺陷**。
- **修复**：在 `_json` 统一做一次 `NaN/Inf → None` 清洗，这是最省事且不会漏的位置：

```python
def _clean(obj):
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj

def _json(self, obj, code=200):
    body = json.dumps(_clean(obj), ensure_ascii=False, allow_nan=False).encode("utf-8")
```

`allow_nan=False` 让"漏清洗"当场抛错，而不是把非法 JSON 发给浏览器。

---

### P1-1　回测首日若为调仓日，当日的开盘→收盘收益被丢弃

- **位置**：`etfquant/backtest.py:106-129`（`if i == 0:` 分支）
- **触发条件**：`start` 参数正好落在某个调仓日（即 `targets` 的键等于回测首个交易日）。
- **影响**：该分支只扣成本、不计算收益，`nav` 保持 `initial_cash`。实测：

  ```
  首日开盘 1.00 → 收盘 1.05（满仓，费率 0）
  期望 nav[0] = 1.05，实际 nav = [1.0, 1.0, 1.0]
  ```

  即首日收益被完整丢弃，且"只扣成本不计收益"造成单向偏低估。
- **当前影响**：**为零**。`warmup=260` 使首个信号日为 2019-01-25，晚于回测起点 2019-01-02，
  因此现有结果不受影响。但把 `--start` 改成任意调仓日就会踩中。
- **修复**：把首日分支与常规分支合并——首日同样按 `开盘价建仓 + 当日 open→close 收益` 处理：

```python
if i == 0:
    if dt in exec_targets:
        tgt = self._norm(exec_targets[dt])
        turn = 0.5 * float((tgt.reindex(cols).fillna(0.0) - w).abs().sum())
        trade_cost = 2.0 * turn * self.cost_per_side
        nav *= 1 - trade_cost
        _record(dt, w, tgt)
        w = tgt.reindex(cols).fillna(0.0)
        r = (c_now / o_now - 1.0).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        pr = float((w * r).sum())
        nav *= 1.0 + pr          # ← 缺失的一行
        turns.append((dt, turn)); costs.append((dt, trade_cost))
    ...
```

---

### P1-2　`run_signal --date` 早于面板起点时，静默用了最后一天

- **位置**：`scripts/run_signal.py:38-40`

```python
if sig_date not in panel.close.index:
    sig_date = panel.close.index[panel.close.index.get_indexer([sig_date], method="ffill")[0]]
```

- **触发条件**：`--date` 传入早于 `PANEL_START=2018-01-02` 的日期。
- **影响**：`get_indexer(..., method="ffill")` 找不到匹配时返回 **-1**，而 Python 的 `index[-1]`
  取的是**最后一个元素**。实测：

  ```
  传入 2020-01-01（早于起点） -> pos = -1 -> 落到 2024-01-04（最后一天）
  ```

  结果是：用户想回看某个历史日期，系统却给了"最新交易日"的信号，且**没有任何提示**。
- **修复**：

```python
pos = panel.close.index.get_indexer([sig_date], method="ffill")[0]
if pos < 0:
    raise SystemExit(f"信号日期 {sig_date.date()} 早于数据起点 {panel.close.index[0].date()}")
sig_date = panel.close.index[pos]
```

---

### P1-3　策略层 `except` 吞掉全部计算异常，静默少调仓

- **位置**：`etfquant/strategy.py:210-216`（`generate` 中 `except (KeyError, ValueError, IndexError): continue`）
- **触发条件**：`target_weights` 内部任何索引/对齐问题（面板缺列、因子全 NaN、类别映射缺失等）。
- **影响**：该调仓日被直接跳过，`targets` 里没有这一天的键，回测**保持旧仓位继续跑**。
  如果连续出错，等于策略在某段时间停止调仓，而回测照常输出净值曲线。
  这类错误不会体现在任何日志里——`run_backtest.py` 只会打印一句 `[OK] ...`。
- **修复**：默认不吞异常；提供显式的"容错模式"并计数上报：

```python
def generate(self):
    out, skipped = {}, []
    for dt in self.rebalance_days():
        elig = self.eligible_at(dt)
        if not elig.any():
            continue
        try:
            out[dt] = self.target_weights(dt, elig)
        except Exception as exc:            # 只在严格模式下才吞
            skipped.append((dt, type(exc).__name__, str(exc)))
    if skipped:
        print(f"[WARN] {self.name}: {len(skipped)} 个调仓日计算失败，"
              f"首例 {skipped[0][0].date()} {skipped[0][1]}: {skipped[0][2]}")
    return out
```

同时在 `run_backtest.py` 里把 `skipped` 写进 stats，让报告能显示"本次回测有 N 日未调仓"。

---

## 2. 中优先级问题（P2）

### P2-1　多因子策略在"只剩 1 只候选"时直接空仓

- **位置**：`etfquant/strategy.py:316` + `etfquant/factors.py:114-118`
- **机理**：`z = F.zscore_cs(df.loc[[dt], cand])` 对单行单列做横截面标准化，
  `df.std(axis=1)` 在样本量 1 时为 NaN（ddof=1），`replace(0.0, np.nan)` 处理不了 NaN，
  结果整行 NaN → `score.dropna()` 为空 → `return {}` → 当日空仓。
- **实测**：

  ```
  单列 z-score -> nan | dropna 后剩余列数: 0
  双列 z-score -> {A: 0.707, B: -0.707}   （正常）
  ```

- **影响**：S3-多因子在极端行情（候选被趋势/流动性过滤到只剩 1 只）时**空仓**，
  这是"无提示的仓位归零"，比不动更危险。也会污染消融实验的对比结论。
- **修复**：候选数 < 2 时不打分，直接按波动率倒数配权：

```python
if len(cand) == 1:
    return self._cap_weights(self._inv_vol_weights(dt, list(cand)))
```

### P2-2　`cagr` 用 252 折算年数，A 股实际 242 天/年，年化收益被高估

- **位置**：`etfquant/metrics.py:32-38`（`years = len(nav) / TRADING_DAYS`）
- **实测**：

  | 口径 | 年数 | CAGR |
  |---|---|---|
  | 252 交易日折算（现用） | 7.51 | **19.04%** |
  | 真实日历年 | 7.81 | **18.25%** |

  样本实际每年 242.4 个交易日。用 252 折算会把 7.81 年压缩成 7.51 年，
  指数项放大，CAGR 系统性高估约 **0.79 个百分点**。
- **影响**：这是报告首页的头号数字。收益率、卡玛、Alpha 都受影响（波动率用 252 年化是行业惯例，可保留）。
- **修复**：CAGR 改用日历天数，并在指标名后标注口径：

```python
def cagr(nav, use_calendar=True):
    years = (nav.index[-1] - nav.index[0]).days / 365.25 if use_calendar else len(nav) / 252
```

### P2-3　"累计交易成本"是收益率简单加总，不等于净值实际少涨的部分

- **位置**：`etfquant/backtest.py:177`（`"累计交易成本": float(cost_s.sum())`）
- **机理**：`trade_cost` 是当日从净值里扣掉的**收益率**，逐日累加忽略复利。
- **实测**：`sum(trade_cost) = 36.24%`，而用零成本反事实对比得到的实际净值拖累是 **30.41%**，
  差 5.8 个百分点。
- **影响**：报告与前端把它当"累计成本"展示，会让人以为成本比实际更重，进而误判"降换手"的收益空间。
- **修复**：用反事实法计算，语义才准确：

```python
res_cost = Backtester(...).run(targets)          # 含成本
bt0 = Backtester(close, open_, fee=0.0, slippage=0.0)
res_free = bt0.run(targets)                      # 零成本
stats["累计交易成本"] = float(1 - res_cost.nav.iloc[-1] / res_free.nav.iloc[-1])
```

### P2-4　回撤熔断用"基准价格"冒充"策略净值"，与文档定义不符

- **位置**：`etfquant/strategy.py:443-447`
- **机理**：`_drawdown_triggered` 用 `_bench_series()` 的 60 日高点回撤判断，而类文档写的是
  "组合级回撤熔断（净值自 60 日高点回撤）"。注释解释是为了避免前视（策略净值需回测后才知），
  但这是**把问题绕过去了**：正确做法是用策略自己的日频净值（可由已实现持仓逐日推算）。
- **影响**：默认 `dd_trigger=10.0` 使该分支永不触发，当前结果不受影响；
  但消融实验里"C. 开启回撤熔断"那一行测的其实是"基准回撤熔断"，结论不可直接引用。
- **修复**：要么改用策略净值（需要把净值状态回传给策略），要么在文档与实验中明确改名为
  "基准回撤熔断"，避免结论误植。

### P2-5　`api_portfolio` 每次请求重建整个面板，1.6 秒 / 约 20 MB

- **位置**：`server.py:400-420`（`load_panel` 读 260 个 CSV）+ `_cached("portfolio", 120, ...)`
- **实测**：缓存未命中 1.61s，其余接口 8-55ms。
- **影响**：TTL 120 秒能挡住重复访问，但每次任务完成都会 `invalidate_cache()`，
  用户回到持仓页要等 1.6 秒。若多个标签页同时刷新，`ThreadingHTTPServer` 会并发构建面板，
  内存瞬时可达 8×20MB ≈ 160MB。
- **修复**：把面板缓存到磁盘（parquet/pickle），或进程内保留一个 `Panel` 单例，
  仅在 `invalidate_cache` 时失效重建；同时给 `load_panel` 加 `functools.lru_cache` 语义的复用。

### P2-6　子进程无超时、无强杀，任务可能永久停留在 running

- **位置**：`server.py:105-118`
- **触发条件**：子进程卡在 HTTP 请求（`_http_get` 单次 15 秒 × 3 次重试 × 260 只 ≈ 3 小时）、
  或等待 stdin、或 pip 安装卡住。
- **影响**：`TASKS[tid]["status"]` 永远 `running`；前端 700ms 轮询一直在跑；
  由于 `do_POST` 认为该 action 仍在运行，用户**无法重新发起**这个任务，只能重启服务。
- **修复**：加超时看护线程，超时后 `proc.kill()` 并标记 `error`：

```python
deadline = time.time() + spec.get("timeout", 900)
for line in proc.stdout:
    if time.time() > deadline:
        proc.kill()
        raise TimeoutError(f"{spec['label']} 超过 {spec.get('timeout', 900)} 秒，已终止")
```

### P2-7　`TASKS` 只增不删，长期运行内存缓慢增长

- **位置**：`server.py:60`、`server.py:86-100`
- **影响**：每个任务保留完整日志列表（110 秒的扫描任务可能有数百行）。单条量级不大，
  但这是一个常驻服务，一周内累积到上千条任务后需要重启。属于典型资源泄漏形态。
- **修复**：任务完成后保留最近 N 条（如 50），或用 `collections.OrderedDict` + 淘汰。

### P2-8　`--end` 默认值写死在 `run_backtest.py`

- **位置**：`scripts/run_backtest.py:43`（`default="2026-10-23"`）
- **影响**：与上一轮修掉的 `download_history.py` 同一类问题。数据更新到 11 月后，
  回测仍只跑到 10-23，而用户以为已经是最新。**启动器与网页的「运行回测」都不会传 `--end`**，
  所以这个写死的日期是实际生效值。
- **修复**：默认改为 `pd.Timestamp.today()`，或干脆 `default=None` 并在 `load_panel` 时取
  `panel.close.index[-1]`。

---

## 3. 低优先级问题（P3，汇总）

| # | 位置 | 问题 | 触发/影响 | 建议 |
|---|---|---|---|---|
| 1 | `server.py:605-609` | 静态目录校验用字符串前缀匹配 | `../webx/a.js`、`../web_evil/a.js` 均通过校验（实测 True），可跨目录读到同前缀路径下的文件 | 改用 `Path.is_relative_to(WEB.resolve())` |
| 2 | `server.py:616-620` | `int(Content-Length)` 非数字时抛 ValueError 未捕获 | 畸形请求导致连接异常断开 | `try: length = int(...) except ValueError: length = 0` |
| 3 | `server.py:590` | `/api/task` 返回 `dict(t)` 浅拷贝，`log` 列表仍与 worker 共享 | 序列化时若 worker 追加日志，可能读到不一致快照 | 返回 `{**t, "log": list(t["log"])}` |
| 4 | `server.py:181` | `resample("YE")` 使 2026 只统计到 10 月 | 年度收益柱状图里 2026 柱是"部分年度"，与整年柱并列易误读 | 末年为部分年度时加斜纹或标注"至今" |
| 5 | `etfquant/datafeed.py:96` | `len(fields) < 46` 硬编码丢弃 | 字段较少的标的（退市整理期、特殊品种）被静默排除，不计数 | 改为按需索引 + 缺失计数上报 |
| 6 | `etfquant/datafeed.py:263-264` | 快照失败时 `expected = end` | 周末/节假日会得到 `expected=周六`，缓存永远达不到 → **每次都全量重下 261 只** | 快照失败时退化为"缓存最后日期 + 7 天"或直接跳过更新 |
| 7 | `etfquant/datafeed.py:283-288` | `to_csv` 直接覆盖，非原子 | 写入中断留下半文件，下次 `read_csv` 抛异常并中断整个 `pool.map` | 写 `.tmp` + `os.replace` |
| 8 | `etfquant/datafeed.py:292` | `pool.map` 中单标的抛异常会中断整批 | 一只标的的损坏缓存导致其余 259 只不落盘 | 用 `submit` + `as_completed`，逐只 try |
| 9 | `etfquant/factors.py:33` | `ma()` 的 `min_periods=max(5, window//3)` | MA200 只需 67 个样本就有值，次新 ETF 会得到"伪 MA200"参与趋势过滤 | 长周期均线用 `min_periods=window`，或至少 0.9×window |
| 10 | `etfquant/strategy.py:155-157` | `anchor` 变量算完未使用 | 死代码，误导读者以为以基准日对齐 | 删除 |
| 11 | `etfquant/strategy.py:291` | `DualMomentum` 防守仓 `{idxmax(): 1.0}` 单只 100% | 违反基类"单只 ≤ max_weight"约束，风险集中 | 套用 `_cap_weights` 或拆分到多只债券 |
| 12 | `etfquant/strategy.py:420` | `regime_at` 数据不足 210 日时返回 `1.0`（满仓） | 乐观默认；新池/短历史时直接满仓 | 返回中性 0.5 或显式告警 |
| 13 | `etfquant/universe.py:33` | "短融"被归入货币类 | 短融 ETF 实质是债券，被 `exclude_categories=("货币",)` 剔除 | 从货币关键词中移除，交给"债券"规则 |
| 14 | `etfquant/universe.py:72-82` | `dedup_key` 只截取 `ETF` 之前部分 | "华夏沪深300ETF" 与 "沪深300ETF华夏" 去重键不同，同一指数保留了两只 | 先剥离基金公司前缀/后缀再比较 |
| 15 | `etfquant/universe.py:132,150,159` | 参数名 `min_avg_amount`（均值）实际用快照单日成交额；`recent` 计算后未使用 | 文档与实现不符；`recent` 是死代码 | 用 `recent` 的成交额均值，或把参数改名为 `min_amount_today` |
| 16 | `etfquant/backtest.py:49` | `open_=None` 时用 `close` 代替开盘价 | 调用方忘记传开盘价时，跳空与日内收益建模静默失真 | 显式 `raise` 或打印警告 |
| 17 | `etfquant/backtest.py:140` | `v_gap <= 0` 时 `w` 不变但 `nav` 未同步 | 组合跳空 -100% 的极端情形会计失真（实际不可能，但逻辑不闭合） | 此时直接 `nav = 0` 并终止 |
| 18 | `web/app.js:1393` | 轮询 `catch { /* 忽略 */ }` 无限重试 | 服务挂掉后 UI 永久显示"运行中"，用户只能手动刷新 | 连续失败 N 次后停止并提示 |
| 19 | `web/app.js:97-101` | `api()` 无超时无重试 | 服务端 1.6s 的 portfolio 请求在弱网下可能长时间挂起 | 加 `AbortController` + 10s 超时 |
| 20 | `tests/test_core.py:68-86` | 买入持有测试从 `iloc[5:]` 起比较 | 恰好绕过了首日，导致 P1-1 未被发现 | 增加"首日即调仓 + 开盘≠收盘"用例 |

---

## 4. 已确认无问题的方面

为避免只报坏消息，以下几项是本次重点验证且**通过**的：

1. **无前视偏差**：篡改 2022-12-12 之后全部价格，2022-12-12 之前的 189 个调仓日信号
   完全一致（不一致数 0）。
2. **记账可复现**：767 个非调仓日的日收益，引擎值与"权重×收益"复算值最大误差 1.67e-16。
3. **换手与成本自洽**：`交易明细 Σ|Δw|/2` 与引擎 `turnover` 完全一致（误差 1e-6），
   且 `换手 × 2 × 费率` 与 `累计交易成本` 折年值一致（4.83% vs 4.83%）。
4. **权重约束**：非负、合计 ≤ 1（最大缺口 4.4e-16）、风险资产单只 ≤ 45%（实测最大 36.8%）。
5. **因子空值处理**：`report.py` 的 `_fmt_pct/_fmt_num` 对 NaN/Inf 统一输出 `-`，
   `api_research` 已做 `pd.isna` 判空（`server.py:349`）。
6. **任务子进程编码**：显式 `PYTHONIOENCODING=utf-8`，中文日志不再乱码。
7. **服务线程模型**：`ThreadingHTTPServer` + `daemon_threads=True`，Ctrl+C 可正常退出。

---

## 5. 整体质量评估

**架构（良好）**：数据层 / 因子层 / 策略层 / 回测层 / 表现层职责清晰，
`Panel` 与 `FactorBank` 的共享设计避免重复计算，策略用参数开关而非复制代码做消融，
这是本项目的最大优点——4 个策略共用一套记账，可比性有保障。

**数值正确性（良好）**：核心回测循环、权重漂移、成本扣减都经得起独立复算，
边界（权重和为 1、单只上限、现金账户）都有不变量校验，且这些校验已被脚本化。

**健壮性（偏弱）**：这是主要短板，具体表现为三点：

1. **静默失败太多**：全项目吞掉异常的位置共 6 处（`datafeed.py` 4 处、`strategy.py` 1 处、`server.py` 1 处），另有大量 `get(..., 0)` / `fillna(0)` 兜底把缺失值变成 0。
   对研究脚本可以接受，对每天要出下单清单的工具不可接受——错的那一天恰恰最需要报警。
2. **无原子写、无并发控制**：`to_csv` 直接覆盖 + 多任务可并发，是"数据被写坏"的标准配方。
3. **测试覆盖偏科**：9 个用例集中在因子数值与记账恒等式，缺少
   "首日建仓""单候选退化""数据缺口""接口序列化"这类**边界与集成**测试，
   本次发现的问题有 80% 落在测试盲区。

**风险结论**：

- 现有产出的数字**可以采信**（有可复现性验证支撑）；
- 但工具作为"每日运行的生产脚本"**还不够格**，主要风险是下次数据或参数变化时
  **出错而不报警**；
- 建议的加固顺序：**P0-1 数据缺口告警 → P0-2 任务互斥 + 原子写 → P0-3 JSON 清洗 →
  P1-1 首日收益 → P1-2 日期越界 → P1-3 异常计数**，其余按表格逐条处理。

以上 P0/P1 共 6 项修复工作量约 150 行代码，不改变任何策略逻辑与现有数字
（P2-2 的 CAGR 口径修正除外，它会让年化从 19.04% 变为 18.25%，需要同步更新报告与 README）。

---

## 6. 修复状态（2026-10-24 更新）

已按建议顺序完成 P0/P1 全部 6 项与 CAGR 口径修正，并顺带处理同文件的低风险项。

| 编号 | 问题 | 状态 | 处理方式 |
|---|---|---|---|
| P0-1 | 下载失败静默跳过 | ✅ 已修 | `fetch_kline` 有失败窗口即抛错；失败标的不写缓存、不中断整批，`download_panel` 返回 failed/gaps 报告 |
| P0-2 | 任务并发写坏产物 | ✅ 已修 | `_RUN_LOCK` 全局互斥（忙时 409）；所有落盘改 `atomic_to_csv` / `atomic_write_text`（临时文件 + `os.replace`） |
| P0-3 | JSON 含 NaN 致前端崩 | ✅ 已修 | `_clean_json` 统一清洗 + `allow_nan=False`，漏清洗会当场抛错 |
| P1-1 | 首日建仓丢当日收益 | ✅ 已修 | `i == 0` 分支补 `open→close` 收益与权重漂移 |
| P1-2 | 日期越界静默取最后一天 | ✅ 已修 | `get_indexer` 返回 -1 时抛 `SystemExit` |
| P1-3 | 策略异常被吞 | ✅ 已修 | 记录到 `self.skipped` 并打印警告，`perf_main.csv` 新增「失败调仓日」列（本次 4 策略均为 0） |
| P2-1 | 多因子单候选空仓 | ✅ 已修 | 候选数 ≤1 时退化为波动率倒数配权 |
| P2-2 | CAGR 用 252 折算 | ✅ 已修 | 改用日历天数：**19.04% → 18.25%**，同步更新报告/README/接口 |
| P2-3 | 累计成本简单加总 | ⬜ 未改 | 会破坏 `fee_side` 反推链，需单独设计，见下方说明 |
| P2-4 | 熔断用基准净值 | ⬜ 未改 | 属策略语义，需同步改消融口径，单独处理 |
| P2-5 | portfolio 每请求重建面板 | ⬜ 未改 | 1.3s/20MB，TTL 120s 可接受，建议后续加面板单例 |
| P2-6 | 子进程无超时 | ✅ 已修 | 看护线程 `threading.Timer` 超时 kill，返回 code 124 |
| P2-7 | TASKS 只增不删 | ✅ 已修 | 完成后裁剪到最近 50 条 |
| P2-8 | `--end` 写死日期 | ✅ 已修 | 默认取数据最后一天并打印实际取值 |
| P3-1 | 静态目录前缀匹配 | ✅ 已修 | 改 `Path.relative_to`，实测 `%2e%2e/` 返回 403 |
| P3-2 | Content-Length 未校验 | ✅ 已修 | try 包裹 + 1MB 上限 |
| P3-3 | 任务快照浅拷贝 | ✅ 已修 | 返回 `{**t, "log": list(t["log"])}` |
| P3-6 | 快照失败导致全量重下 | ✅ 已修 | 退化为取缓存最后日期 |
| P3-7 | to_csv 非原子 | ✅ 已修 | 同 P0-2 |
| P3-8 | pool.map 单点中断整批 | ✅ 已修 | 改 `submit + as_completed`，逐只 try |
| P3-19 | 前端请求无超时 | ✅ 已修 | `AbortController` 30s；错误信息优先展示服务端中文原因 |
| P3-20 | 测试未覆盖首日 | ✅ 已修 | 新增 6 项回归测试（首日收益、首日成本、CAGR 口径、单候选、部分缓存不落盘） |

**合计：34 项中已修 19 项，剩余 15 项为低风险或需独立设计。**

### 剩余项的处理意见

- **P2-3（累计成本口径）**：改成"零成本反事实对比"会让 `api_portfolio` 里
  `fee_side = 累计成本 / (换手 × 年数 × 2)` 的反推失效（当前正好抵消年数，取回 0.001）。
  正确做法是让 `Backtester` 额外输出 `cost_notional`（成交额绝对值），
  接口直接用它算费率，再单独给"成本拖累(复利)"字段。建议单开一轮改动。
- **P2-4（熔断口径）**：`_drawdown_triggered` 用的是基准价格而非策略净值。
  要么把策略净值状态回传（需要引擎协作），要么把文档与消融表标注改成"基准回撤熔断"。
  后者零风险，前者才解决问题。
- **P3-9 / P3-11 / P3-13 / P3-14（因子与分类口径）**：都影响选股结果，
  改动会让现有回测数字全部位移，应与策略迭代一起做，不适合夹在加固批次里。
- **其余**（P3-4 展示、P3-5 字段数、P3-10 死代码、P3-12 默认满仓、P3-15 命名、
  P3-16/17 极端边界、P3-18 轮询重试）均为低风险，可随手清理。

### 修复后的验证结果

```
单元测试     15 passed（原 9 项 + 新增 6 项回归）
审计脚本     19 项检查全部 PASS，0 FAIL
逐日复现     非调仓日日收益最大误差 1.67e-16
并发保护     POST 第二个任务 → 409「已有任务正在执行」
路径穿越     /static/%2e%2e/webx/x.js → 403（正常文件 200）
JSON 合法性   5 个接口无 NaN/Infinity
```

CAGR 口径变化带来的数字位移（这是唯一的对外数字变更）：

| 指标 | 252 折算 | 日历（现行） |
|---|---|---|
| 年数 | 7.5 | **7.8** |
| TGAR CAGR | 19.04% | **18.25%** |
| 基准 CAGR | 7.63% | **7.33%** |
| 卡玛 | 0.72 | **0.69** |
| 年化超额 | 11.41% | **10.92%** |
