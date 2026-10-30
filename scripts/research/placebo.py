"""
随机对照：同样候选池、同样闸门、同样成本，只把"选 3 只"换成随机抽签。

判据（跑之前写定）
------------------
* 主检验：随机组**净收益 CAGR** 分布中，真实策略 ≥95 分位才算"选股有贡献"；
  50~95 分位 = "有但不显著"；<50 分位 = 不如随机。
* 同时给毛收益分位（剔除成本差异后的纯选股能力）。
* 抽 300 组；不允许重复、必须落在当日可交易候选集内。

成本为什么用"年换手 × 费率"的标量折算
--------------------------------------
7.10 已确认：每日持仓快照的逐日变化大部分是价格漂移，不是成交，
用它算换手会把成本放大十倍。所以这里直接用 `perf_main.csv` 的年换手（实际约 24 倍），
随机组按"每期全部换手"（2 × 75 期 ≈ 150 倍）折算，两边各自承担自己的成本。

用法
----
python -m etfquant research placebo
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from etfquant import placebo as P  # noqa: E402
from etfquant.io_utils import atomic_to_csv  # noqa: E402
from etfquant.pipeline import load_panel  # noqa: E402
from etfquant.strategy import TrendGatedAdaptive  # noqa: E402

OUT = ROOT / "output" / "experiments"
MAIN_START = "2019-01-02"
N_DRAWS = 300
COST = 0.001          # 单边 0.10%


def main() -> int:
    t0 = time.time()
    panel = load_panel(ROOT / "data" / "etf_pool.csv", ROOT / "data" / "cache",
                       "2018-01-02", "2026-12-31")
    close, ret = panel.close, panel.close.pct_change(fill_method=None)
    cat = panel.meta["category"] if "category" in panel.meta else pd.Series(dtype=object)
    strat = TrendGatedAdaptive(panel)
    dates = pd.DatetimeIndex([d for d in strat.rebalance_days() if d >= pd.Timestamp(MAIN_START)])

    # ---------- 候选集：与策略同口径（可交易 + 非债券） ----------
    # 候选集必须与策略**完全同口径**，包括趋势与量比过滤。
    # 第一版漏了这两道过滤，于是随机组是从"全部可交易标的"里抽，
    # 而实际组是从"已在 60 日线上方、量比正常"的标的里抽——
    # 那测的不是"选哪 3 只"，而是"要不要做趋势过滤"，差距被夸大成 14pp。
    cands: dict[pd.Timestamp, list[str]] = {}
    n_trend = []
    for d in dates:
        elig = strat.eligible_at(d)
        nonbond = pd.Series([cat.get(t) != "债券" for t in elig.index], index=elig.index)
        liq = strat.liquid_at(d, elig & nonbond)
        above = strat.fb.above60.loc[d].reindex(liq.index).fillna(False)
        vr = strat.fb.vr.loc[d].reindex(liq.index)
        cand = liq & (vr.fillna(1.0) < strat.max_vr) & above if strat.require_trend else liq & (vr.fillna(1.0) < strat.max_vr)
        cols = list(cand[cand].index)
        if len(cols) < strat.top_n:            # 与策略一致的兜底
            cols = list(liq[liq].index)
        cands[d] = cols
        n_trend.append(len(cols))
    n_c = pd.Series(n_trend, index=dates)
    print(f"[1/4] 候选集（含趋势/量比过滤）：{len(dates)} 个调仓日，中位 {int(n_c.median())} 只"
          f"（{int(n_c.min())}~{int(n_c.max())}）")

    # ---------- 实际权益腿（去掉债券腿）与敞口 ----------
    w = pd.read_csv(OUT.parent / "weights_s4_tgar_main.csv", index_col=0, parse_dates=True)
    bond_cols = [c for c in w.columns if cat.get(c) == "债券"]
    eq_w = w.drop(columns=bond_cols, errors="ignore")
    exposure = eq_w.sum(axis=1).reindex(ret.index).ffill().fillna(0.0)
    eq_norm = eq_w.div(eq_w.sum(axis=1).replace(0.0, np.nan), axis=0)
    actual_eq = (eq_norm * ret.reindex(columns=eq_w.columns)).sum(axis=1)
    act_gross = (actual_eq * exposure).reindex(ret.loc[MAIN_START:].index).fillna(0.0)

    perf = pd.read_csv(OUT.parent / "perf_main.csv", index_col=0)
    s4 = perf[perf["策略"].astype(str).str.startswith("S4")].iloc[0]
    act_turn = float(s4["年均换手(单边)"])
    years = len(act_gross) / 252.0
    rand_turn = 2.0 * len(dates) / years          # 每期全换手
    print(f"[2/4] 权益敞口均值 {exposure.loc[MAIN_START:].mean():.2f}；"
          f"实际年换手 {act_turn:.1f} 倍（成本 {act_turn*COST*100:.2f}%/年），"
          f"随机组 {rand_turn:.0f} 倍（{rand_turn*COST*100:.1f}%/年）")

    # ---------- 随机抽签 ----------
    leg = P.random_leg_returns(ret.loc[MAIN_START:], cands, top=3, n_draws=N_DRAWS, seed=20261030)
    expo = exposure.reindex(leg.index).fillna(0.0)
    rand_gross = leg.mul(expo, axis=0)
    print(f"[3/4] 随机 {N_DRAWS} 组已生成（{time.time()-t0:.0f}s）")

    # ---------- 分位 ----------
    g = np.array([P.cagr_of(rand_gross[c]) for c in rand_gross.columns])
    a_g = P.cagr_of(act_gross)
    g_net = g - rand_turn * COST
    a_n = a_g - act_turn * COST
    sharpe = np.array([rand_gross[c].mean() / rand_gross[c].std() * np.sqrt(252)
                       for c in rand_gross.columns])
    act_sharpe = act_gross.mean() / act_gross.std() * np.sqrt(252)

    print("[4/4] 结果（2019-01 ~ 最新，权益腿 × 闸门）")
    print(f"      实际：毛 {a_g*100:5.1f}%  净 {a_n*100:5.1f}%  夏普 {act_sharpe:.2f}")
    for name, arr, val in (("毛", g, a_g), ("净", g_net, a_n)):
        q5, q50, q95 = np.nanpercentile(arr, [5, 50, 95])
        print(f"      随机{name}：p5 {q5*100:5.1f}%  中位 {q50*100:5.1f}%  p95 {q95*100:5.1f}%"
              f"   → 实际分位 {P.percentile_of(val, arr):.0f}%，"
              f"随机 ≥ 实际 {(arr >= val).mean()*100:.1f}%")
    print(f"      随机夏普：中位 {np.nanmedian(sharpe):.2f}  p95 {np.nanpercentile(sharpe, 95):.2f}"
          f"   实际 {act_sharpe:.2f} → 分位 {P.percentile_of(act_sharpe, sharpe):.0f}%")

    OUT.mkdir(parents=True, exist_ok=True)
    atomic_to_csv(pd.DataFrame({"draw": np.arange(N_DRAWS), "gross_cagr": g,
                                "net_cagr": g_net, "gross_sharpe": sharpe}),
                  OUT / "placebo_random.csv", index=False)
    atomic_to_csv(pd.DataFrame([{
        "actual_gross": a_g, "actual_net": a_n,
        "pct_gross": P.percentile_of(a_g, g), "pct_net": P.percentile_of(a_n, g_net),
        "pct_sharpe": P.percentile_of(act_sharpe, sharpe), "actual_sharpe": act_sharpe,
        "n_draws": N_DRAWS, "n_dates": len(dates), "median_candidates": float(n_c.median()),
        "actual_turnover": act_turn, "random_turnover": rand_turn,
    }]), OUT / "placebo_summary.csv", index=False)
    print(f"      产物 output/experiments/placebo_{{random,summary}}.csv   耗时 {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
