"""策略诊断：年度收益、市场状态分布、仓位暴露归因。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from etfquant import metrics
from etfquant.backtest import Backtester
from etfquant.pipeline import benchmark_series, load_panel
from etfquant.strategy import STRATEGY_REGISTRY, TrendGatedAdaptive

START, END = "2019-01-02", "2026-10-23"
PANEL_START = "2018-01-02"


def main() -> None:
    panel = load_panel(ROOT / "data" / "etf_pool.csv", ROOT / "data" / "cache", PANEL_START, END)
    bench = benchmark_series(panel.close, "sh510300")
    bench = bench[bench.index >= pd.Timestamp(START)]

    # 四策略年度收益
    ann = {"基准-沪深300": metrics.annual_returns(bench)}
    for key in ["s1_baseline", "s2_dual", "s3_multi", "s4_tgar"]:
        s = STRATEGY_REGISTRY[key](panel)
        t = s.generate()
        r = Backtester(panel.close, panel.open_).run(t, start=START, end=END)
        ann[s.name] = metrics.annual_returns(r.nav)
    tbl = pd.DataFrame(ann)
    tbl.index = tbl.index.year
    print("=== 年度收益 ===")
    print((tbl * 100).round(1).to_string())

    # TGAR 状态分布
    s4 = TrendGatedAdaptive(panel)
    days = s4.rebalance_days()
    reg = np.array([s4.regime_at(d) for d in days])
    print("\n=== TGAR 市场状态分布（信号日） ===")
    for v in [0.0, 0.3, 0.4, 0.6, 0.7, 1.0]:
        print(f"  regime={v:.1f}: {(reg == v).sum():3d} 天 ({(reg == v).mean():.1%})")
    print(f"  平均 regime = {reg.mean():.3f}")

    t = s4.generate()
    w = pd.DataFrame(
        [{k: v for k, v in d.items()} for d in t.values()], index=list(t.keys())
    ).fillna(0.0)
    print(f"\n  信号日平均股票类仓位 = {w.sum(axis=1).mean():.2%}")
    bond_cols = [c for c in w.columns if s4.cat.get(c) == "债券"]
    print(f"  其中债券类平均仓位 = {w[bond_cols].sum(axis=1).mean():.2%}")
    print(f"  持有标的数分布: \n{w.gt(0).sum(axis=1).value_counts().to_string()}")


if __name__ == "__main__":
    main()
