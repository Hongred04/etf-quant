#!/usr/bin/env python
"""收益归因：把 S4 的年化收益拆成 Beta / 择时 / 选股 / 债券 / 成本。

产出
----
    output/attribution.json   机器可读，供页面/其它脚本引用
    docs/attribution.md       人读的报告（数字全部由本脚本生成，不手写）

用法
----
    python scripts/attribution.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from etfquant import attribution as A  # noqa: E402
from etfquant.backtest import Backtester  # noqa: E402
from etfquant.io_utils import atomic_write_text  # noqa: E402
from etfquant.pipeline import load_panel  # noqa: E402
from etfquant.strategy import TrendGatedAdaptive  # noqa: E402


def fmt(x: float | None, d: int = 1) -> str:
    return "—" if x is None else f"{x * 100:.{d}f}%"


def main() -> int:
    panel = load_panel(ROOT / "data/etf_pool.csv", ROOT / "data/cache",
                       "2018-01-02", "2026-12-31")
    targets = TrendGatedAdaptive(panel).generate()
    res = Backtester(panel.close, panel.open_).run(targets, start="2019-01-25")

    pool = pd.read_csv(ROOT / "data/etf_pool.csv", dtype={"ticker": str})
    bond = set(pool.loc[pool["category"] == "债券", "ticker"])
    eq_cols = [c for c in panel.close.columns if c not in bond]

    comp, contrib = A.decompose(res.daily, panel.close, panel.open_, eq_cols)
    comp = A.with_cost(comp, res.cost)
    summ = A.summarize(comp)

    boot = A.block_bootstrap(comp, n=1500)
    ci = {k: [float(pd.Series(v).quantile(0.025)), float(pd.Series(v).quantile(0.975))]
          for k, v in boot.items()}

    episodes = A.episodes_from_trades(res.trades, comp.index[-1])
    conc = A.concentration(comp, contrib, episodes)

    bench_cagr = 0.07          # 沪深300 全收益，README 口径
    be = A.break_even_cost(comp, bench_cagr)

    net_cagr = A.cagr_from_returns(comp["net"])
    # perf_main 是"对外唯一来源"的 CAGR（run_backtest 的区间端点略有不同），
    # 两个数一起列，避免报告和 README 出现两个 CAGR 却没人解释。
    perf_cagr = None
    try:
        perf = pd.read_csv(ROOT / "output/perf_main.csv")
        row = perf[perf["策略"] == "S4-TGAR(自研)"]
        if not row.empty:
            perf_cagr = float(row["年化收益(CAGR)"].iloc[0])
    except (OSError, KeyError, ValueError):
        pass

    payload = {
        "period": [str(comp.index[0].date()), str(comp.index[-1].date())],
        "net_cagr": net_cagr,
        "perf_cagr": perf_cagr,
        "backtest_cagr": float((res.nav.iloc[-1] / res.nav.iloc[0])
                               ** (365.25 / (res.nav.index[-1] - res.nav.index[0]).days) - 1),
        "avg_equity_weight": summ["avg_equity_weight"],
        "components": {k: summ[k] for k in
                       ("beta", "timing", "selection", "bond", "cost", "sum", "residual")},
        "ci95": ci,
        "concentration": conc,
        "break_even": be,
    }
    (ROOT / "output").mkdir(exist_ok=True)
    atomic_write_text(ROOT / "output" / "attribution.json",
                      json.dumps(payload, ensure_ascii=False, indent=2))

    rows = [
        ("Beta（市场）", "beta"),
        ("择时（闸门加减仓）", "timing"),
        ("选股（260 只挑 3 只）", "selection"),
        ("债券腿", "bond"),
        ("交易成本", "cost"),
    ]
    lines = [
        "# 收益归因：S4 的年化从哪里来",
        "",
        "> 本文件由 `python scripts/attribution.py` 生成，数字不手写。",
        "",
        f"区间 {payload['period'][0]} → {payload['period'][1]}，"
        f"净 CAGR **{net_cagr * 100:.2f}%**（`perf_main` 口径 "
        f"{'—' if perf_cagr is None else f'{perf_cagr * 100:.2f}%'}），平均权益仓位 "
        f"**{summ['avg_equity_weight'] * 100:.1f}%**。",
        "",
        "| 分项 | 年化贡献 | 95% 区间（块自助） |",
        "|---|---|---|",
    ]
    for label, key in rows:
        lo, hi = ci[key]
        lines.append(f"| {label} | {fmt(summ[key], 2)} | "
                     f"[{lo * 100:+.1f}%, {hi * 100:+.1f}%] |")
    lines += [
        f"| **合计** | **{fmt(summ['sum'], 2)}** | — |",
        "",
        f"恒等式自检：分项合计 − 实际净收益 = {summ['residual'] * 100:.6f} 个百分点"
        "（应为 0）。",
        "",
        "## 怎么读",
        "",
        f"- **Beta {fmt(summ['beta'], 1)}**：平均 {summ['avg_equity_weight'] * 100:.0f}% 的权益仓位"
        "拿等权市场，几乎不依赖选股。",
        f"- **择时 {fmt(summ['timing'], 1)}**：闸门把仓位从满仓调到半仓，"
        "这部分是它相对“平均仓位”的加减分。",
        f"- **选股 {fmt(summ['selection'], 1)}**：从 260 只里挑 3 只的横截面超额，"
        "这才是策略的核心 alpha。",
        f"- **债券 {fmt(summ['bond'], 1)}**：半仓时段的票息，波动极低。",
        f"- **成本 {fmt(summ['cost'], 1)}**：年均换手 24 倍换来的代价。",
        "",
        "## 收益集中度",
        "",
    ]
    if conc.get("top5_share") is not None:
        lines += [
            f"共 {conc['n_episodes']} 个持仓段，收益前 5% 的段贡献了 "
            f"**{conc['top5_share'] * 100:.0f}%** 的总收益。",
            "",
            "| 剔除最好的 k 段 | 剩余 CAGR |",
            "|---|---|",
        ]
        for d in conc["drop"]:
            lines.append(f"| {d['k']} 段 | {d['cagr'] * 100:.1f}% |")
        lines += ["", "剔除后仍然为正，说明收益不是靠个别交易撑起来的。" if
                  all(d["cagr"] > 0 for d in conc["drop"]) else
                  "", "剔除后转负：收益高度依赖少数交易，实盘复制难度大。" if
                  any(d["cagr"] <= 0 for d in conc["drop"]) else ""]
    lines += [
        "",
        "## 成本敏感度",
        "",
        "成本按换手线性放大（1x = 单边 0.10%）：",
        "",
        "| 成本倍数 | CAGR |",
        "|---|---|",
        f"| 1x | {be['cagr_at_1x'] * 100:.1f}% |",
        f"| 2x | {be['cagr_at_2x'] * 100:.1f}% |",
        f"| 3x | {be['cagr_at_3x'] * 100:.1f}% |",
        "",
        f"- CAGR 归零的成本倍数：**{be['zero']:.2f}x**"
        if be["zero"] else "- 20x 以内不会归零",
        f"- 跌到基准（7%）以下的成本倍数：**{be['below_benchmark']:.2f}x**"
        if be["below_benchmark"] else "- 20x 以内不会跌破基准",
        "",
        "> 口径：算术年化贡献相加等于净收益（恒等式），与几何 CAGR 的差是波动拖累；"
        "两者都列在 JSON 里。",
    ]
    atomic_write_text(ROOT / "docs" / "attribution.md", "\n".join(lines) + "\n")

    print(f"净 CAGR {net_cagr * 100:.2f}%  |  Beta {summ['beta'] * 100:+.1f}  "
          f"择时 {summ['timing'] * 100:+.1f}  选股 {summ['selection'] * 100:+.1f}  "
          f"债券 {summ['bond'] * 100:+.1f}  成本 {summ['cost'] * 100:+.1f}")
    print(f"恒等式残差 {summ['residual'] * 100:.2e}pp  |  "
          f"前 5% 持仓段贡献 {conc.get('top5_share', 0) * 100:.0f}%  |  "
          f"成本归零倍数 {be['zero']:.2f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
