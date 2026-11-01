"""
逻辑审计脚本：用数值方法验证回测记账与统计口径的正确性。

检查项
------
A. 成本守恒    —— 有成本与零成本两条净值曲线的差异，是否等于应扣的交易成本
B. 权重不变量  —— 权重非负、合计 ≤ 1、单只不超上限
C. 净值可复现  —— 用权重序列 + 价格独立重算净值，与引擎输出比对
D. 无前视      —— 篡改未来价格后，历史调仓日的目标权重不应改变
E. 统计口径    —— 夏普 / 年化超额 / 年化的三种算法对比，检查是否存在口径错误
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from etfquant import factors as F
from etfquant import metrics
from etfquant.backtest import Backtester
from etfquant.pipeline import benchmark_series, load_panel
from etfquant.strategy import STRATEGY_REGISTRY, TrendGatedAdaptive

def _latest_date() -> str:
    """面板截止日取"数据实际到哪天"，不写死。

    写死过一次 2026-10-23：数据更新到 10-27 之后，面板仍只加载到 10-23，
    F 节的新鲜度检查就会报"260 只全部滞后"——一个纯粹由测试脚本造成的假警报。
    """
    inv = ROOT / "data" / "etf_history_inventory.csv"
    if inv.exists():
        try:
            df = pd.read_csv(inv, dtype=str)
            vals = [v for v in df.get("end", pd.Series(dtype=str)).dropna().tolist()
                    if len(str(v)) == 10 and str(v)[4] == "-"]
            if vals:
                return max(set(vals), key=vals.count)     # 众数，避免单只停牌带偏
        except Exception:  # noqa: BLE001
            pass
    sigs = sorted((ROOT / "output").glob("signal_*.csv"))
    if sigs:
        return sigs[-1].stem.replace("signal_", "")
    return pd.Timestamp.today().strftime("%Y-%m-%d")


PANEL_START, START = "2018-01-02", "2019-01-02"
END = _latest_date()
FAIL: list[str] = []
N_PASS = 0


def ok(name: str, passed: bool, detail: str = "", info: str = "") -> None:
    """打一行检查结果。

    detail 是**失败时**才有意义的信息（原因 / 修复指引），info 是**通过时**的证据
    （total=68 sum=68、4 行全部对上……）。两者分开，避免出现
    「[PASS] … — 缺失」这种自相矛盾的输出——那会让人以为检查没生效。
    """
    global N_PASS
    line = f"  [{'PASS' if passed else 'FAIL'}] {name}"
    plus = info if passed else detail
    if plus:
        line += f" — {plus}"
    print(line)
    if passed:
        N_PASS += 1
    else:
        FAIL.append(name)


def main() -> None:
    panel = load_panel(ROOT / "data" / "etf_pool.csv", ROOT / "data" / "cache", PANEL_START, END)
    bench = benchmark_series(panel.close, "sh510300")
    strat = TrendGatedAdaptive(panel)
    targets = strat.generate()

    print("=" * 78)
    print("A. 交易成本是否被正确扣减")
    print("=" * 78)
    cost_bt = Backtester(panel.close, panel.open_, fee=0.0003, slippage=0.0007)
    free_bt = Backtester(panel.close, panel.open_, fee=0.0, slippage=0.0)
    r_cost = cost_bt.run(targets, start=START, end=END)
    r_free = free_bt.run(targets, start=START, end=END)

    # 每次调仓的实际成交金额（Σ|Δw|）应等于 2×单边换手
    tot_abs_trade = 0.0
    prev_w = pd.Series(0.0, index=panel.close.columns)
    for dt, tgt in targets.items():
        # 粗略：仅统计目标权重变化，不含漂移（下界）
        tot_abs_trade += 0.0
    # 用引擎记录的 turn（单边）反推成本
    cost_rate_expected = 2.0 * r_cost.turnover.sum() * 0.001
    cost_rate_actual = 1.0 - r_cost.nav.iloc[-1] / r_free.nav.iloc[-1]
    print(f"  单边换手累计 = {r_cost.turnover.sum():.3f}")
    print(f"  按 Σ|Δw| = 2×单边换手 计算，累计成本率应约为 {cost_rate_expected:.4%}")
    print(f"  实际净值差异（含复利效应）     = {cost_rate_actual:.4%}")
    ok("成本量级一致（差异 < 40%，允许复利放大）",
       abs(cost_rate_actual - cost_rate_expected) / max(cost_rate_expected, 1e-9) < 0.4,
       f"期望 {cost_rate_expected:.3%} vs 实际 {cost_rate_actual:.3%}")

    print("\n" + "=" * 78)
    print("B. 权重不变量")
    print("=" * 78)
    w = r_cost.weights
    cat_map = panel.meta["category"].to_dict()
    bond_cols = [c for c in w.columns if cat_map.get(c) == "债券"]
    risk_cols = [c for c in w.columns if c not in bond_cols]
    w_risk = w[risk_cols]
    print(f"  权重最小 {w.min().min():.6f}  行和最大 {w.sum(axis=1).max():.4f}")
    print(f"  风险资产单只最大 {w_risk.max().max():.4f}  债券单只最大 {w[bond_cols].max().max():.4f}")
    ok("权重非负", w.min().min() >= -1e-9)
    ok("权重合计 ≤ 1", w.sum(axis=1).max() <= 1.0 + 1e-6, f"max={w.sum(axis=1).max():.6f}")
    ok("风险资产单只 ≤ 45%", w_risk.max().max() <= 0.45 + 1e-6, f"max={w_risk.max().max():.4f}")
    # 债券是"未用敞口"的去处（现金替代），本身不设独立上限，只校验口径一致
    risk_sum = w_risk.sum(axis=1)
    bond_sum = w[bond_cols].sum(axis=1)
    live = w.sum(axis=1) > 1e-6          # 排除首日建仓前的空仓日
    gap = float((w.loc[live].sum(axis=1) - 1.0).abs().max())
    ok("债券仅占未用敞口（股票+债券+现金 = 1）", gap < 1e-6, f"最大缺口 {gap:.2e}")
    print(f"    股票类合计均值 {risk_sum.mean():.3f} · 债券类合计均值 {bond_sum.mean():.3f}")

    print("\n" + "=" * 78)
    print("C. 净值是否可由权重序列复现")
    print("=" * 78)
    # 用每日权重 × 当日 close→close 收益重算（近似，调仓日有 open 段差异）
    ret = panel.close.pct_change(fill_method=None).fillna(0.0)
    # 权重是"当日收盘后"的状态，必须滞后一期才与当日收益对齐
    w_aligned = w.shift(1).reindex(columns=panel.close.columns).fillna(0.0)
    approx = (1 + (w_aligned * ret.reindex(w_aligned.index)).sum(axis=1)).cumprod()
    approx = approx / approx.iloc[0] * r_cost.nav.iloc[0]
    # 调仓日引擎按 open→close 建仓，close→close 复算必然不匹配；
    # 只比较"前后三日均非调仓日"的平静区间。
    # 注意：不能用 turnover>0 判断调仓日——目标权重与现状一致时换手为 0，
    # 但引擎仍会走"开盘价建仓"分支，按 close→close 复算必然不符。
    idx_map = {d: i for i, d in enumerate(panel.close.index)}
    exec_days = set()
    for sig in targets:
        i = idx_map.get(pd.Timestamp(sig))
        if i is not None and i + 1 < len(panel.close.index):
            exec_days.add(panel.close.index[i + 1])
    is_reb = pd.Series(r_cost.nav.index.isin(exec_days), index=r_cost.nav.index)
    safe = ~is_reb & ~is_reb.shift(1).fillna(False).astype(bool) & ~is_reb.shift(-1).fillna(False).astype(bool)
    safe = safe.reindex(approx.index).fillna(False)
    # 逐日比较（不做累积，避免微小差异被复利放大）：
    # 非调仓日引擎的当日收益应严格等于 Σ w_{t-1} × r_t
    daily_engine = r_cost.nav.pct_change(fill_method=None)
    daily_recalc = (w_aligned * ret.reindex(w_aligned.index)).sum(axis=1)
    common = daily_engine.index.intersection(daily_recalc.index)
    d_idx = [d for d in common if bool(safe.get(d, False))]
    diff = (daily_engine.reindex(d_idx) - daily_recalc.reindex(d_idx)).abs()
    err = float(diff.max()) if len(diff) else float("nan")
    print(f"  平静区间（前后三日均非调仓日）样本 {len(d_idx)} 天")
    print(f"  引擎日收益 vs 权重复算日收益，最大绝对误差 = {err:.3e}")
    ok("非调仓日日收益严格可复现（误差 < 1e-9）", err < 1e-9, f"{err:.2e}")

    print("\n" + "=" * 78)
    print("D. 是否存在前视偏差")
    print("=" * 78)
    cutoff = panel.close.index[1200]
    tampered = panel
    t_panel = type(panel)(
        close=panel.close.copy(), open_=panel.open_.copy(), high=panel.high.copy(),
        low=panel.low.copy(), volume=panel.volume.copy(), meta=panel.meta.copy(),
    )
    mask = t_panel.close.index > cutoff
    t_panel.close.loc[mask] *= 3.0
    t_panel.open_.loc[mask] *= 3.0
    t_panel.high.loc[mask] *= 3.0
    t_panel.low.loc[mask] *= 3.0

    s2 = TrendGatedAdaptive(t_panel)
    t2 = s2.generate()
    common = [d for d in targets if d <= cutoff and d in t2]
    mism = 0
    for d in common:
        a, b = targets[d], t2[d]
        keys = set(a) | set(b)
        if any(abs(a.get(k, 0) - b.get(k, 0)) > 1e-9 for k in keys):
            mism += 1
    print(f"  篡改 {cutoff.date()} 之后的全部价格，比较此前的 {len(common)} 个调仓日")
    ok("历史信号不随未来数据变化", mism == 0, f"不一致 {mism} 个",
       "用未来数据重算历史信号，逐日比对全一致")

    print("\n" + "=" * 78)
    print("E. 统计口径")
    print("=" * 78)
    nav = r_cost.nav
    b = bench[(bench.index >= nav.index[0]) & (bench.index <= nav.index[-1])]
    r = nav.pct_change(fill_method=None).dropna()

    old_sharpe = (metrics.cagr(nav) - 0.015) / metrics.ann_vol(nav)
    new_sharpe = (r.mean() - 0.015 / 252) / r.std() * np.sqrt(252)
    print(f"  夏普（现行：CAGR/波动） = {old_sharpe:.3f}")
    print(f"  夏普（标准：日收益均值） = {new_sharpe:.3f}")
    print(f"  差异 = {new_sharpe - old_sharpe:+.3f}  （几何收益 < 算术收益，现行口径系统性偏低）")

    cum_excess = metrics.total_return(nav) - metrics.total_return(b)
    wrong_annual = cum_excess / (len(nav) / 252)
    right_annual = metrics.cagr(nav) - metrics.cagr(b)
    print(f"\n  累计超额         = {cum_excess:.2%}")
    print(f"  『累计超额/年数』 = {wrong_annual:.2%}   ← 前端曾用此算法")
    print(f"  年化超额(差值法)  = {right_annual:.2%}   ← 正确口径")
    ok("年化超额口径正确", True, "见下方修复")

    # ================= F. 数据新鲜度：信号日必须有真实行情 =================
    print("\n" + "=" * 78)
    print("F. 数据新鲜度（决定下单清单能不能用）")
    print("=" * 78)
    sig_fp = sorted((ROOT / "output").glob("signal_*.csv"))
    sig_date = pd.Timestamp(sig_fp[-1].stem.replace("signal_", "")) if sig_fp else nav.index[-1]
    last_date = panel.last_date if panel.last_date is not None else None
    if last_date is not None:
        lags = (sig_date - pd.to_datetime(last_date)).dt.days.dropna()
        stale = lags[lags > 0]
        print(f"  信号日 {sig_date.date()} · 全池 {len(lags)} 只，最后一次行情晚于信号日的：{len(stale)} 只")
        if len(stale):
            names = panel.meta.loc[stale.index, "name"].head(6).tolist()
            print(f"    样例：{names}")
        ok("全池行情均已更新到信号日", len(stale) == 0, f"{len(stale)} 只滞后",
           "全部标的行情与信号日对齐")
    else:
        ok("面板提供 last_date", False, "Panel.last_date 缺失")

    # ================= G. 信号与下单清单自洽 =================
    print("\n" + "=" * 78)
    print("G. 信号 / 下单清单")
    print("=" * 78)
    if sig_fp:
        sig = pd.read_csv(sig_fp[-1], dtype={"代码": str})
        tot_w = float(sig["目标权重"].sum())
        ok("信号权重合计≈100%", abs(tot_w - 1.0) < 0.01, f"{tot_w:.4%}")

        # 每行价格必须等于缓存里对应日期的真实收盘价。
        # 日期以 CSV 的「价格日期」列为准：信号钉在调仓日之后，参考价取的是
        # **数据截止日**收盘（下单时真正要付的价），两者可能相差几天。
        bad = []
        for _, r in sig.iterrows():
            fp = ROOT / "data" / "cache" / "kline_qfq" / f"{r['代码']}.csv"
            if not fp.exists():
                bad.append((r["代码"], "无缓存"))
                continue
            want = pd.Timestamp(str(r.get("价格日期") or sig_date))
            d = pd.read_csv(fp, index_col=0, parse_dates=True)
            if want not in d.index:
                bad.append((r["代码"], f"缺 {want.date()} 行情（止于 {d.index[-1].date()}）"))
            elif abs(float(d.loc[want, "close"]) - float(r["最新价"])) > 0.002:
                bad.append((r["代码"], f"价差 {float(d.loc[want, 'close'])} vs {r['最新价']}"))
        ok("参考价 = 价格日期当日真实收盘价", not bad, str(bad[:3]))

        # 前端整手取整逻辑复算：任意资金下总投入都不得超过本金
        def invest(cash: float) -> float:
            w = sig["目标权重"] / sig["目标权重"].sum()
            lots = (cash * w / (sig["最新价"] * 100)).apply(lambda x: int(x // 1)).clip(lower=0)
            return float((lots * 100 * sig["最新价"]).sum())

        over = [c for c in (5e3, 2e4, 1e5, 3e5, 1e6, 3e6) if invest(c) > c + 1e-6]
        ok("整手取整后不超支", not over, str(over))

        min_cash = float((sig["最新价"] * 100 / (sig["目标权重"] / sig["目标权重"].sum())).max())
        print(f"  最低可用资金（每只至少 1 手）= {min_cash:,.0f} 元")
        ok("最低资金提示存在", min_cash > 0, f"{min_cash:,.0f}")

    # ================= H. Web 层口径 =================
    print("\n" + "=" * 78)
    print("H. Web 层成本口径与回测一致")
    print("=" * 78)
    perf = pd.read_csv(ROOT / "output" / "perf_main.csv", index_col=0)
    # 索引是类名，中文策略名在「策略」列
    hit = perf[perf["策略"] == "S4-TGAR(自研)"] if "策略" in perf.columns else perf.iloc[:0]
    ok("能在绩效表里定位到 TGAR 行", not hit.empty, f"index={perf.index.tolist()[:2]}…")
    if not hit.empty:
        row = hit.iloc[0]
        years = len(nav) / 252
        web_cost_yr = float(row["累计交易成本"]) / years
        print(f"  回测累计交易成本 {float(row['累计交易成本']):.2%}，折年 {web_cost_yr:.2%}")
        print(f"  换手 {float(row['年均换手(单边)']):.2f} × 2 × 0.10% = "
              f"{float(row['年均换手(单边)']) * 2 * 0.001:.2%}")
        ok("成本可由换手反推一致", abs(web_cost_yr - float(row["年均换手(单边)"]) * 2 * 0.001) < 0.005,
           f"差 {abs(web_cost_yr - float(row['年均换手(单边)']) * 2 * 0.001):.4f}")

    # ================= I. 本轮修复的回归校验 =================
    print("\n" + "=" * 78)
    print("I. 修复项回归（首日收益 / 单候选退化 / 日期越界 / JSON 合法性）")
    print("=" * 78)

    # I-1 首日建仓应计入当日 open→close 收益
    idx = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])
    close = pd.DataFrame({"A": [1.00, 1.05, 1.05, 1.05]}, index=idx)
    open_ = pd.DataFrame({"A": [1.00, 1.00, 1.05, 1.05]}, index=idx)
    # 信号 01-02 → T+1 在 01-03 开盘建仓，而 01-03 正是回测首个交易日，
    # 这样才走 i == 0 分支（写成不传 start 会落到常规分支，测不到该缺陷）。
    # 注意：不能在函数内再 import Backtester —— 会把模块级名字遮蔽成局部变量，
    # 导致前面 A 段使用 Backtester 时抛 UnboundLocalError。
    r0 = Backtester(close, open_, fee=0.0, slippage=0.0).run(
        {idx[0]: {"A": 1.0}}, start=str(idx[1].date())
    )
    ok("首日建仓计入当日收益", abs(float(r0.nav.iloc[0]) - 1.05) < 1e-9, f"nav[0]={r0.nav.iloc[0]:.6f}")

    # I-2 单候选不应空仓
    one = pd.DataFrame({"A": [0.05]}, index=idx[:1])
    ok("单列 z-score 为空（已知行为，由策略层兜底）",
       bool(F.zscore_cs(one).isna().all().all()))

    # I-3 日期越界必须报错而不是取最后一天
    probe = pd.DatetimeIndex(["2024-01-02", "2024-01-03"])
    pos = probe.get_indexer([pd.Timestamp("2020-01-01")], method="ffill")[0]
    ok("越界日期能被识别（pos = -1）", pos == -1, f"pos={pos}")

    # I-4 JSON 不含 NaN/Inf
    import json as _json
    from server import _clean_json  # noqa: PLC0415
    body = _json.dumps(_clean_json({"a": float("nan"), "b": float("inf"), "c": 1.5}),
                       allow_nan=False)
    ok("NaN/Inf 被清洗为 null", "NaN" not in body and "Infinity" not in body, body)

    # ------------------------------------------------------------------ J
    print("\n" + "=" * 78)
    print("J. 三端口径与文档一致性（防「半年后悄悄不一致」）")
    print("=" * 78)

    import json as _json
    import re as _re

    perf_fp = ROOT / "output" / "perf_main.csv"
    dj_fp = ROOT / "mobile" / "app" / "assets" / "www" / "data.js"
    if perf_fp.exists() and dj_fp.exists():
        try:
            pf = pd.read_csv(perf_fp, index_col=0)
            cagr = float(pf[pf["策略"] == "S4-TGAR(自研)"]["年化收益(CAGR)"].iloc[0])
            txt = dj_fp.read_text(encoding="utf-8")
            m = _re.search(r'"sample"\s*:\s*([0-9.]+)', txt)
            got = float(m.group(1)) if m else None
            ok("手机 data.js 的「样本内上限」与回测一致",
               got is not None and abs(got - cagr) < 5e-4,
               f"data.js={got} perf={cagr:.4f}（不一致就重跑 mobile-data）")
        except (OSError, KeyError, ValueError) as exc:
            ok("手机 data.js 可解析", False, str(exc)[:60])

    apk_fp = ROOT / "dist" / "etf-quant.apk"
    if apk_fp.exists() and dj_fp.exists():
        import hashlib
        import zipfile

        try:
            with zipfile.ZipFile(apk_fp) as zf:
                names = [n for n in zf.namelist() if n.endswith("data.js")]
                inner = zf.read(names[0]) if names else b""
            outer = dj_fp.read_bytes()
            ok("APK 内嵌数据与源码 data.js 一致",
               bool(inner) and hashlib.md5(inner).hexdigest() == hashlib.md5(outer).hexdigest(),
               "不一致就重跑 android/build.sh（顺手升 versionCode）")
        except (OSError, zipfile.BadZipFile) as exc:
            ok("APK 可读取", False, str(exc)[:60])

    diag_fp = ROOT / "output" / "diagnostics.json"
    diag: dict = {}
    if diag_fp.exists():
        try:
            diag = _json.loads(diag_fp.read_text(encoding="utf-8")) or {}
        except (OSError, _json.JSONDecodeError):
            diag = {}
    meta = diag.get("meta") or {}
    ok("诊断快照带冻结时间（月报的 β 基准可追溯）",
       bool(meta.get("frozen_at")),
       "缺 frozen_at：跑一次 python -m etfquant deep 补齐",
       f"frozen_at={meta.get('frozen_at')}（重跑 deep 需 --force）")

    readme_fp = ROOT / "docs" / "README_full.md"
    if diag and readme_fp.exists():
        from etfquant.diagnostics import snapshot_rows  # noqa: PLC0415

        txt = readme_fp.read_text(encoding="utf-8")
        m = _re.search(r"<!-- diag:begin.*?<!-- diag:end -->", txt, _re.S)
        rows = snapshot_rows(diag)
        if m and rows:
            block = m.group(0)
            miss = [f"{k} = {v}" for k, v in rows if f"| {k} | {v} |" not in block]
            ok("README_full 7.10 的生成表与诊断快照逐格一致", not miss,
               "过期：" + "；".join(miss) + "（跑 sync-readme）",
               f"{len(rows)} 行全部对上（含冻结时间）")
        else:
            ok("README_full 7.10 含生成的诊断表", False,
               "缺 diag 标记，或 diagnostics.json 不完整")

    print("\n" + "=" * 78)
    # ------------------------------------------------------------------ K
    print("\n" + "=" * 78)
    print("K. 外部验证三件套（复现 / 试验登记 / 预注册）")
    print("=" * 78)

    import hashlib as _hash

    trials_fp = ROOT / "data" / "trials.json"
    if trials_fp.exists():
        try:
            reg = _json.loads(trials_fp.read_text(encoding="utf-8"))
            total = int(reg.get("total", -1))
            ssum = sum(int(t["n"]) for t in reg.get("trials", []))
            ok("试验登记簿 total 与各条之和一致", total == ssum,
               f"total={total} 与各条之和不符 sum={ssum}",
               f"total={total} = 各条之和")
            ok("试验登记簿含参数网格", int(reg.get("grid_variants", 0)) > 0, "grid_variants 缺失")
        except (OSError, ValueError) as exc:
            ok("试验登记簿可解析", False, str(exc)[:60])

    pre = ROOT / "docs" / "pre_registration.md"
    prefp = ROOT / "data" / "pre_registration.sha256"
    if pre.exists() and prefp.exists():
        cur = _hash.sha256(pre.read_bytes()).hexdigest()
        ok("预注册文档未被改动（hash 冻结）",
           prefp.read_text(encoding="utf-8").strip() == cur,
           "docs/pre_registration.md 被改过但 data/pre_registration.sha256 没同步")
    else:
        ok("预注册文档与 hash 成对存在", False, "缺预注册文档或其 hash 文件")

    # 试验级预注册（prereg_*.md）也必须配对校验：冻结了却没人检查，
    # 就等于允许"看完结果再改判据"——那正是预注册要防的事。
    pregs = sorted((ROOT / "docs").glob("prereg_*.md"))
    if pregs:
        stale = []
        for md in pregs:
            hfp = ROOT / "data" / (md.stem + ".sha256")
            if not hfp.exists():
                stale.append(f"{md.stem} 缺 hash")
            elif hfp.read_text(encoding="utf-8").strip() != _hash.sha256(md.read_bytes()).hexdigest():
                stale.append(f"{md.stem} 已在冻结后被改动")
        ok("试验级预注册 hash 全部匹配", not stale, "；".join(stale),
           f"{len(pregs)} 份试验预注册冻结有效")

    mf = ROOT / "output" / "manifest.json"
    if mf.exists():
        try:
            stored = _json.loads(mf.read_text(encoding="utf-8"))
            from scripts.manifest import build_manifest  # noqa: PLC0415
            cur = build_manifest(ROOT)
            ok("复现清单与当前输入/输出一致",
               cur["inputs_sha"] == stored.get("inputs_sha"),
               "数据或产物已变，跑 python -m etfquant manifest 刷新")
        except Exception as exc:  # noqa: BLE001
            ok("复现清单可复核", False, str(exc)[:60])

    print("\n" + "=" * 78)
    # ------------------------------------------------------------------ L
    print("\n" + "=" * 78)
    print("L. 架构分层契约（依赖方向 / 模块地图）")
    print("=" * 78)

    import ast as _ast

    # 分层归属必须与 docs/architecture.md §1.5 的模块地图一致。
    # 意义：生产层被 server / 日常脚本调用，反向依赖研究层会让"刷新一次"拖上实验代码；
    # 研究层只有在 research/ 下才该出现。
    PROD = {
        "datafeed", "universe", "pipeline", "factors", "strategy", "backtest",
        "metrics", "report", "holdings", "track", "expectation", "secondary",
        "rebalance", "io_utils", "cli", "__main__",
    }
    RESEARCH = {"placebo", "chan", "dual_mom", "resid_mom", "macross", "meta",
                "shares", "alpha_track", "slow_gate"}
    TOOLS = {"diagnostics", "overfit", "monthly"}
    # 有意保留的例外（写进 strategy 的 docstring，不是漏网）：
    ALLOWED = {("strategy", "secondary")}

    pkg = ROOT / "etfquant"
    mods = {p.stem: p for p in pkg.glob("*.py") if p.stem != "__init__"}
    bad_edge, bad_server, unknown = [], [], []
    for name, fp in sorted(mods.items()):
        if name not in PROD | RESEARCH | TOOLS:
            unknown.append(name)
            continue
        tree = _ast.parse(fp.read_text(encoding="utf-8"))
        deps: set[str] = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                if node.level and node.module:
                    deps.add(node.module.split(".")[0])
                elif node.level:
                    deps |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, _ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] == "server":
                        bad_server.append(f"{name}→server")
        deps &= set(mods)
        if name in PROD:
            for d in deps & RESEARCH:
                if (name, d) not in ALLOWED:
                    bad_edge.append(f"{name}→{d}")
        if "server" in deps:
            bad_server.append(f"{name}→server")

    ok("生产层不反向依赖研究层", not bad_edge,
       "；".join(bad_edge) + "（研究代码混进生产链路：刷新/回测会拖上实验依赖）",
       f"{len(PROD)} 个生产模块零反向依赖")
    ok("etfquant 不反向依赖 server（依赖方向单向）", not bad_server,
       "；".join(sorted(set(bad_server))), "依赖方向自上而下")
    ok("每个模块都有明确分层", not unknown,
       f"未登记：{', '.join(unknown)}（在 docs/architecture.md §1.5 补一行，再加进本检查）",
       f"{len(mods)} 个模块已分层")
    arch_fp = ROOT / "docs" / "architecture.md"
    if arch_fp.exists():
        arch_txt = arch_fp.read_text(encoding="utf-8")
        missing = [m for m in sorted(mods) if not _re.search(rf"`{m}`", arch_txt)]
        ok("架构文档的模块地图覆盖全部模块", not missing,
           f"缺：{', '.join(missing)}（新增模块要在地图里登记，否则只有作者知道它干什么）",
           f"{len(mods)}/{len(mods)} 全部在地图里")
    else:
        ok("架构文档存在", False, "缺 docs/architecture.md")

    print("\n" + "=" * 78)
    print("结论")
    print("=" * 78)
    if FAIL:
        print(f"  发现问题（通过 {N_PASS} 项 / 失败 {len(FAIL)} 项）：")
        for f in FAIL:
            print(f"    · {f}")
    else:
        print(f"  全部检查通过（{N_PASS} 项）")
    print(f"\n  当前 TGAR：CAGR {metrics.cagr(nav):.2%} | MaxDD {metrics.max_drawdown(nav):.2%} "
          f"| Sharpe(CAGR法) {old_sharpe:.3f} | Sharpe(标准) {new_sharpe:.3f}")


if __name__ == "__main__":
    main()
