"""
外部策略对照：「普通动量 + 改进残差动量」双因子行业轮动（12 个月 / 月度 / Top3 等权）。

为什么要跑
----------
这类策略的卖点通常是"某段牛市年化 60%+"。判断它有没有参考性，靠读文字没用：
要看 (a) 换到我们的数据上还成不成立；(b) 换个起点会怎样；(c) 两个因子各出了多少力。

做法
----
* 15 个行业取池内成交额最大的代表 ETF，其余口径按原文（12 个月对数动量、
  滚动 12 月回归剥离大盘/债券/黄金、排名各 50% 合成、Top3 等权、月末调仓）；
* 同一套引擎（T+1 开盘、双边 0.10%）把"因子的功劳"和"调仓频率的功劳"拆开；
* 三个窗口：全样本、原文声称的 2024-02-06~2026-06-30、原文自曝的 2023 起点。

一个必须说清的口径
------------------
残差动量要 12 个月回归 + 再 12 个月求和，所以这套策略**头两年没法建仓**：
2018-01 起的数据，双因子第一个信号在 2020-01。各变体的回测起点因此取
`max(窗口起点, 该变体首个信号日)`，并在结果里打印"有效起点"——
拿 2019 起的窗口直接比，会把两年的空仓摊进年化。

用法：python scripts/research/ab_dual_mom.py
产物：output/experiments/ab_dual_mom.csv（不写回 perf_main.csv，不影响主线）
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from etfquant import dual_mom as D  # noqa: E402
from etfquant import metrics  # noqa: E402
from etfquant.backtest import Backtester  # noqa: E402
from etfquant.io_utils import atomic_to_csv  # noqa: E402
from etfquant.pipeline import benchmark_series, load_panel  # noqa: E402
from etfquant.strategy import TrendGatedAdaptive  # noqa: E402

PANEL_START, END = "2018-01-02", "2026-12-31"

# 15 个行业：池内成交额最大的代表，同一行业只留一只（半导体 3 只、证券 2 只…只取一）
INDUSTRY = {
    "半导体": "sh512480",
    "通信": "sh515880",
    "软件": "sz159852",
    "证券": "sh512880",
    "银行": "sh512800",
    "煤炭": "sh515220",
    "有色": "sh512400",
    "军工": "sh512660",
    "医药": "sh512010",
    "消费": "sz159928",
    "电力": "sz159611",
    "化工": "sz159870",
    "传媒": "sh512980",
    "新能源": "sh516160",
    "地产": "sh512200",
}
MARKET, BOND, GOLD = "sh510300", "sh511010", "sh518880"

WINDOWS = {
    # 用 2020-01 当"全样本"起点：残差因子要 24 个月 warmup，2019 只有纯动量能跑，
    # 起点不同比年化毫无意义（2019 年沪深300 涨 40%，谁少跑一年谁吃亏）。
    "同起点 2020 起": ("2020-01-02", None),
    "原文样本 2024-02~2026-06": ("2024-02-06", "2026-06-30"),
    "原文自曝 2023 起": ("2023-01-03", None),
}


def fmt(v: float | None, d: int = 1) -> str:
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "—"
    return f"{v * 100:.{d}f}%"


def align_to_month_end(df: pd.DataFrame, ends: list[pd.Timestamp]) -> pd.DataFrame:
    """
    把"月末标签"的因子矩阵对齐到**当月最后一个交易日**。

    `resample("ME")` 的标签是日历月末（2023-04-30 是周日），直接 reindex 到交易日会全变 NaN。
    按「年-月」这个键去映射，才不会因为某个月末休市就整行丢掉。
    """
    p = df.copy()
    p.index = p.index.to_period("M")
    p = p[~p.index.duplicated(keep="last")]
    key = pd.Index([e.to_period("M") for e in ends])
    out = p.reindex(key)
    out.index = pd.DatetimeIndex(ends)
    return out


def main() -> int:
    print("[1/3] 加载面板并构造 15 个行业…")
    panel = load_panel(ROOT / "data" / "etf_pool.csv", ROOT / "data" / "cache", PANEL_START, END)
    close = panel.close
    missing = [c for c in {*INDUSTRY.values(), MARKET, BOND, GOLD} if c not in close.columns]
    if missing:
        print(f"      缺少标的：{missing}")
        return 1
    # 列名保持**代码**：喂给回测器的权重必须按代码匹配，用中文名会匹配不到任何一列，
    # 结果就是全程空仓、净值恒等于 1，而所有指标都还能正常算出来（只是全是 0）。
    ind = close[list(INDUSTRY.values())]
    name_of = {v: k for k, v in INDUSTRY.items()}
    print(f"      行业 {ind.shape[1]} 个 · 交易日 {len(ind)} · "
          f"{ind.index[0].date()} ~ {ind.index[-1].date()}")

    print("[2/3] 计算因子…")
    mclose = D.monthly_close(ind)
    mret = mclose.pct_change(fill_method=None)
    factors = pd.DataFrame({
        "mkt": D.monthly_close(close[[MARKET]]).pct_change(fill_method=None)[MARKET],
        "bond": D.monthly_close(close[[BOND]]).pct_change(fill_method=None)[BOND],
        "gold": D.monthly_close(close[[GOLD]]).pct_change(fill_method=None)[GOLD],
    })
    resid = D.residual_matrix(mret, factors, lookback=12)
    dvol = D.monthly_vol(ind)

    ends = D.month_end_dates(ind.index)
    mom_m = align_to_month_end(D.momentum(mclose, 12), ends)
    res_adj_m = align_to_month_end(D.residual_score(resid, dvol, 12, flip_anomaly=True), ends)
    res_raw_m = align_to_month_end(D.residual_score(resid, dvol, 12, flip_anomaly=False), ends)

    sig_dual = D.pick_top(D.combine(D.rank_scores(mom_m), D.rank_scores(res_adj_m)), n=3)
    sig_dual5 = D.pick_top(D.combine(D.rank_scores(mom_m), D.rank_scores(res_adj_m)), n=5)
    sig_mom = D.pick_top(D.rank_scores(mom_m), n=3)
    sig_resid = D.pick_top(D.rank_scores(res_adj_m), n=3)
    sig_resid_raw = D.pick_top(D.rank_scores(res_raw_m), n=3)

    # 同池、同选股数，只把"12 个月动量 + 月末调仓"换成"20 日动量 + 5 日调仓"：
    # 用来回答"是因子窗口的问题，还是调仓频率的问题"。
    r20 = close / close.shift(25) - 1        # skip 最近 5 日，与主策略一致
    grid5 = ind.index[252::5]
    sig_r20 = {}
    for d in grid5:
        row = r20.loc[d, list(INDUSTRY.values())].dropna()
        if len(row) < 3:
            continue
        picked = sorted(row.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        sig_r20[pd.Timestamp(d)] = {c: 1 / 3 for c, _ in picked}

    bt = Backtester(close, panel.open_)
    tgar_sig = TrendGatedAdaptive(panel).generate()
    bench = benchmark_series(close, MARKET)

    VARIANTS = [
        ("双因子 Top3 · 月度（原文）", sig_dual),
        ("纯动量 Top3 · 月度", sig_mom),
        ("纯残差 Top3 · 月度（含异常月补丁）", sig_resid),
        ("纯残差 Top3 · 月度（无补丁）", sig_resid_raw),
        ("双因子 Top5 · 月度", sig_dual5),
        ("R20 动量 Top3 · 5 日（同池，无闸门）", sig_r20),
        ("本项目 TGAR（对照）", tgar_sig),
    ]

    rows = []
    for wname, (w_start, w_end) in WINDOWS.items():
        print(f"[3/3] 窗口：{wname}")
        for label, sig in VARIANTS:
            first = min(sig) if sig else None
            if first is None:
                continue
            start = max(pd.Timestamp(w_start), first).strftime("%Y-%m-%d")
            r = bt.run(sig, start=start, end=w_end)
            nav = r.nav
            b = bench.loc[nav.index[0]:nav.index[-1]]
            st = metrics.summarize(nav, b)
            rows.append({
                "窗口": wname, "策略": label, "起点": start, "终点": str(nav.index[-1].date()),
                "交易日": int(len(nav)),
                "累计": float(st["累计收益"]), "CAGR": float(st["年化收益(CAGR)"]),
                "年化波动": float(st["年化波动"]), "夏普": float(st["夏普比率"]),
                "最大回撤": float(st["最大回撤"]), "卡玛": float(st["卡玛比率"]),
                "超额": float(st["超额收益"]), "年换手": float(r.stats.get("年均换手(单边)", np.nan)),
            })
        b = bench.loc[w_start:(w_end or None)]
        bs = metrics.summarize(b, None)
        rows.append({
            "窗口": wname, "策略": "沪深300（基准）", "起点": str(b.index[0].date()),
            "终点": str(b.index[-1].date()), "交易日": int(len(b)),
            "累计": float(bs["累计收益"]), "CAGR": float(bs["年化收益(CAGR)"]),
            "年化波动": float(bs["年化波动"]), "夏普": float(bs["夏普比率"]),
            "最大回撤": float(bs["最大回撤"]), "卡玛": float(bs["卡玛比率"]),
            "超额": 0.0, "年换手": 0.0,
        })

    df = pd.DataFrame(rows)
    out = ROOT / "output" / "experiments" / "ab_dual_mom.csv"
    atomic_to_csv(df, out, index=False, encoding="utf-8-sig")

    for wname in WINDOWS:
        sub = df[df["窗口"] == wname]
        if sub.empty:
            continue
        print(f"\n=== {wname} ===")
        print(f"{'策略':<36}{'起点':>12}{'CAGR':>8}{'夏普':>6}{'回撤':>9}{'卡玛':>6}{'超额':>9}{'换手':>6}")
        for r in sub.itertuples():
            print(f"{r.策略:<36}{r.起点:>12}{fmt(r.CAGR):>8}{r.夏普:>6.2f}"
                  f"{fmt(r.最大回撤):>9}{r.卡玛:>6.2f}{fmt(r.超额):>9}{r.年换手:>6.1f}")

    last = max(sig_dual)
    print(f"\n最近一期（信号日 {last.date()}）："
          + "、".join(f"{name_of.get(c, c)} {w*100:.0f}%" for c, w in sig_dual[last].items()))
    print(f"\n[OK] 已写 {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
