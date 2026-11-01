"""
个券止损有效性诊断（描述性统计，不是策略变体，不登记 trials.json）。

问题
----
外部项目（Adkid-Zephyr）用「单只 ETF 从持有高点回撤 5% 就卖、冷却 10 天」做风控。
本项目只有组合级回撤熔断，且已实测为负贡献（README 7.2/7.4）。

在花成本改造回测引擎之前，先回答一个更便宜的问题：
**历史上出现「持有期内回撤 >5%」之后，接下来 20 个交易日是继续跌还是反弹？**

- 若之后中位收益 ≥ 0 → 止损会卖在低点，直接否，不写策略代码。
- 若 < 0 且样本 ≥ 100 → 值得按规矩预注册做完整实验。
- 若 < 0 但样本 < 100 → 证据不足，不下结论。

判据在跑之前已写定（见对话/记录），不做事后调整。

用法：python scripts/research/stop_loss_diagnostic.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from etfquant.pipeline import load_panel

THRESHOLD = -0.05      # 相对持有期内高点的回撤阈值
FORWARD = 20           # 事件后观察窗口（交易日）


def main() -> int:
    inv = pd.read_csv(ROOT / "data" / "etf_history_inventory.csv", dtype=str)
    vals = [v for v in inv["end"].dropna().tolist() if len(str(v)) == 10]
    end = max(set(vals), key=vals.count)

    panel = load_panel(ROOT / "data" / "etf_pool.csv", ROOT / "data" / "cache", "2018-01-02", end)
    px = panel.close
    w = pd.read_csv(ROOT / "output" / "weights_s4_tgar_main.csv", index_col=0, parse_dates=True)
    w = w.reindex(px.index).ffill().fillna(0.0)

    rows = []
    for ticker in [c for c in w.columns]:
        held = w[ticker] > 1e-6
        if not held.any():
            continue
        # 把连续持有日合并成区间
        grp = (held != held.shift()).cumsum()
        for _, idx in held[held].groupby(grp[held]).groups.items():
            seg = px[ticker].reindex(idx).dropna()
            if len(seg) < 2:
                continue
            peak = seg.cummax()
            dd = seg / peak - 1.0
            breach = dd[dd <= THRESHOLD]
            if breach.empty:
                continue
            event_dt = breach.index[0]                 # 首次跌破阈值那天
            pos = px.index.get_loc(event_dt)
            fwd_end = min(pos + FORWARD, len(px.index) - 1)
            if fwd_end <= pos:
                continue
            p0 = px[ticker].iloc[pos]
            p1 = px[ticker].iloc[fwd_end]
            if not np.isfinite(p0) or not np.isfinite(p1) or p0 <= 0:
                continue
            rows.append({
                "ticker": ticker,
                "event": event_dt,
                "drawdown_at_event": float(dd.loc[event_dt]),
                "fwd_ret_20d": float(p1 / p0 - 1.0),
                "hold_days": len(seg),
            })

    if not rows:
        print("没有找到任何事件，样本为空——无法回答。")
        return 0

    df = pd.DataFrame(rows)
    n = len(df)
    med = df["fwd_ret_20d"].median()
    mean = df["fwd_ret_20d"].mean()
    win = (df["fwd_ret_20d"] > 0).mean()

    print(f"事件定义：持有期内相对高点回撤 ≤ {THRESHOLD:.0%}（首次触发）")
    print(f"观察窗口：事件日后 {FORWARD} 个交易日")
    print(f"样本数：{n} 个事件（覆盖 {df['ticker'].nunique()} 只标的）\n")
    print(f"  后续 20 日中位收益   {med:+.2%}")
    print(f"  后续 20 日平均收益   {mean:+.2%}")
    print(f"  后续 20 日为正的比例 {win:.1%}")
    print(f"  事件时平均回撤       {df['drawdown_at_event'].mean():.2%}")
    print(f"  平均持有天数         {df['hold_days'].mean():.1f}")

    by_year = df.groupby(df["event"].dt.year)["fwd_ret_20d"].agg(["count", "median"])
    print("\n按年份（看是否只是某一年造成的）:")
    for y, r in by_year.iterrows():
        print(f"  {y}: n={int(r['count']):3d}  中位 {r['median']:+.2%}")

    print("\n判定（按预先写定的判据）:")
    if med >= 0:
        verdict = "否 —— 之后中位收益非负，止损会卖在低点"
    elif n >= 100:
        verdict = "值得完整测试 —— 之后中位收益为负且样本充足"
    else:
        verdict = "证据不足 —— 中位为负但样本 < 100，不下结论"
    print(f"  {verdict}")

    out = ROOT / "output" / "experiments" / "stop_loss_diagnostic.csv"
    df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n明细已写入 {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
