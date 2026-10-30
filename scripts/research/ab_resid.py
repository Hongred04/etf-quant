"""
A/B：把残差动量接到 5 日框架上，到底加不加分？

问题从哪来
----------
README 7.6 复现了外部那套「普通动量 + 残差动量」月度策略，结论是"结构不行，但残差因子
本身是那套里最好的单因子"。于是剩下一个具体问题：**换成我们自己的 5 日框架，它还加分吗？**

四个必须分开看的用法（不是一次只测一个"最好的"）
------------------------------------------------
1. 混合打分：`(1-w)·z(R20) + w·z(残差)`，w = 0.2 / 0.3 / 0.5；
2. 单独当主因子：它能不能替代 R20；
3. 质量过滤：主排序仍是 R20，只把残差为负的剔掉（这把"打分"和"准入"分开）。

判定标准（先定好，避免看到结果再找理由）
------------------------------------
* 主窗口 2020-01 起（残差要 ~200 个交易日预热，统一起点才可比）；
* **必须先看两个子区间同向**：2020-2022 与 2023-2026 都改善，才算候选；
  只有一个区间改善 = 某段行情的运气，直接否掉；
* 通过后再看参数邻域（残差窗口 60 / 120 / 250）是否稳定——单点最优不算数。

用法：python scripts/research/ab_resid.py
产物：output/experiments/ab_resid.csv
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from etfquant import metrics  # noqa: E402
from etfquant import resid_mom as R  # noqa: E402
from etfquant.backtest import Backtester  # noqa: E402
from etfquant.io_utils import atomic_to_csv  # noqa: E402
from etfquant.pipeline import benchmark_series, load_panel  # noqa: E402
from etfquant.strategy import TrendGatedAdaptive  # noqa: E402

PANEL_START, END = "2018-01-02", "2026-12-31"
FACTORS = ["sh510300", "sh511010", "sh518880"]   # 大盘 / 债券 / 黄金

W_START = "2020-01-02"        # 残差预热后的统一起点
W2023 = "2023-01-03"
SUB_A = ("2020-01-02", "2022-12-31")
SUB_B = ("2023-01-03", None)

VARIANTS = [
    ("基线 R20（现状）", dict(score_mode="mom20")),
    ("+残差 20%（混合）", dict(score_mode="r20+resid", resid_weight=0.20)),
    ("+残差 30%（混合）", dict(score_mode="r20+resid", resid_weight=0.30)),
    ("+残差 50%（混合）", dict(score_mode="r20+resid", resid_weight=0.50)),
    ("仅残差（替代 R20）", dict(score_mode="resid")),
    ("R20 + 残差>0 过滤", dict(score_mode="r20+resid_filter", resid_min=0.0)),
]


def fmt(v, d=1) -> str:
    return "—" if v is None or v != v else f"{v * 100:.{d}f}%"


def run(panel, bt, bench, sig, start, end=None) -> dict:
    r = bt.run(sig, start=start, end=end)
    b = bench.loc[r.nav.index[0]:r.nav.index[-1]]
    st = metrics.summarize(r.nav, b)
    return {
        "CAGR": float(st["年化收益(CAGR)"]), "夏普": float(st["夏普比率"]),
        "回撤": float(st["最大回撤"]), "卡玛": float(st["卡玛比率"]),
        "超额": float(st["超额收益"]), "换手": float(r.stats.get("年均换手(单边)", float("nan"))),
        "起点": str(r.nav.index[0].date()),
    }


def main() -> int:
    print("[1/4] 加载面板…")
    panel = load_panel(ROOT / "data" / "etf_pool.csv", ROOT / "data" / "cache", PANEL_START, END)
    close = panel.close
    bt = Backtester(close, panel.open_)
    bench = benchmark_series(close, "sh510300")
    base_strat = TrendGatedAdaptive(panel)
    dates = base_strat.rebalance_days()
    factors = R.factor_matrix(close, FACTORS)
    print(f"      {close.shape[1]} 只 · 调仓日 {len(dates)} 个")

    print("[2/4] 计算残差因子（252 日回归 / 120 日残差和）…")
    t0 = time.time()
    resid = R.score_frame(close, factors, dates, lookback=252, window=120, min_obs=200)
    first = resid.dropna(how="all").index[0]
    print(f"      完成 {time.time()-t0:.1f}s · 首个有效调仓日 {first.date()} · "
          f"当日可用标的 {int(resid.loc[first].notna().sum())}")

    print("[3/4] 跑变体…")
    rows = []
    for label, kw in VARIANTS:
        t = time.time()
        strat = TrendGatedAdaptive(panel, resid=resid, **kw)
        sig = strat.generate()
        full = run(panel, bt, bench, sig, W_START)
        y23 = run(panel, bt, bench, sig, W2023)
        a = run(panel, bt, bench, sig, *SUB_A)
        b = run(panel, bt, bench, sig, *SUB_B)
        rows.append({
            "变体": label, "起点": full["起点"],
            "CAGR": full["CAGR"], "夏普": full["夏普"], "回撤": full["回撤"],
            "卡玛": full["卡玛"], "超额": full["超额"], "换手": full["换手"],
            "2020-22": a["CAGR"], "2023-26": b["CAGR"],
            "CAGR_2023起": y23["CAGR"], "回撤_2023起": y23["回撤"],
        })
        print(f"      {label:<22} CAGR {fmt(full['CAGR'])}  回撤 {fmt(full['回撤'])}  "
              f"子区间 {fmt(a['CAGR'])} / {fmt(b['CAGR'])}  ({time.time()-t:.0f}s)")

    df = pd.DataFrame(rows)
    atomic_to_csv(df, ROOT / "output" / "experiments" / "ab_resid.csv", index=False, encoding="utf-8-sig")

    print(f"\n=== 主窗口 {W_START} 起 ===")
    print(f"{'变体':<24}{'CAGR':>8}{'夏普':>7}{'回撤':>9}{'卡玛':>7}{'超额':>9}{'换手':>7}"
          f"{'20-22':>8}{'23-26':>8}")
    for r in df.to_dict("records"):
        print(f"{r['变体']:<24}{fmt(r['CAGR']):>8}{r['夏普']:>7.2f}{fmt(r['回撤']):>9}"
              f"{r['卡玛']:>7.2f}{fmt(r['超额']):>9}{r['换手']:>7.1f}"
              f"{fmt(r['2020-22']):>8}{fmt(r['2023-26']):>8}")

    # ---------- 参数邻域：唯一看起来有改善的权重，必须在邻域里也赢才算数 ----------
    # 判据：同一权重下，窗口 60/120/250 三个取值要**方向一致**；只有一个窗口赢 = 尖峰 = 运气。
    base = df.to_dict("records")[0]
    print("\n[4/4] 参数邻域：权重 × 残差窗口（基线两段 "
          f"{fmt(base['2020-22'])} / {fmt(base['2023-26'])}）")
    grid_rows = []
    for w in (0.2, 0.3):
        for win in (60, 120, 250):
            fr = R.score_frame(close, factors, dates, lookback=252, window=win, min_obs=200)
            strat = TrendGatedAdaptive(panel, resid=fr, score_mode="r20+resid", resid_weight=w)
            sig = strat.generate()
            full = run(panel, bt, bench, sig, W_START)
            a = run(panel, bt, bench, sig, *SUB_A)
            b = run(panel, bt, bench, sig, *SUB_B)
            both = a["CAGR"] > base["2020-22"] and b["CAGR"] > base["2023-26"]
            grid_rows.append({
                "权重": w, "窗口": win, "CAGR": full["CAGR"], "回撤": full["回撤"],
                "2020-22": a["CAGR"], "2023-26": b["CAGR"], "两段同向改善": bool(both),
            })
            print(f"      w={w:.1f} window={win:>3}  CAGR {fmt(full['CAGR'])}  "
                  f"回撤 {fmt(full['回撤'])}  子区间 {fmt(a['CAGR'])} / {fmt(b['CAGR'])}  "
                  f"→ {'两段同向改善' if both else '不成立'}")
    grid = pd.DataFrame(grid_rows)
    atomic_to_csv(grid, ROOT / "output" / "experiments" / "ab_resid_grid.csv", index=False, encoding="utf-8-sig")
    print(f"      两段同向改善的组合：{int(grid['两段同向改善'].sum())}/{len(grid)}")

    print(f"\n[OK] 已写 output/experiments/ab_resid.csv + ab_resid_grid.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
