"""
A/B 对照：标的池范围对 TGAR 的影响。

上一轮验证过"权重工程"（去重/错峰/波动目标）全部无效，那收益到底来自哪里？
最可疑的是池子：现在 260 只里 39 只是宽基（沪深300/中证500/创业板…），
这些品种放进来只是重复暴露大盘，而策略本身已经有市场状态闸门在管大盘。

本脚本按资产类别切池子，其余参数一律保持现网默认，只动"能买什么"。

用法：python scripts/research/ab_pool.py
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
from etfquant.strategy import Panel, TrendGatedAdaptive

PANEL_START, START = "2018-01-02", "2019-01-02"


def latest_date() -> str:
    inv = ROOT / "data" / "etf_history_inventory.csv"
    df = pd.read_csv(inv, dtype=str)
    vals = [v for v in df["end"].dropna().tolist() if len(str(v)) == 10]
    return max(set(vals), key=vals.count)


END = latest_date()
SUBS = [("19-22", "2019-01-02", "2022-12-30"), ("23-26", "2023-01-03", END)]

# 名称 → 保留的类别。债券必须始终保留：它是"未用敞口的容器"，
# 抽掉债券等于强制留现金，会把仓位结构和回撤一起改变，那是另一个实验。
POOLS: dict[str, set[str] | None] = {
    "0 全池 260（基线）": None,
    "1 剔除宽基": {"行业", "主题", "跨境", "商品", "债券", "转债"},
    "2 剔除跨境": {"宽基", "行业", "主题", "商品", "债券", "转债"},
    "3 只要 行业+主题": {"行业", "主题", "债券", "转债"},
    "4 行业+主题+跨境": {"行业", "主题", "跨境", "债券", "转债"},
    "5 剔除 宽基+跨境": {"行业", "主题", "商品", "债券", "转债"},
}


def subset(panel: Panel, cats: set[str] | None) -> Panel:
    if cats is None:
        return panel
    keep = [c for c in panel.close.columns if panel.meta.loc[c, "category"] in cats]
    return Panel(
        close=panel.close[keep],
        open_=panel.open_[keep],
        high=panel.high[keep],
        low=panel.low[keep],
        volume=panel.volume[keep],
        meta=panel.meta.loc[keep],
    )


def run_one(panel: Panel, bench, cats: set[str] | None) -> dict:
    sub = subset(panel, cats)
    s = TrendGatedAdaptive(sub)
    targets = s.generate()
    r = Backtester(sub.close, sub.open_).run(targets, start=START, end=END)
    st = metrics.summarize(r.nav, bench).to_dict()

    eq = sub.meta["category"] != "债券"
    n_hold = [sum(1 for c, w in t.items() if w > 1e-6 and eq.get(c, False)) for t in targets.values()]

    rec = {
        "标的数": sub.close.shape[1],
        "年化": st["年化收益(CAGR)"],
        "累计": st["累计收益"],
        "最大回撤": st["最大回撤"],
        "夏普": st["夏普比率"],
        "卡玛": st["卡玛比率"],
        "换手": r.stats["年均换手(单边)"],
        "权益只数": (sum(n_hold) / len(n_hold)) if n_hold else 0.0,
    }
    for label, s0, s1 in SUBS:
        rr = Backtester(sub.close, sub.open_).run(targets, start=s0, end=s1)
        rec[f"{label}年化"] = metrics.cagr(rr.nav)
        rec[f"{label}回撤"] = metrics.max_drawdown(rr.nav)
    return rec


def main() -> None:
    panel = load_panel(ROOT / "data" / "etf_pool.csv", ROOT / "data" / "cache", PANEL_START, END)
    # 基准始终用全池的沪深300，不随池子变化，否则各变体的超额收益不可比
    bench = benchmark_series(panel.close, "sh510300")
    print(f"区间 {START} ~ {END} · 全池 {panel.close.shape[1]} 只\n")

    rows = {}
    for name, cats in POOLS.items():
        rows[name] = run_one(panel, bench, cats)
        r = rows[name]
        print(f"  {name:<20} 标的 {r['标的数']:>3}  CAGR {r['年化']*100:5.2f}%  "
              f"MaxDD {r['最大回撤']*100:6.2f}%  Sharpe {r['夏普']:4.2f}  换手 {r['换手']:5.1f}")

    df = pd.DataFrame(rows).T
    show = df.copy()
    for c in ["年化", "累计", "最大回撤", "19-22年化", "19-22回撤", "23-26年化", "23-26回撤"]:
        show[c] = show[c].map(lambda x: f"{x * 100:6.2f}%")
    for c in ["夏普", "卡玛", "换手", "权益只数"]:
        show[c] = show[c].map(lambda x: f"{x:6.2f}")

    print("\n" + "=" * 104)
    print(show.to_string())
    print("=" * 104)

    out = ROOT / "output" / "experiments" / "ab_pool.csv"
    df.to_csv(out, encoding="utf-8-sig")
    print(f"\n已写入 {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
