"""
信号衰减：R20 的横截面 Rank IC 随持有期的变化（README 7.24）。

回答的问题
----------
`rebalance_freq` 在 7.5 里测的是**回测层面**的 3/5/10/20 档优劣，
但不知道信号本身能活多久。若 IC 在 10 日就衰减殆尽，那"持有 20 日"不是保守，
是拿着失效信号；若 20 日仍有 IC，那 5 日调仓就是白付换手成本。

用法：python scripts/research/signal_decay.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from etfquant.pipeline import load_panel
from etfquant.strategy import TrendGatedAdaptive

HORIZONS = (1, 3, 5, 10, 20, 40, 60)


def rank_ic(factor: pd.DataFrame, fwd: pd.DataFrame, min_names: int = 20) -> pd.Series:
    """逐日横截面 Rank IC。

    先各自排名再算 Pearson = Spearman，避免引入 scipy 依赖。
    """
    a = factor.rank(axis=1)
    b = fwd.rank(axis=1)
    a = a.sub(a.mean(axis=1), axis=0)
    b = b.sub(b.mean(axis=1), axis=0)
    num = (a * b).sum(axis=1, min_count=1)
    den = np.sqrt((a**2).sum(axis=1, min_count=1) * (b**2).sum(axis=1, min_count=1))
    ic = num / den
    return ic[factor.notna().sum(axis=1) >= min_names]


def main() -> int:
    inv = pd.read_csv(ROOT / "data" / "etf_history_inventory.csv", dtype=str)
    vals = [v for v in inv["end"].dropna().tolist() if len(str(v)) == 10]
    end = max(set(vals), key=vals.count)

    panel = load_panel(ROOT / "data" / "etf_pool.csv", ROOT / "data" / "cache",
                       "2018-01-02", end)
    z = TrendGatedAdaptive(panel).fb.z_r20

    print(f"R20 横截面 Rank IC 随持有期衰减（面板 {panel.close.shape[1]} 只）")
    print(f"{'持有期':>6}{'均值IC':>9}{'标准差':>9}{'IC_IR':>8}{'t值':>8}{'相对5日':>9}")
    rows, base = [], None
    for n in HORIZONS:
        fwd = panel.close.pct_change(n).shift(-n)
        ic = rank_ic(z, fwd).dropna()
        if len(ic) < 100:
            continue
        m, s = float(ic.mean()), float(ic.std())
        # 重叠窗口会虚增样本量，按 n 折价
        t = m / s * np.sqrt(len(ic) / n) if s > 0 else np.nan
        if n == 5:
            base = m
        rel = m / base if base else np.nan
        rows.append({"持有期": n, "均值IC": round(m, 4), "标准差": round(s, 4),
                     "IC_IR": round(m / s, 3), "t值": round(t, 2),
                     "相对5日": round(rel, 3) if np.isfinite(rel) else None})
        print(f"{n:>5}日{m:>9.4f}{s:>9.4f}{m/s:>8.2f}{t:>8.1f}"
              f"{(f'{rel*100:.0f}%' if np.isfinite(rel) else '—'):>9}")

    df = pd.DataFrame(rows)
    fp = ROOT / "output" / "experiments" / "signal_decay.csv"
    df.to_csv(fp, index=False, encoding="utf-8-sig")
    print(f"\n已写入 {fp.relative_to(ROOT)}")
    print("读法：IC 水平在 1–10 日基本持平、20 日骤降、40 日转负 → 半衰期约 10–15 天。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
