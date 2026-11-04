"""
参数的置信区间：块自助法（README 7.25）。

与 7.15 的分工
--------------
- DSR（7.15）回答"17% 里有多少是挑参数挑出来的"，是**多重比较**折价；
- 本脚本回答"换一段历史，这个数字会是多少"，是**抽样不确定性**。

用 block bootstrap：按 20 日切块整块重采样，保留日收益的时序自相关
（简单 iid 重采样会把波动率低估，得到过窄的区间）。

用法：python scripts/research/bootstrap_ci.py --block 20 --n 2000
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from etfquant.io_utils import atomic_to_csv


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--block", type=int, default=20)
    ap.add_argument("--n", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    nav_fp = ROOT / "output" / "nav_main.csv"
    nav = pd.read_csv(nav_fp, index_col=0, parse_dates=True)["S4-TGAR(自研)"].dropna()
    r = nav.pct_change().dropna()
    vals = r.to_numpy()
    nb = len(r) // args.block
    if nb < 10:
        print("[ERR] 样本太短，无法切块")
        return 1

    rng = np.random.default_rng(args.seed)
    cagrs, mdds, calmars = [], [], []
    for _ in range(args.n):
        pick = rng.integers(0, nb, nb)
        s = np.concatenate([vals[j * args.block:(j + 1) * args.block] for j in pick])
        cum = np.cumprod(1.0 + s)
        cagr = float(cum[-1] ** (242 / len(s)) - 1)
        path = np.concatenate([[1.0], cum])
        mdd = float((path / np.maximum.accumulate(path) - 1).min())
        cagrs.append(cagr)
        mdds.append(mdd)
        calmars.append(cagr / abs(mdd) if mdd < 0 else np.nan)

    c, m, d = map(np.array, (cagrs, calmars, mdds))
    point = float(nav.iloc[-1] ** (365.25 / max(1, (nav.index[-1] - nav.index[0]).days)) - 1)
    # 点估计全部现算。原来卡玛写死 0.588、回撤写死 -0.2886——那是旧回测器的数；
    # 修完记账 bug 后重跑，CSV 里就会出现「点估计 12.4%、卡玛 0.588」的自相矛盾。
    point_mdd = float((nav / nav.cummax() - 1).min())
    point_calmar = point / abs(point_mdd) if point_mdd < 0 else float("nan")
    # "跑不赢买指数"的基准也现算：沪深300 同区间几何年化。原来写死 6.94%。
    # 注意 nav_main.csv 的基准列锚在 2018（文件内首值 ≈0.69，不是 1.0，
    # 见 AGENTS.md §7 的"归一化净值当价格"陷阱），必须用首尾比值而不是末值。
    bench = pd.read_csv(nav_fp, index_col=0, parse_dates=True)["基准-沪深300"].dropna()
    bench_cagr = float(
        (bench.iloc[-1] / bench.iloc[0])
        ** (365.25 / max(1, (bench.index[-1] - bench.index[0]).days)) - 1
    )

    def ci(x: np.ndarray) -> tuple[float, float]:
        x = x[np.isfinite(x)]
        return float(np.percentile(x, 2.5)), float(np.percentile(x, 97.5))

    print(f"样本 {len(r)} 天 | 块长 {args.block} | 重采样 {args.n} | 种子 {args.seed}")
    print()
    print(f"{'指标':<10}{'点估计':>10}{'95% 区间':>24}")
    lo, hi = ci(c)
    print(f"{'CAGR':<10}{point*100:>9.2f}%   [{lo*100:6.2f}%, {hi*100:6.2f}%]   宽 {(hi-lo)*100:.1f}pp")
    lo2, hi2 = ci(m)
    print(f"{'卡玛':<10}{point_calmar:>9.3f}   [{lo2:6.3f}, {hi2:6.3f}]")
    lo3, hi3 = ci(d)
    print(f"{'最大回撤':<10}{point_mdd*100:>8.2f}%   [{lo3*100:6.2f}%, {hi3*100:6.2f}%]")
    print()
    print(f"CAGR < 0 的概率            {(c < 0).mean() * 100:5.1f}%")
    print(f"CAGR < {bench_cagr*100:.2f}%（沪深300 同区间年化） {(c < bench_cagr).mean() * 100:5.1f}%")

    df = pd.DataFrame({
        "指标": ["CAGR", "卡玛", "最大回撤"],
        "点估计": [round(point, 4), round(point_calmar, 4), round(point_mdd, 4)],
        "P2.5": [round(lo, 4), round(lo2, 4), round(lo3, 4)],
        "P97.5": [round(hi, 4), round(hi2, 4), round(hi3, 4)],
        "P(CAGR<0)": [round(float((c < 0).mean()), 4), None, None],
        "P(CAGR<基准)": [round(float((c < bench_cagr).mean()), 4), None, None],
    })
    fp = ROOT / "output" / "experiments" / "bootstrap_ci.csv"
    atomic_to_csv(df, fp, index=False, encoding="utf-8-sig")
    print(f"\n已写入 {fp.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
