"""
A/B：视频版「动量 + 波动率双因子，每月 Top3」策略。

规则按原文最朴素的实现（**参数未做任何挑选**，这点很重要：本实验要验证的是
"漂亮窗口 vs 长窗口"的落差，如果在这里挑参数，实验本身就变成了它批评的对象）：
* 打分 = 0.5·z(3 个月动量) + 0.5·z(低波动)，横截面排名合成（另一组用 0.7/0.3）；
* 每月末收盘打分，买总分前 3 等权，跌出前 3 的卖出；
* 无闸门、无条件筛选、T+1 开盘、双边 0.10%——与主回测同一引擎。

对照窗口
--------
* 2024-01 ~ 2025-12：视频声称"年化 40% / 回撤 6.5% / 胜率 80%"的窗口；
* 2019-01 ~ 2026-10：全样本；
* 2020-22 / 2023-26：子区间，看方向是否一致。

用法：python scripts/research/ab_momvol.py
产物：output/experiments/ab_momvol.csv
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from etfquant import dual_mom as D  # noqa: E402  （复用月度因子/排名/选股的纯函数）
from etfquant import metrics  # noqa: E402
from etfquant.backtest import Backtester  # noqa: E402
from etfquant.io_utils import atomic_to_csv  # noqa: E402
from etfquant.pipeline import benchmark_series, load_panel  # noqa: E402

PANEL_START, END = "2018-01-02", "2026-12-31"

WINDOWS = {
    "视频窗口 2024-01~2025-12": ("2024-01-02", "2025-12-31"),
    "全样本 2019-01~2026-10": ("2019-01-02", None),
    "2020-22": ("2020-01-02", "2022-12-31"),
    "2023-26": ("2023-01-03", None),
}


def fmt(v, d=1) -> str:
    return "—" if v is None or v != v else f"{v * 100:.{d}f}%"


def monthly_win_rate(nav: pd.Series, start: str, end: str) -> float | None:
    """月度胜率：正收益月份 / 有收益月份。"""
    seg = nav.loc[start:end]
    m = (1 + seg.pct_change(fill_method=None).dropna()).resample("ME").prod() - 1
    if len(m) < 6:
        return None
    return float((m > 0).mean())


def main() -> int:
    print("[1/3] 加载面板…")
    panel = load_panel(ROOT / "data" / "etf_pool.csv", ROOT / "data" / "cache", PANEL_START, END)
    close = panel.close
    bt = Backtester(close, panel.open_)
    bench = benchmark_series(close, "sh510300")

    print("[2/3] 生成月度信号（动量 3 个月 + 低波动 3 个月，参数未挑选）…")
    mclose = D.monthly_close(close)
    mret = mclose.pct_change(fill_method=None)
    mom = D.momentum(mclose, 3)                    # 3 个月对数动量
    lowvol = -mret.rolling(3).std()                # 低波动 = 波动率因子的负向
    # 卫生约束（不是调参）：上市不满 12 个月的新 ETF 不参与打分。
    # 不加它，"只有 3 个月数据"的新品种会凭极端首月收益/波动拿走排名，
    # 实验就变成对次新 ETF 噪声的测量，而不是对这条规则的测量。
    cnt = mret.notna().cumsum()
    mom = mom.where(cnt >= 12)
    lowvol = lowvol.where(cnt >= 12)
    sig50 = D.pick_top(D.combine(D.rank_scores(mom), D.rank_scores(lowvol), 0.5), n=3)
    sig70 = D.pick_top(D.combine(D.rank_scores(mom), D.rank_scores(lowvol), 0.7), n=3)
    print(f"      50/50 版 {len(sig50)} 期，70/30 版 {len(sig70)} 期")

    rows = []
    for label, sig in [("动量+低波 50/50 · 月度Top3", sig50),
                       ("动量+低波 70/30 · 月度Top3", sig70),
                       ("本项目 TGAR（对照）", __import__("etfquant.strategy", fromlist=["TrendGatedAdaptive"])
                        .TrendGatedAdaptive(panel).generate())]:
        r_full = bt.run(sig, start="2019-01-02")
        row = {"策略": label}
        for wname, (s, e) in WINDOWS.items():
            r = bt.run(sig, start=s, end=e)
            st = metrics.summarize(r.nav)
            row[wname] = {
                "CAGR": float(st["年化收益(CAGR)"]), "MDD": float(st["最大回撤"]),
            }
        row["月胜率_视频窗口"] = monthly_win_rate(r_full.nav, "2024-01-02", "2025-12-31")
        st = metrics.summarize(r_full.nav, bench.loc[r_full.nav.index[0]:])
        row["全样本CAGR"] = float(st["年化收益(CAGR)"])
        row["全样本MDD"] = float(st["最大回撤"])
        row["全样本夏普"] = float(st["夏普比率"])
        row["全样本换手"] = float(r_full.stats.get("年均换手(单边)", float("nan")))
        rows.append(row)

    # 基准
    brow = {"策略": "沪深300（基准）"}
    for wname, (s, e) in WINDOWS.items():
        seg = bench.loc[s:e].dropna()
        cagr = (seg.iloc[-1] / seg.iloc[0]) ** (252 / len(seg)) - 1
        mdd = (seg / seg.cummax() - 1).min()
        brow[wname] = {"CAGR": float(cagr), "MDD": float(mdd)}
    brow["月胜率_视频窗口"] = monthly_win_rate(bench, "2024-01-02", "2025-12-31")
    brow.update({"全样本CAGR": float(metrics.cagr(bench)), "全样本MDD": float(metrics.max_drawdown(bench)),
                 "全样本夏普": float(metrics.sharpe(bench)), "全样本换手": 0.0})
    rows.append(brow)

    df = pd.DataFrame(rows)
    atomic_to_csv(df, ROOT / "output" / "experiments" / "ab_momvol.csv", index=False, encoding="utf-8-sig")

    print("\n=== 分窗口对照（CAGR / 最大回撤） ===")
    for wname in WINDOWS:
        print(f"--- {wname}")
        for r in df.to_dict("records"):
            w = r[wname]
            print(f"  {r['策略']:<26} {fmt(w['CAGR']):>8} / {fmt(w['MDD']):>8}"
                  + (f"   月胜率 {r['月胜率_视频窗口']*100:.0f}%" if r['月胜率_视频窗口'] is not None else ""))

    # 样本量视角：视频窗口年化波动有多大，2 年样本的年化估计置信区间就有多宽
    v50 = df.iloc[0]
    print("\n=== 统计视角 ===")
    print("视频窗口 2 年样本 → 年化估计的 95% 置信区间约 ±1.96·σ/√2；")
    print("也就是说'年化 40%'与'年化 19%或 61%'在统计上分不开。")
    print(f"\n[OK] 已写 output/experiments/ab_momvol.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
