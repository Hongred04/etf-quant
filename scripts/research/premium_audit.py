"""
场内溢价审计：策略实际买入/卖出时，付了多少溢价？

背景
----
`scripts/research/cb_premium_sensitivity.py` 只能在"假设 X%/边"下做压力测试，
因为当时拿不到净值。现在有了 `data/etf_nav_history.csv`（同花顺源，见
`scripts/download_etf_nav.py`），可以直接量出真实溢价。

判定口径
--------
溢价率 = 收盘价 / 单位净值 − 1。加仓日溢价 − 减仓日溢价 ≈ 这笔交易的溢价损耗。
注意：**分红会伪造折价**——境内 ETF 在除权日价格下跌、单位净值不含已分红部分，
会算出 −5% 甚至更低的假折价（510300 在 2024-06-20 是 −5.96%）。所以本脚本
把跨境与非跨境分开统计，非跨境那一列的中位数才有参考意义，均值不可用。

用法：python scripts/research/premium_audit.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from etfquant.io_utils import atomic_to_csv
from etfquant.pipeline import load_panel


def main() -> int:
    p = load_panel(ROOT / "data" / "etf_pool.csv", ROOT / "data" / "cache",
                   "2021-01-01", "2026-10-30")
    w = pd.read_csv(ROOT / "output" / "weights_s4_tgar_main.csv",
                    index_col=0, parse_dates=True).fillna(0)
    w = w.reindex(p.close.index).ffill().fillna(0)

    nav = pd.read_csv(ROOT / "data" / "etf_nav_history.csv", dtype={"ticker": str})
    nav["date"] = pd.to_datetime(nav["date"])
    nav = nav.pivot(index="date", columns="ticker", values="nav")
    nav.columns = [("sh" if c[0] in "56" else "sz") + c for c in nav.columns]
    nav = nav.reindex(p.close.index)

    prem = p.close / nav - 1.0
    print(f"溢价矩阵覆盖：{prem.notna().mean().mean():.1%}"
          f"（{nav.notna().sum().sum():,} 个净值点）")

    cb = {t for t in p.close.columns
          if "跨境" in str(p.meta["category"].get(t, ""))}
    groups = {"跨境": sorted(cb),
              "非跨境": [c for c in p.close.columns if c not in cb]}
    dw = w.diff().fillna(w)

    rows = []
    print()
    print(f"{'组':<8}{'方向':<6}{'n':>6}{'中位':>9}{'均值':>9}{'P75':>9}{'P90':>9}")
    for name, cols in groups.items():
        for side, mask in (("加仓", dw > 1e-6), ("减仓", dw < -1e-6)):
            v = prem.where(mask, np.nan)[cols].stack().dropna()
            if v.empty:
                continue
            rows.append({
                "组": name, "方向": side, "样本": len(v),
                "中位": round(float(v.median()), 4), "均值": round(float(v.mean()), 4),
                "P75": round(float(v.quantile(.75)), 4), "P90": round(float(v.quantile(.90)), 4),
                "P99": round(float(v.quantile(.99)), 4),
            })
            r = rows[-1]
            print(f"{name:<8}{side:<6}{r['样本']:>6}{r['中位']*100:>8.2f}%{r['均值']*100:>8.2f}%"
                  f"{r['P75']*100:>8.2f}%{r['P90']*100:>8.2f}%")

    df = pd.DataFrame(rows)
    fp = ROOT / "output" / "experiments" / "premium_audit.csv"
    atomic_to_csv(df, fp, index=False, encoding="utf-8-sig")
    print(f"\n已写入 {fp.relative_to(ROOT)}")

    # 关键读法：跨境加仓与减仓的中位之差 ≈ 每笔往返的溢价损耗
    cbrows = {r["方向"]: r for r in rows if r["组"] == "跨境"}
    if {"加仓", "减仓"} <= set(cbrows):
        loss = cbrows["加仓"]["中位"] - cbrows["减仓"]["中位"]
        print(f"\n跨境 ETF：加仓中位 {cbrows['加仓']['中位']:.2%} − 减仓中位 "
              f"{cbrows['减仓']['中位']:.2%} = 往返损耗 {loss:.2%}")
        print(f"高溢价尾部：P90 = {cbrows['加仓']['P90']:.2%}"
              f"（有 10% 的买入发生在高于此的水平）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
