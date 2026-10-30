"""
A/B：视频版「ETF 均线波段操作策略」（5/20 金叉死叉 + MA60 过滤）。

规则按原文实现，两条原文没说清的地方做了显式选择：
1. 多只 ETF 同时出信号 → 池内全部满足条件者等权（= "分批进场"的组合化）；
2. 空仓期 → 现金（原文未提）。
「买入后下跌补仓摊薄」无法在组合级回测忠实复现（需要单持仓加仓记账），标注为未测。

对照设计
--------
* 池内版（全部 260 只，各自独立执行金叉/死叉，等权）——策略的"横向扫描"用法；
* 无 MA60 过滤版——把"趋势安全垫"的价值单独剥出来；
* 单只版（沪深300ETF 全仓进出）——视频最常见的散户用法；
* 本项目 TGAR——现有主线；
* 沪深300 买入持有——很多均线策略真正的对手其实是它。

判定（沿用项目纪律）：主窗口 + 两个子区间同向，再看要不要谈别的。

用法：python scripts/research/ab_macross.py
产物：output/experiments/ab_macross.csv
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from etfquant import macross as M  # noqa: E402
from etfquant import metrics  # noqa: E402
from etfquant.backtest import Backtester  # noqa: E402
from etfquant.io_utils import atomic_to_csv  # noqa: E402
from etfquant.pipeline import benchmark_series, load_panel  # noqa: E402
from etfquant.strategy import TrendGatedAdaptive  # noqa: E402

PANEL_START, END = "2018-01-02", "2026-12-31"
MAIN_START = "2019-01-02"
SUB_A = ("2020-01-02", "2022-12-31")
SUB_B = ("2023-01-03", None)


def fmt(v, d=1) -> str:
    return "—" if v is None or v != v else f"{v * 100:.{d}f}%"


def stats(bt, bench, sig, start, end=None) -> dict:
    r = bt.run(sig, start=start, end=end)
    b = bench.loc[r.nav.index[0]:r.nav.index[-1]]
    st = metrics.summarize(r.nav, b)
    return {
        "CAGR": float(st["年化收益(CAGR)"]), "夏普": float(st["夏普比率"]),
        "回撤": float(st["最大回撤"]), "卡玛": float(st["卡玛比率"]),
        "超额": float(st["超额收益"]), "换手": float(r.stats.get("年均换手(单边)", float("nan"))),
        "起始": str(r.nav.index[0].date()), "终点": str(r.nav.index[-1].date()),
    }


def main() -> int:
    print("[1/3] 加载面板…")
    panel = load_panel(ROOT / "data" / "etf_pool.csv", ROOT / "data" / "cache", PANEL_START, END)
    close = panel.close
    bt = Backtester(close, panel.open_)
    bench = benchmark_series(close, "sh510300")

    print("[2/3] 生成均线信号…")
    sig_full = M.build_targets(close, fast=5, slow=20, trend_look=5)
    sig_nofilter = M.build_targets(close, fast=5, slow=20, trend_look=None)
    sig_300 = M.single_etf_targets(close["sh510300"], fast=5, slow=20, trend_look=5)
    sig_tgar = TrendGatedAdaptive(panel).generate()
    for name, sig in [("池内版", sig_full), ("无过滤", sig_nofilter),
                      ("单只510300", sig_300), ("TGAR", sig_tgar)]:
        n_hold = sum(1 for w in sig.values() if w)
        print(f"      {name:<10} {len(sig)} 个调仓日，其中有持仓 {n_hold} 天")

    VARIANTS = [
        ("均线波段 + MA60 过滤（原文）", sig_full),
        ("均线波段（无 MA60 过滤）", sig_nofilter),
        ("单只沪深300ETF 波段", sig_300),
        ("本项目 TGAR（对照）", sig_tgar),
    ]

    print("[3/3] 回测…")
    rows = []
    for label, sig in VARIANTS:
        full = stats(bt, bench, sig, MAIN_START)
        a = stats(bt, bench, sig, *SUB_A)
        b = stats(bt, bench, sig, *SUB_B)
        rows.append({
            "策略": label, "CAGR": full["CAGR"], "夏普": full["夏普"],
            "回撤": full["回撤"], "卡玛": full["卡玛"], "超额": full["超额"],
            "换手": full["换手"], "2020-22": a["CAGR"], "2023-26": b["CAGR"],
            "回撤_20-22": a["回撤"], "回撤_23-26": b["回撤"],
        })
    # 基准直接用基准序列算：往回测器塞空 targets 会得到一条常数净值，
    # 夏普变成 0/0 的天文数字——上一版就输出过这种"看起来很炸"的数。
    bnav = bench.loc[MAIN_START:]
    rows.append({
        "策略": "沪深300 买入持有", "CAGR": float(metrics.cagr(bnav)),
        "夏普": float(metrics.sharpe(bnav)), "回撤": float(metrics.max_drawdown(bnav)),
        "卡玛": float(metrics.calmar(bnav)), "超额": 0.0, "换手": 0.0,
        "2020-22": float("nan"), "2023-26": float("nan"),
        "回撤_20-22": float("nan"), "回撤_23-26": float("nan"),
    })
    df = pd.DataFrame(rows)
    atomic_to_csv(df, ROOT / "output" / "experiments" / "ab_macross.csv", index=False, encoding="utf-8-sig")

    print(f"\n=== 主窗口 {MAIN_START} ~ {str(bnav.index[-1].date())} ===")
    print(f"{'策略':<26}{'CAGR':>8}{'夏普':>7}{'回撤':>9}{'卡玛':>7}{'超额':>9}{'换手':>7}"
          f"{'20-22':>8}{'23-26':>8}")
    for r in df.to_dict("records"):
        print(f"{r['策略']:<26}{fmt(r['CAGR']):>8}{r['夏普']:>7.2f}{fmt(r['回撤']):>9}"
              f"{r['卡玛']:>7.2f}{fmt(r['超额']):>9}{r['换手']:>7.1f}"
              f"{fmt(r['2020-22']):>8}{fmt(r['2023-26']):>8}")

    # 在场天数：从状态矩阵直接数（目标字典只记变化日，数它会把单只版数错）
    span = (close.index >= MAIN_START) & (close.index <= pd.Timestamp(END))
    st_pool = M.in_position(close) & M.trend_ok(close, 60, 5)
    st_one = M.in_position(close[["sh510300"]])["sh510300"] & M.trend_ok(close[["sh510300"]], 60, 5)["sh510300"]
    n_days = int(span.sum())
    print(f"\n在场天数：池内版 {int(st_pool.loc[MAIN_START:].any(axis=1).sum())}/{n_days}，"
          f"单只510300 {int(st_one.loc[MAIN_START:].sum())}/{n_days}")
    print("（池内版几乎天天有持仓：260 只里总有几十只在金叉态，等权组合的边际换手因此被放大）")
    print(f"\n[OK] 已写 output/experiments/ab_macross.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
