"""
试验 7.21 — 加仓时的溢价尾部过滤（预注册见 docs/prereg_7_21_premium_filter.md）。

背景：`premium_audit.py` 量出跨境 ETF 加仓中位溢价仅 +0.63%（往返损耗 0.02%），
但 P90 达 +7.21%——10% 的买入发生在 7% 以上的溢价上。本试验只处理这个尾部。

两个必须写进结论的限制：
1. 净值库从 2021-01-04 开始，**2019-2020 无过滤**，全样本会被前两年稀释；
2. 过滤只作用于新买入，已持仓不因高溢价强制卖出（预注册第三节）。

用法：python scripts/research/ab_premium_filter.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from etfquant import metrics
from etfquant.backtest import Backtester
from etfquant.io_utils import atomic_to_csv
from etfquant.pipeline import benchmark_series, load_panel
from etfquant.strategy import TrendGatedAdaptive

VARIANTS = [(None, "V0 基线"), (0.05, "V1 溢价>5%跳过"), (0.03, "V2 溢价>3%跳过")]
SUB = ("2021-01-04", "2026-10-30")


def load_premium(panel) -> pd.DataFrame:
    nav = pd.read_csv(ROOT / "data" / "etf_nav_history.csv", dtype={"ticker": str})
    nav["date"] = pd.to_datetime(nav["date"])
    nav = nav.pivot(index="date", columns="ticker", values="nav")
    nav.columns = [("sh" if c[0] in "56" else "sz") + c for c in nav.columns]
    return (panel.close / nav.reindex(panel.close.index) - 1.0)


def add_premium_stats(targets, prem: pd.DataFrame) -> dict:
    """加仓事件上的溢价分布。加仓 = 权重增加（含从 0 买入）。"""
    w = pd.DataFrame(targets).T.fillna(0.0).reindex(prem.index).ffill().fillna(0.0)
    dw = w.diff().fillna(w)
    v = prem.where(dw > 1e-6, np.nan).stack().dropna()
    if v.empty:
        return {"n": 0}
    return {"n": int(len(v)), "中位": round(float(v.median()), 4),
            "P75": round(float(v.quantile(.75)), 4), "P90": round(float(v.quantile(.90)), 4)}


def main() -> int:
    inv = pd.read_csv(ROOT / "data" / "etf_history_inventory.csv", dtype=str)
    vals = [v for v in inv["end"].dropna().tolist() if len(str(v)) == 10]
    end = max(set(vals), key=vals.count)

    panel = load_panel(ROOT / "data" / "etf_pool.csv", ROOT / "data" / "cache",
                       "2018-01-02", end)
    bench = benchmark_series(panel.close, "sh510300")
    prem = load_premium(panel)
    print(f"溢价矩阵覆盖 {prem.notna().mean().mean():.1%}")
    print(f"净值起点 {prem.dropna(how='all').index[0].date()} → "
          f"2019-2020 段无净值，等同基线\n")

    rows = []
    for cap, label in VARIANTS:
        tg = TrendGatedAdaptive(panel, premium=prem, premium_max=cap).generate()
        r = Backtester(panel.close, panel.open_).run(tg, start="2019-01-02")
        st = metrics.summarize(r.nav, bench).to_dict()
        r2 = Backtester(panel.close, panel.open_).run(tg, start=SUB[0], end=SUB[1])
        st2 = metrics.summarize(r2.nav, bench).to_dict()
        ps = add_premium_stats(tg, prem)
        rows.append({
            "变体": label, "阈值": cap,
            "CAGR": round(st["年化收益(CAGR)"], 4), "MaxDD": round(st["最大回撤"], 4),
            "Sharpe": round(st["夏普比率"], 3), "卡玛": round(st["卡玛比率"], 3),
            "CAGR_2021起": round(st2["年化收益(CAGR)"], 4),
            "卡玛_2021起": round(st2["卡玛比率"], 3),
            "加仓n": ps.get("n", 0), "加仓溢价中位": ps.get("中位"),
            "加仓溢价P90": ps.get("P90"),
            "换手": round(float(r.stats["年均换手(单边)"]), 2),
        })
        print(f"{label:<16} CAGR {rows[-1]['CAGR']*100:5.2f}%  卡玛 {rows[-1]['卡玛']:5.3f}  "
              f"| 2021起 CAGR {rows[-1]['CAGR_2021起']*100:5.2f}%  卡玛 {rows[-1]['卡玛_2021起']:5.3f}  "
              f"| 加仓 P90 {rows[-1]['加仓溢价P90']*100:5.2f}%")

    df = pd.DataFrame(rows)
    fp = ROOT / "output" / "experiments" / "ab_premium_filter.csv"
    atomic_to_csv(df, fp, index=False, encoding="utf-8-sig")
    print(f"\n已写入 {fp.relative_to(ROOT)}")
    print("\n判据（预注册第三节）：卡玛 ≥ 基线−0.02 且 加仓P90 < 4%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
