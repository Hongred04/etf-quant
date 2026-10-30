"""
闸门判据的滚动窗口稳定性检验。

为什么不是"训练/测试切分"
--------------------------
`gate_mode` 没有可调参数（要么用沪深300，要么用池内等权），
所以不存在"在训练集上选参数、在测试集上验证"的空间。真正要回答的是：

    换判据带来的优势，是**每个时间段都成立**，还是只在某一段（比如 2019-2021
    的小盘/主题行情）成立？

做法：把 2019-2026 切成若干个 12 个月滚动窗口（步长 6 个月），每个窗口独立跑一遍
两个判据的净值，统计胜率与差值分布。窗口内不调参、不筛选。

用法：python scripts/research/rolling_gate.py [--freq 5] [--window 12] [--step 6]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from etfquant import metrics
from etfquant.backtest import Backtester
from etfquant.pipeline import benchmark_series, load_panel
from etfquant.strategy import TrendGatedAdaptive

PANEL_START = "2018-01-02"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--freq", type=int, default=5)
    ap.add_argument("--window", type=int, default=12, help="窗口长度（月）")
    ap.add_argument("--step", type=int, default=6, help="步长（月）")
    args = ap.parse_args()

    panel = load_panel(ROOT / "data" / "etf_pool.csv", ROOT / "data" / "cache",
                       PANEL_START, "2026-12-31")
    end_all = panel.close.index[-1]
    bench = benchmark_series(panel.close, "sh510300")

    targets = {}
    for gm in ("csi300", "breadth"):
        s = TrendGatedAdaptive(panel, gate_mode=gm, rebalance_freq=args.freq)
        targets[gm] = s.generate()

    # 滚动窗口：以自然月为边界，保证每个窗口长度一致
    starts = pd.date_range("2019-01-01", end_all, freq=f"{args.step}MS")
    rows = []
    for st in starts:
        en = st + pd.DateOffset(months=args.window) - pd.Timedelta(days=1)
        if en > end_all:
            en = end_all
        if (en - st).days < 150:          # 太短的窗口不给结论
            continue
        rec = {"窗口": f"{st:%Y-%m}~{en:%Y-%m}"}
        for gm, tg in targets.items():
            r = Backtester(panel.close, panel.open_).run(tg, start=str(st.date()), end=str(en.date()))
            rec[f"{gm}_cagr"] = metrics.cagr(r.nav)
            rec[f"{gm}_dd"] = metrics.max_drawdown(r.nav)
            rec[f"{gm}_sharpe"] = metrics.sharpe(r.nav)
        rec["cagr差"] = rec["breadth_cagr"] - rec["csi300_cagr"]
        rec["dd差"] = rec["breadth_dd"] - rec["csi300_dd"]      # 越接近 0 / 越正越好
        rec["sharpe差"] = rec["breadth_sharpe"] - rec["csi300_sharpe"]
        rows.append(rec)

    df = pd.DataFrame(rows)
    show = df.copy()
    for c in ["csi300_cagr", "breadth_cagr", "cagr差", "csi300_dd", "breadth_dd", "dd差"]:
        show[c] = show[c].map(lambda x: f"{x * 100:6.1f}%")
    for c in ["csi300_sharpe", "breadth_sharpe", "sharpe差"]:
        show[c] = show[c].map(lambda x: f"{x:6.2f}")

    print(f"\n{args.freq} 日调仓 · {args.window} 个月滚动窗口 · 步长 {args.step} 个月")
    print("=" * 96)
    print(show.to_string(index=False))
    print("=" * 96)

    n = len(df)
    win_c = (df["cagr差"] > 0).sum()
    win_s = (df["sharpe差"] > 0).sum()
    win_d = (df["dd差"] > 0).sum()      # breadth 回撤更浅
    print(f"窗口数 {n}：breadth 收益更高的 {win_c} 个（{win_c / n:.0%}），"
          f"夏普更高的 {win_s} 个（{win_s / n:.0%}），回撤更浅的 {win_d} 个（{win_d / n:.0%}）")
    print(f"收益差中位数 {df['cagr差'].median() * 100:+.1f}pp，均值 {df['cagr差'].mean() * 100:+.1f}pp，"
          f"最差 {df['cagr差'].min() * 100:+.1f}pp，最好 {df['cagr差'].max() * 100:+.1f}pp")

    out = ROOT / "output" / f"rolling_gate_{args.freq}d_{args.window}m.csv"
    df.to_csv(out, encoding="utf-8-sig")
    print(f"已写入 {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
