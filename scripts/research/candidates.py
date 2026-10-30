"""最终配置对比：在增量归因基础上锁定 3 个候选版本。"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from etfquant import metrics
from etfquant.backtest import Backtester
from etfquant.pipeline import benchmark_series, load_panel
from etfquant.strategy import TrendGatedAdaptive

PANEL_START, START, END = "2018-01-02", "2019-01-02", "2026-10-23"

CANDIDATES = {
    "V1 初版(多因子+1/vol+全闸门+熔断)": {},
    "V2 纯动量+等权+半仓闸门+无熔断":
        dict(score_mode="mom20", require_trend=True, max_vr=3.0, buffer_mult=2,
             weight_mode="equal", dd_trigger=10.0),
    "V3 V2 去掉趋势过滤":
        dict(score_mode="mom20", require_trend=False, max_vr=3.0, buffer_mult=2,
             weight_mode="equal", dd_trigger=10.0),
    "V4 V2 但闸门可分档空仓":
        # 需要三档才叫"分档"：regime 低于 0.35 时敞口归零
        dict(score_mode="mom20", require_trend=True, max_vr=3.0, buffer_mult=2,
             weight_mode="equal", dd_trigger=10.0, expo_mid=0.5,
             expo_low=0.0, regime_low=0.35),
}


def main() -> None:
    panel = load_panel(ROOT / "data" / "etf_pool.csv", ROOT / "data" / "cache", PANEL_START, END)
    bench = benchmark_series(panel.close, "sh510300")
    subs = [("2019-2022", "2019-01-02", "2022-12-30"), ("2023-2026", "2023-01-03", END)]

    rows = {}
    for name, cfg in CANDIDATES.items():
        s = TrendGatedAdaptive(panel, **cfg)
        t = s.generate()
        r = Backtester(panel.close, panel.open_).run(t, start=START, end=END)
        st = metrics.summarize(r.nav, bench).to_dict()
        st["换手"] = r.stats["年均换手(单边)"]
        rec = {k: st[k] for k in ["累计收益", "年化收益(CAGR)", "夏普比率", "最大回撤", "卡玛比率", "换手"]}
        for label, s0, s1 in subs:
            rr = Backtester(panel.close, panel.open_).run(t, start=s0, end=s1)
            bb = bench[(bench.index >= pd.Timestamp(s0)) & (bench.index <= pd.Timestamp(s1))]
            rec[f"{label}_CAGR"] = metrics.cagr(rr.nav)
            rec[f"{label}_MaxDD"] = metrics.max_drawdown(rr.nav)
            rec[f"{label}_基准CAGR"] = metrics.cagr(bb)
        rows[name] = rec

    df = pd.DataFrame(rows).T
    pd.set_option("display.width", 200)
    out = df.copy()
    for c in out.columns:
        if "CAGR" in c or "回撤" in c or "累计" in c or "收益" in c:
            out[c] = out[c].map(lambda x: f"{x * 100:6.2f}%")
        else:
            out[c] = out[c].map(lambda x: f"{x:6.2f}")
    print(out.to_string())
    df.to_csv(ROOT / "output" / "experiments" / "candidates.csv", encoding="utf-8-sig")


if __name__ == "__main__":
    main()
