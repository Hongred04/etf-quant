"""
A/B：元标签（meta-labeling）决定本期仓位系数，不改选股。

判据（**跑之前写定，看到结果不许改**）
--------------------------------------
0. 有效性前置：仓位系数必须在 ≥20% 的期数上真正偏离 1.0，否则实验无意义（等于没改）。
1. 回测层：对**每一种**映射（threshold / linear / downside），要求
   - 2020-2022 夏普 ≥ 基线（容差 1e-9），且
   - 2023-2026 夏普 ≥ 基线，且
   - 全样本最大回撤恶化 ≤ 1pp。
2. 稳健性：至少 2/3 种映射满足第 1 条（只有一种能过 = 映射是挑出来的）。
3. 敏感性：把训练起点从 100 期改到 150 期，通过的那几种映射结论方向不变。

三条全过才考虑采纳；否则记为负结果，`meta_exposure` 保持默认关闭。

为什么值得试
------------
7.10 说 alpha 的 t=1.38，7.12 说"任何确认趋势的补充信息都在反转市付费"。
那么剩下的唯一空间不是"加因子"，而是**同一批标的、更少的手数**：
如果存在"什么时候不该满仓"的可预测性，它应该表现为回撤更浅，而不是选股更准。

设计上的三条自限
----------------
1. 只用 3 个慢变量特征（regime / 基准 20 日波动 / 信号近 6 期胜率）；
2. 走前向验证，逐期用**当日之前**的标签重训（`meta.walk_forward`，有因果性单测）；
3. 仓位只减不增（0.5~1.0），最坏情况是少赚，而不是把杠杆引进来。

用法
----
python -m etfquant research ab_meta
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from etfquant import meta as M  # noqa: E402
from etfquant import metrics  # noqa: E402
from etfquant.backtest import Backtester  # noqa: E402
from etfquant.io_utils import atomic_to_csv  # noqa: E402
from etfquant.pipeline import load_panel  # noqa: E402
from etfquant.strategy import TrendGatedAdaptive  # noqa: E402

OUT = ROOT / "output" / "experiments"
MAIN_START = "2019-01-02"
SUB_A = ("2020-01-02", "2022-12-30")
SUB_B = ("2023-01-02", None)
MODES = ("threshold", "linear", "downside")


def pct(x: float) -> str:
    return "—" if x is None or not np.isfinite(x) else f"{x*100:+.1f}%"


def num(x: float, nd: int = 2) -> str:
    return "—" if x is None or not np.isfinite(x) else f"{x:.{nd}f}"


def perf(nav: pd.Series) -> dict:
    if len(nav) < 60:
        return {"CAGR": float("nan"), "MDD": float("nan"), "夏普": float("nan")}
    return {"CAGR": float(metrics.cagr(nav)), "MDD": float(metrics.max_drawdown(nav)),
            "夏普": float(metrics.sharpe(nav))}


def build_exposure(panel, strat, dates, min_train: int) -> tuple[pd.Series, dict]:
    """算出仓位系数序列 + 诊断信息（样本数、增减分布）。"""
    close = panel.close
    score = strat.fb.z_r20.reindex(dates)
    rets = M.top_basket_return(close, score, dates, top=3, horizon=5)
    labels = (rets > 0).astype(float).where(rets.notna())

    regime = pd.Series({d: strat.regime_at(d) for d in dates}, dtype=float)
    vol = M.bench_vol(close).reindex(dates)
    hit = M.rolling_hit(labels).reindex(dates)
    x = M.build_features(regime, vol, hit)
    y = labels.reindex(x.index)
    prob = M.walk_forward(x, y, min_train=min_train)
    info = {
        "样本": int(len(x)),
        "有预测": int(prob.notna().sum()),
        "标签为正比例": float(y.mean()) if len(y) else float("nan"),
        "prob 中位": float(prob.median()) if prob.notna().any() else float("nan"),
    }
    return prob, info


def main() -> int:
    t0 = time.time()
    panel = load_panel(ROOT / "data" / "etf_pool.csv", ROOT / "data" / "cache",
                       "2018-01-02", "2026-12-31")
    close = panel.close
    strat0 = TrendGatedAdaptive(panel)
    dates = pd.DatetimeIndex([d for d in strat0.rebalance_days() if d >= pd.Timestamp(MAIN_START)])
    bt = Backtester(close, panel.open_)

    base_nav = bt.run(strat0.generate(), start=MAIN_START).nav
    base = {"全": perf(base_nav), "A": perf(base_nav.loc[SUB_A[0]:SUB_A[1]]),
            "B": perf(base_nav.loc[SUB_B[0]:])}
    print(f"[1/4] 基线：全样本 {pct(base['全']['CAGR'])} / MDD {pct(base['全']['MDD'])} / "
          f"夏普 {num(base['全']['夏普'])}   20-22 {num(base['A']['夏普'])}   "
          f"23-26 {num(base['B']['夏普'])}")

    print("[2/4] 建标签与特征（3 个慢变量，走前向验证）")
    prob, info = build_exposure(panel, strat0, dates, min_train=100)
    print(f"      样本 {info['样本']}，有预测 {info['有预测']}，"
          f"标签正比例 {info['标签为正比例']*100:.0f}%，prob 中位 {num(info['prob 中位'], 3)}")

    print("[3/4] 三种映射各跑一次")
    rows = []
    for mode in MODES:
        k = M.to_exposure(prob, mode).reindex(dates).fillna(1.0)
        changed = float((k < 1 - 1e-9).mean())
        strat = TrendGatedAdaptive(panel, meta_exposure=k)
        nav = bt.run(strat.generate(), start=MAIN_START).nav
        full, a, b = perf(nav), perf(nav.loc[SUB_A[0]:SUB_A[1]]), perf(nav.loc[SUB_B[0]:])
        rows.append({"映射": mode, "减仓比例": changed, "CAGR": full["CAGR"], "MDD": full["MDD"],
                     "夏普": full["夏普"], "夏普_A": a["夏普"], "CAGR_A": a["CAGR"],
                     "夏普_B": b["夏普"], "CAGR_B": b["CAGR"]})
        print(f"      {mode:<10} 减仓 {changed*100:>3.0f}% 期  全样本 {pct(full['CAGR'])} / "
              f"MDD {pct(full['MDD'])} / 夏普 {num(full['夏普'])}   "
              f"20-22 {num(a['夏普'])}  23-26 {num(b['夏普'])}")

    # ---------------- 判据 ----------------
    print("[4/4] 判据核对（跑之前写定的三条）")
    ok_nonnull = all(r["减仓比例"] >= 0.20 for r in rows)
    print(f"      ⓿ 有效性：{'过' if ok_nonnull else '不过'}（要求每种映射都真的减过仓）")

    for r in rows:
        r["过"] = bool(r["夏普_A"] >= base["A"]["夏普"] - 1e-9
                       and r["夏普_B"] >= base["B"]["夏普"] - 1e-9
                       and r["MDD"] >= base["全"]["MDD"] - 0.01)
        print(f"      ① {r['映射']:<10} {'过' if r['过'] else '不过'}  "
              f"（20-22 {num(r['夏普_A'])} vs {num(base['A']['夏普'])}；"
              f"23-26 {num(r['夏普_B'])} vs {num(base['B']['夏普'])}；"
              f"MDD {pct(r['MDD'])} vs {pct(base['全']['MDD'])}）")
    n_pass = sum(1 for r in rows if r["过"])
    print(f"      ② 稳健性：{n_pass}/3 种映射通过 → {'过' if n_pass >= 2 else '不过'}")

    # 敏感性：训练起点 100 → 150
    prob2, _ = build_exposure(panel, strat0, dates, min_train=150)
    sens = []
    for r in rows:
        if not r["过"]:
            continue
        k2 = M.to_exposure(prob2, r["映射"]).reindex(dates).fillna(1.0)
        nav2 = bt.run(TrendGatedAdaptive(panel, meta_exposure=k2).generate(), start=MAIN_START).nav
        a2, b2 = perf(nav2.loc[SUB_A[0]:SUB_A[1]]), perf(nav2.loc[SUB_B[0]:])
        keep = bool(a2["夏普"] >= base["A"]["夏普"] - 1e-9 and b2["夏普"] >= base["B"]["夏普"] - 1e-9)
        sens.append(keep)
        print(f"      ③ {r['映射']:<10} 训练 150 期起：20-22 {num(a2['夏普'])}、"
              f"23-26 {num(b2['夏普'])} → {'仍过' if keep else '不再过'}")
    ok_sens = bool(sens) and all(sens)
    print(f"      ③ 敏感性：{'过' if ok_sens else '不过（无通过项或训练窗口一变就失效）'}")

    passed = ok_nonnull and n_pass >= 2 and ok_sens
    OUT.mkdir(parents=True, exist_ok=True)
    atomic_to_csv(pd.DataFrame(rows), OUT / "ab_meta.csv", index=False)
    atomic_to_csv(pd.DataFrame([{**{"min_train": 100}, **info}]), OUT / "ab_meta_diag.csv", index=False)
    print(f"\n[OK] 采纳：{'是' if passed else '否（记为负结果，meta_exposure 保持默认关闭）'}")
    print(f"     产物 output/experiments/ab_meta.csv   耗时 {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
