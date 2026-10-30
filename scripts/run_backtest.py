"""
批量回测多个策略并与基准对比。

用法
----
python scripts/run_backtest.py --start 2019-01-02 --end 2026-10-23
python scripts/run_backtest.py --strategies s4_tgar --tag tgar
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from etfquant import metrics
from etfquant.backtest import Backtester
from etfquant.io_utils import atomic_to_csv
from etfquant.pipeline import benchmark_series, load_panel
from etfquant.universe import POOL_MODES, apply_pool
from etfquant.strategy import STRATEGY_REGISTRY

OUT = ROOT / "output"


def run_one(panel, key: str, start: str, end: str, fee: float, slip: float, **kw):
    cls = STRATEGY_REGISTRY[key]
    strat = cls(panel, **kw)
    targets = strat.generate()
    bt = Backtester(panel.close, panel.open_, fee=fee, slippage=slip)
    res = bt.run(targets, start=start, end=end)
    res.strategy_name = strat.name
    return strat, res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01-02")
    # 默认 None：加载面板后取数据实际最后一天。写死日期会让回测永远停在某天之前。
    ap.add_argument("--end", default=None)
    ap.add_argument("--strategies", default="s1_baseline,s2_dual,s3_multi,s4_tgar")
    ap.add_argument("--fee", type=float, default=0.0003)
    ap.add_argument("--slippage", type=float, default=0.0007)
    ap.add_argument("--tag", default="main")
    ap.add_argument("--pool", default="full", choices=list(POOL_MODES),
                    help="池子口径：full=全部标的（默认）；core=剔除纯大盘宽基（实验，实测无改善）")
    ap.add_argument("--panel-start", default="2018-01-02",
                    help="面板数据起点（须早于回测起点，用于因子预热）")
    args = ap.parse_args()

    panel = load_panel(
        ROOT / "data" / "etf_pool.csv", ROOT / "data" / "cache",
        args.panel_start, args.end or "2035-12-31",
    )
    panel = apply_pool(panel, args.pool)
    if args.end is None:
        args.end = panel.close.index[-1].strftime("%Y-%m-%d")
        print(f"[INFO] 未指定 --end，自动取数据最后一天 {args.end}")
    print(f"[INFO] 池内标的 {panel.close.shape[1]} 只，交易日 {panel.close.shape[0]} 个")
    bench = benchmark_series(panel.close, "sh510300")
    bench = bench[bench.index >= pd.Timestamp(args.start)]

    OUT.mkdir(parents=True, exist_ok=True)
    rows, curves = {}, {}
    for key in [s.strip() for s in args.strategies.split(",") if s.strip()]:
        strat, res = run_one(panel, key, args.start, args.end, args.fee, args.slippage)
        st = metrics.summarize(res.nav, bench).to_dict()
        st["策略"] = strat.name
        st.update(res.stats)
        # 计算失败的调仓日数量要落盘：>0 说明这次回测的结果不完整
        st["失败调仓日"] = len(getattr(strat, "skipped", []))
        rows[key] = st
        curves[strat.name] = res.nav
        atomic_to_csv(res.holdings, OUT / f"holdings_{key}_{args.tag}.csv",
                      index=False, encoding="utf-8-sig")
        atomic_to_csv(res.weights, OUT / f"weights_{key}_{args.tag}.csv", encoding="utf-8-sig")
        if res.trades is not None and not res.trades.empty:
            atomic_to_csv(res.trades, OUT / f"trades_{key}_{args.tag}.csv",
                          index=False, encoding="utf-8-sig")
        print(f"[OK] {strat.name}: 累计 {st['累计收益']:.1%} | CAGR {st['年化收益(CAGR)']:.1%} "
              f"| MaxDD {st['最大回撤']:.1%} | Sharpe {st['夏普比率']:.2f} "
              f"| 调仓 {st['调仓次数']} 次 年均换手 {st['年均换手(单边)']:.1f}")

    curves["基准-沪深300"] = bench.reindex(next(iter(curves.values())).index).ffill()
    df = pd.DataFrame(rows).T
    df.index = [STRATEGY_REGISTRY[k].__name__ for k in df.index]
    atomic_to_csv(df, OUT / f"perf_{args.tag}.csv", encoding="utf-8-sig")

    nav_df = pd.DataFrame(curves)
    atomic_to_csv(nav_df, OUT / f"nav_{args.tag}.csv", encoding="utf-8-sig")

    show = ["策略", "累计收益", "年化收益(CAGR)", "年化波动", "夏普比率", "最大回撤",
            "卡玛比率", "超额收益", "Beta", "信息比率", "年均换手(单边)"]
    show = [c for c in show if c in df.columns]
    print("\n=== 绩效汇总 ===")
    fmt = df[show].copy()
    # 换手是"倍数"不是比例，必须单独处理，否则会被格式化成 2413%
    plain = {"策略", "年均换手(单边)", "调仓次数"}
    for c in fmt.columns:
        if c in plain:
            if c != "策略":
                fmt[c] = fmt[c].map(lambda x: f"{x:.2f}" if pd.notna(x) else "-")
        elif fmt[c].abs().max() < 5:
            fmt[c] = fmt[c].map(lambda x: f"{x:.3f}" if pd.notna(x) else "-")
        else:
            fmt[c] = fmt[c].map(lambda x: f"{x:.2%}" if pd.notna(x) else "-")
    print(fmt.to_string())


if __name__ == "__main__":
    main()
