"""
跨境 ETF 场内溢价损耗的敏感性测试（诊断，非策略变体）。

背景
----
场内跨境 ETF（纳指、标普、日经等）经常出现较大溢价——2026 年 5 月纳指科技
ETF 景顺（159509）溢价一度 21%，同期纳斯达克100 ETF 平均 7~8%，极端品种 34.7%。
我们拿不到 IOPV（腾讯快照不提供），所以无法在回测里精确还原溢价。

替代做法：把溢价损耗抽象成"跨境标的的额外单边成本 cb_extra"，
用 Backtester(cb_tickers=..., cb_extra=...) 跑敏感性，回答
"如果每次进出跨境标的平均损耗 X%，这个策略还剩多少"。

注意这是**压力测试，不是预期值**：真实损耗取决于买入溢价与卖出溢价之差，
若溢价长期稳定，损耗接近 0；若追高买入（动量策略的倾向），损耗会显著。

用法：python scripts/research/cb_premium_sensitivity.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from etfquant import metrics
from etfquant.backtest import Backtester
from etfquant.io_utils import atomic_to_csv
from etfquant.pipeline import benchmark_series, load_panel
from etfquant.strategy import TrendGatedAdaptive

SCENARIOS = [(0.0, "0%（现状）"), (0.005, "0.5%/边"), (0.01, "1%/边"),
             (0.02, "2%/边"), (0.03, "3%/边"), (0.05, "5%/边")]


def main() -> int:
    inv = pd.read_csv(ROOT / "data" / "etf_history_inventory.csv", dtype=str)
    vals = [v for v in inv["end"].dropna().tolist() if len(str(v)) == 10]
    end = max(set(vals), key=vals.count)

    panel = load_panel(ROOT / "data" / "etf_pool.csv", ROOT / "data" / "cache",
                       "2018-01-02", end)
    bench = benchmark_series(panel.close, "sh510300")
    cb = {t for t in panel.close.columns
          if "跨境" in str(panel.meta["category"].get(t, ""))}
    print(f"池 {panel.close.shape[1]} 只，其中跨境 {len(cb)} 只"
          f"（{len(cb) / panel.close.shape[1]:.0%}）")

    tg = TrendGatedAdaptive(panel).generate()
    rows = []
    for extra, label in SCENARIOS:
        r = Backtester(panel.close, panel.open_, cb_tickers=cb, cb_extra=extra).run(
            tg, start="2019-01-02")
        st = metrics.summarize(r.nav, bench).to_dict()
        rows.append({
            "情景": label, "cb_extra": extra,
            "CAGR": round(st["年化收益(CAGR)"], 4),
            "MaxDD": round(st["最大回撤"], 4),
            "Sharpe": round(st["夏普比率"], 3),
            "卡玛": round(st["卡玛比率"], 3),
            "累计成本": round(float(r.stats["累计交易成本"]), 4),
            "年均换手(单边)": round(float(r.stats["年均换手(单边)"]), 2),
        })

    df = pd.DataFrame(rows)
    out = ROOT / "output" / "experiments" / "cb_premium_sensitivity.csv"
    atomic_to_csv(df, out, index=False, encoding="utf-8-sig")
    print()
    print(df.to_string(index=False))
    print(f"\n已写入 {out.relative_to(ROOT)}")
    print("\n读法：cb_extra 是「跨境标的单边额外成本」，往返即 2 倍。")
    print("      它不是预期值——真实损耗 = 买入溢价 − 卖出溢价，可为 0。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
