"""
A/B 对照：把 GitHub 上三条主流改进挂到 TGAR 上逐条验证。

对应关系
--------
* 同质化去重  ← guoyaohua/etf-adaptive-rotation-qmt（risk group + 相关性去重）
                fadaiba/a-share-etf-rotation（层次聚类，每簇选代表）
* 四周错峰    ← guoyaohua 的 staggered_weeks：组合 = 最近 N 次信号等权
* 波动率目标  ← AbacusFlow/quant V1、fadaiba（把组合已实现波动压到目标值）

三条都是**可选开关**，默认关闭，主线 S4 的行为不变。
本脚本只做对照，不写回 output/perf_main.csv。

用法：python scripts/research/ab_test.py
"""
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

PANEL_START, START = "2018-01-02", "2019-01-02"


def latest_date() -> str:
    inv = ROOT / "data" / "etf_history_inventory.csv"
    if inv.exists():
        df = pd.read_csv(inv, dtype=str)
        vals = [v for v in df.get("end", pd.Series(dtype=str)).dropna().tolist()
                if len(str(v)) == 10]
        if vals:
            return max(set(vals), key=vals.count)
    nav = ROOT / "output" / "nav_main.csv"   # 规范产物，仍在 output 顶层
    if nav.exists():
        return pd.read_csv(nav, index_col=0, parse_dates=True).index[-1].strftime("%Y-%m-%d")
    return "2026-10-27"


END = latest_date()

# 统一以「现网默认配置」为基线，各变体只改自己那一两个开关
BASE = dict(
    score_mode="mom20", require_trend=True, max_vr=3.0, buffer_mult=2,
    weight_mode="equal", dd_trigger=10.0, expo_mid=0.5,
)

VARIANTS: dict[str, dict] = {
    "0 基线（5 日调仓）": {},
    "1 3 日调仓": dict(rebalance_freq=3),
    "2 2 日调仓": dict(rebalance_freq=2),
    "3 10 日调仓": dict(rebalance_freq=10),
    "4 20 日调仓": dict(rebalance_freq=20),
}
SUBS = [("19-22", "2019-01-02", "2022-12-30"), ("23-26", "2023-01-03", END)]


def run_one(panel, bench, cfg: dict) -> dict:
    s = TrendGatedAdaptive(panel, **{**BASE, **cfg})
    targets = s.generate()
    r = Backtester(panel.close, panel.open_).run(targets, start=START, end=END)
    st = metrics.summarize(r.nav, bench).to_dict()

    # 平均同时在手的权益只数（不含债券），看去重是不是真把「伪分散」挤掉了
    eq = panel.meta["category"].reindex(panel.close.columns).fillna("主题") != "债券"
    n_hold = [sum(1 for c, w in t.items() if w > 1e-6 and eq.get(c, False)) for t in targets.values()]
    avg_hold = sum(n_hold) / len(n_hold) if n_hold else 0.0

    rec = {
        "年化": st["年化收益(CAGR)"],
        "累计": st["累计收益"],
        "最大回撤": st["最大回撤"],
        "夏普": st["夏普比率"],
        "卡玛": st["卡玛比率"],
        "年化波动": st["年化波动"],
        "换手": r.stats["年均换手(单边)"],
        "累计成本": r.stats.get("累计交易成本", 0.0),
        "平均权益只数": avg_hold,
        "调仓次数": len(targets),
    }
    for label, s0, s1 in SUBS:
        rr = Backtester(panel.close, panel.open_).run(targets, start=s0, end=s1)
        rec[f"{label}年化"] = metrics.cagr(rr.nav)
        rec[f"{label}回撤"] = metrics.max_drawdown(rr.nav)
    return rec


def main() -> None:
    panel = load_panel(ROOT / "data" / "etf_pool.csv", ROOT / "data" / "cache", PANEL_START, END)
    bench = benchmark_series(panel.close, "sh510300")
    print(f"区间 {START} ~ {END} · 标的 {panel.close.shape[1]} 只\n")

    rows = {}
    for name, cfg in VARIANTS.items():
        rows[name] = run_one(panel, bench, cfg)
        r = rows[name]
        print(f"  {name:<28} CAGR {r['年化']*100:5.2f}%  MaxDD {r['最大回撤']*100:6.2f}%  "
              f"Sharpe {r['夏普']:4.2f}  换手 {r['换手']:5.1f}  权益只数 {r['平均权益只数']:.2f}")

    df = pd.DataFrame(rows).T
    show = df.copy()
    for c in ["年化", "累计", "最大回撤", "年化波动", "累计成本", "19-22年化", "19-22回撤", "23-26年化", "23-26回撤"]:
        show[c] = show[c].map(lambda x: f"{x * 100:6.2f}%")
    for c in ["夏普", "卡玛", "换手", "平均权益只数"]:
        show[c] = show[c].map(lambda x: f"{x:6.2f}")

    print("\n" + "=" * 96)
    print(show.to_string())
    print("=" * 96)
    print("注：年化成本列为回测累计交易成本（占总资产），不是年化值。")

    out = ROOT / "output" / "experiments" / "ab_freq.csv"
    df.to_csv(out, encoding="utf-8-sig")
    print(f"\n已写入 {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
