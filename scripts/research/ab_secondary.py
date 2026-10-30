"""
A/B：52 周高点距离作为第二排序键（只在近平局处生效）。

判据（**跑之前写定，看到结果不许改**）
--------------------------------------
1. 信号层：5 日与 20 日两个窗口的 `top3 − 全池` Newey-West t 值，两个权重都要
   **同时高于基线**；且 2019-2022 段的均值不下降（不允许"只靠最近三年"）。
2. 回测层：2020-2022 与 2023-2026 两段的夏普**同时 ≥ 基线**（容差 1e-9），
   全样本最大回撤恶化不超过 1pp。
3. 邻域：w=0.05 与 w=0.10 方向一致（同为正或同为负）。
三条全过才考虑采纳；否则记为负结果，`secondary` 开关保持默认关闭。

为什么是"第二排序键"而不是"第二个因子"
--------------------------------------
w=0.05 时，z 值 ±2 的贡献最多 ±0.1，只能翻动 z20 差 < 0.1 的配对。
这不是保守，是 7.1/7.6/7.8 三次失败的直接结论：任何有实质权重的第二因子都会
稀释"尽快换到最强"，然后两段子区间反号。

用法
----
python -m etfquant research ab_secondary
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from etfquant import diagnostics as D  # noqa: E402
from etfquant import metrics  # noqa: E402
from etfquant import secondary as S  # noqa: E402
from etfquant.backtest import Backtester  # noqa: E402
from etfquant.io_utils import atomic_to_csv  # noqa: E402
from etfquant.pipeline import load_panel  # noqa: E402
from etfquant.strategy import TrendGatedAdaptive  # noqa: E402

OUT = ROOT / "output" / "experiments"
MAIN_START = "2019-01-02"
SUB_A = ("2020-01-02", "2022-12-30")
SUB_B = ("2023-01-02", None)
SIG_A = ("2019-01-01", "2022-12-31")
SIG_B = ("2023-01-01", "2026-12-31")
WEIGHTS = (0.0, 0.05, 0.10)


def pct(x: float) -> str:
    return "—" if x is None or not np.isfinite(x) else f"{x*100:+.1f}%"


def num(x: float) -> str:
    return "—" if x is None or not np.isfinite(x) else f"{x:.2f}"


def window(nav: pd.Series, start: str, end: str | None) -> pd.Series:
    seg = nav.loc[start:] if end is None else nav.loc[start:end]
    return seg.dropna()


def perf(nav: pd.Series) -> dict:
    if len(nav) < 60:
        return {"CAGR": float("nan"), "MDD": float("nan"), "夏普": float("nan")}
    return {
        "CAGR": float(metrics.cagr(nav)),
        "MDD": float(metrics.max_drawdown(nav)),
        "夏普": float(metrics.sharpe(nav)),
    }


def breadth_degeneracy(close: pd.DataFrame, groups: pd.Series) -> dict:
    """
    同组内 ETF 的日收益相关性 vs 全池相关性。

    若同组相关性接近 1，"同行业多少只在涨"就退化成"这只自己涨没涨"，
    与 R20 共线，作为横截面因子的信息量接近于零——这就是不接它的原因。
    """
    ret = close.pct_change(fill_method=None).loc["2020-01-02":]
    corr = ret.corr(min_periods=120)
    # Panel.meta 是 DataFrame（index=代码，含 category 列）；允许直接传 Series
    g = groups["category"] if isinstance(groups, pd.DataFrame) else pd.Series(groups)
    g = g.reindex(close.columns)
    same, diff = [], []
    cols = list(close.columns)
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            v = corr.at[a, b]
            if not np.isfinite(v):
                continue
            (same if (g.get(a) == g.get(b) and pd.notna(g.get(a))) else diff).append(v)
    return {
        "同类别": float(np.mean(same)) if same else float("nan"),
        "跨类别": float(np.mean(diff)) if diff else float("nan"),
        "n_same": len(same), "n_diff": len(diff),
    }


def main() -> int:
    t0 = time.time()
    panel = load_panel(ROOT / "data" / "etf_pool.csv", ROOT / "data" / "cache",
                       "2018-01-02", "2026-12-31")
    close = panel.close
    strat0 = TrendGatedAdaptive(panel)
    dates = pd.DatetimeIndex([d for d in strat0.rebalance_days() if d >= pd.Timestamp(MAIN_START)])
    z20 = strat0.fb.z_r20.reindex(dates)
    z52 = S.score_frame(close, dates)

    # 共线性：如果 52 周高点与 R20 几乎同一个东西，那这个实验本身就没意义
    cors = []
    for d in dates:
        x, y = z20.loc[d], z52.loc[d]
        ok = x.notna() & y.notna()
        if int(ok.sum()) > 20:
            cors.append(float(np.corrcoef(x[ok], y[ok])[0, 1]))
    rho = float(np.nanmedian(cors)) if cors else float("nan")
    print(f"[1/4] 共线性：z(52周高点) 与 z(R20) 的横截面相关中位 {rho:.2f}（n={len(cors)} 期）")

    # ---------------- 信号层 ----------------
    print("[2/4] 信号层：5d / 20d 的 top3−全池 t 值")
    fwd = {h: D.fwd_return(close, h).reindex(dates) for h in (5, 20)}
    sig_rows = []
    for w in WEIGHTS:
        score = z20 if w == 0 else S.blend_secondary(z20, z52, w)
        row = {"w": w}
        for h in (5, 20):
            ser = D.tail_spread_series(score.dropna(how="all"), fwd[h], dates, top=3, min_names=20)
            row[f"t{h}"] = float(D.newey_west_t(ser))
            row[f"mean{h}"] = float(ser.mean() * 1e4)
            for tag, (a, b) in (("A", SIG_A), ("B", SIG_B)):
                seg = ser.loc[a:b]
                row[f"mean{h}_{tag}"] = float(seg.mean() * 1e4) if len(seg) else float("nan")
        sig_rows.append(row)
        print(f"      w={w:.2f}  5d t={row['t5']:+.2f}（{row['mean5']:+.1f}bp）  "
              f"20d t={row['t20']:+.2f}（{row['mean20']:+.1f}bp）  "
              f"5d 分段 {row['mean5_A']:+.1f}/{row['mean5_B']:+.1f}bp")

    # ---------------- 回测层 ----------------
    print("[3/4] 回测层：3 个权重各跑一次全样本，再切子区间")
    bt = Backtester(close, panel.open_)
    bt_rows = []
    for w in WEIGHTS:
        strat = TrendGatedAdaptive(panel, secondary=z52 if w else None, secondary_weight=w)
        nav = bt.run(strat.generate(), start=MAIN_START).nav
        full = perf(nav)
        a = perf(window(nav, *SUB_A))
        b = perf(window(nav, *SUB_B))
        bt_rows.append({"w": w, "CAGR": full["CAGR"], "MDD": full["MDD"], "夏普": full["夏普"],
                        "CAGR_A": a["CAGR"], "夏普_A": a["夏普"],
                        "CAGR_B": b["CAGR"], "夏普_B": b["夏普"]})
        print(f"      w={w:.2f}  全样本 {pct(full['CAGR'])} / MDD {pct(full['MDD'])} / "
              f"夏普 {num(full['夏普'])}   20-22 {pct(a['CAGR'])}（夏普 {num(a['夏普'])}）   "
              f"23-26 {pct(b['CAGR'])}（夏普 {num(b['夏普'])}）")

    # ---------------- 判据 ----------------
    print("[4/4] 判据核对（跑之前写定的三条）")
    base_sig, base_bt = sig_rows[0], bt_rows[0]
    verdict = {}
    for row, brow in zip(sig_rows[1:], bt_rows[1:]):
        c1 = (row["t5"] > base_sig["t5"] and row["t20"] > base_sig["t20"]
              and row["mean5_A"] >= base_sig["mean5_A"])
        c2 = (brow["夏普_A"] >= base_bt["夏普_A"] - 1e-9
              and brow["夏普_B"] >= base_bt["夏普_B"] - 1e-9
              and brow["MDD"] >= base_bt["MDD"] - 0.01)
        verdict[row["w"]] = {"信号层": bool(c1), "回测层": bool(c2)}
        print(f"      w={row['w']:.2f}  ① 信号层 {'过' if c1 else '不过'}   "
              f"② 回测层 {'过' if c2 else '不过'}")
    same_dir = all(v == verdict[WEIGHTS[1]] for v in verdict.values())
    print(f"      ③ 邻域一致性（0.05 与 0.10 方向一致）：{'过' if same_dir else '不过'}")
    passed = [w for w, v in verdict.items() if v["信号层"] and v["回测层"]] if same_dir else []

    # ---------------- 扩散度验证 ----------------
    print("\n[附] 行业扩散度是否退化：同类别 vs 跨类别的日收益相关性")
    deg = breadth_degeneracy(close, panel.meta["category"] if "category" in panel.meta else pd.Series(dtype=object))
    print(f"      同类别 {deg['同类别']:.3f}（{deg['n_same']} 对）  "
          f"跨类别 {deg['跨类别']:.3f}（{deg['n_diff']} 对）")
    if np.isfinite(deg["同类别"]) and deg["同类别"] > 0.9:
        print("      → 同类别相关性 >0.9：breadth 退化为二值量，与 R20 共线，不接入")

    OUT.mkdir(parents=True, exist_ok=True)
    atomic_to_csv(pd.DataFrame(sig_rows), OUT / "ab_secondary_signal.csv", index=False)
    atomic_to_csv(pd.DataFrame(bt_rows), OUT / "ab_secondary_backtest.csv", index=False)
    print(f"\n[OK] 采纳权重：{passed if passed else '无（记为负结果，开关默认关闭）'}")
    print(f"     产物 output/experiments/ab_secondary_{{signal,backtest}}.csv   "
          f"耗时 {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
