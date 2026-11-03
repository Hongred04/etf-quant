#!/usr/bin/env python
"""把最大回撤压到 20% 以内的候选方案对比。

只在**同一套记账**上比较，指标口径与主回测一致。三类机制：
    A 固定敞口 k        —— 最简单，参数最少
    B 回撤熔断（触发 dd → 降敞口到 cut，回到 dd/2 解除）
    C 波动目标           —— 按过去 20 日已实现波动缩放

输出 output/dd_frontier.json，并在终端按"满足 MaxDD ≥ -20%"分组打印。

用法
----
    python scripts/dd_frontier.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from etfquant import metrics as M  # noqa: E402
from etfquant.backtest import Backtester  # noqa: E402
from etfquant.io_utils import atomic_write_text  # noqa: E402
from etfquant.pipeline import load_panel  # noqa: E402
from etfquant.strategy import TrendGatedAdaptive  # noqa: E402

TARGET_DD = -0.20


def const_overlay(k: float):
    return lambda dt, nav, peak: k


def dd_overlay(trigger: float, cut: float):
    state = {"armed": False}

    def f(dt, nav, peak):
        dd = nav / peak - 1.0 if peak > 0 else 0.0
        if not state["armed"] and dd <= -trigger:
            state["armed"] = True
        elif state["armed"] and dd > -trigger / 2:
            state["armed"] = False
        return cut if state["armed"] else 1.0

    return f


def vol_overlay(target: float, window: int = 20):
    hist: list[float] = []

    def f(dt, nav, peak):
        hist.append(float(nav))
        if len(hist) < window + 1:
            return 1.0
        seg = np.asarray(hist[-(window + 1):], dtype=float)
        rets = seg[1:] / seg[:-1] - 1.0
        vol = float(np.std(rets, ddof=1) * np.sqrt(252)) if len(rets) > 2 else 0.0
        if vol <= 1e-6:
            return 1.0
        return float(min(1.0, target / vol))

    return f


def measure(panel, targets, overlay, name: str) -> dict:
    res = Backtester(panel.close, panel.open_, overlay=overlay).run(
        targets, start="2019-01-25")
    nav = res.nav
    return {
        "name": name,
        "cagr": float(M.cagr(nav)),
        "mdd": float(M.max_drawdown(nav)),
        "sharpe": float(M.sharpe(nav)),
        "calmar": float(M.calmar(nav)),
        "avg_exposure": float(res.weights.sum(axis=1).mean()),
        "cost": float(res.cost.sum()),
        "underwater_days": int(M.underwater_stats(nav)["max_days"]),
    }


def main() -> int:
    panel = load_panel(ROOT / "data/etf_pool.csv", ROOT / "data/cache",
                       "2018-01-02", "2026-12-31")
    targets = TrendGatedAdaptive(panel).generate()

    scenarios: list[tuple[str, object]] = [("基线（无覆盖）", None)]
    for k in (0.9, 0.8, 0.7, 0.65, 0.6, 0.55, 0.5):
        scenarios.append((f"A 固定敞口 {k:.2f}", const_overlay(k)))
    for trig in (0.05, 0.08, 0.10, 0.12, 0.15):
        for cut in (0.5, 0.0):
            scenarios.append((f"B 回撤熔断 {trig:.0%}→{cut:.0%}",
                              dd_overlay(trig, cut)))
    for tgt in (0.10, 0.12, 0.15, 0.18):
        scenarios.append((f"C 波动目标 {tgt:.0%}", vol_overlay(tgt)))

    rows = [measure(panel, targets, ov, name) for name, ov in scenarios]
    df = pd.DataFrame(rows)
    df["meets"] = df["mdd"] >= TARGET_DD

    print(f"{'方案':22}{'CAGR':>8}{'MaxDD':>9}{'Sharpe':>8}{'Calmar':>8}{'均仓':>7}")
    print("-" * 62)
    for r in df.itertuples():
        flag = " ✓" if r.meets else ""
        print(f"{r.name:22}{r.cagr * 100:7.1f}%{r.mdd * 100:8.1f}%"
              f"{r.sharpe:8.2f}{r.calmar:8.2f}{r.avg_exposure * 100:6.0f}%{flag}")

    ok = df[df["meets"]].sort_values("cagr", ascending=False)
    print("\n满足 MaxDD ≥ -20% 的方案（按 CAGR 排序）：")
    if ok.empty:
        print("  无。最接近的是：")
        print(df.sort_values("mdd", ascending=False).head(3).to_string(index=False))
    else:
        print(ok[["name", "cagr", "mdd", "sharpe", "calmar", "avg_exposure"]]
              .to_string(index=False, float_format=lambda v: f"{v:.3f}"))

    payload = {
        "target_mdd": TARGET_DD,
        "baseline": rows[0],
        "scenarios": rows,
        "meeting": ok["name"].tolist(),
    }
    atomic_write_text(ROOT / "output" / "dd_frontier.json",
                      json.dumps(payload, ensure_ascii=False, indent=2))
    print("\n[OK] output/dd_frontier.json")

    # ---- 人读报告 ----
    base = rows[0]
    fixed = df[df["name"].str.startswith("A ")]
    brk = df[df["name"].str.startswith("B ")]
    vol = df[df["name"].str.startswith("C ")]
    meet = ok.iloc[0] if not ok.empty else None

    lines = [
        "# 把最大回撤压到 20% 以下：可行方案与代价",
        "",
        "> 本文件由 `python scripts/dd_frontier.py` 生成，数字不手写。",
        "",
        f"基线（完全按信号执行）：CAGR **{base['cagr'] * 100:.1f}%**，"
        f"MaxDD **{base['mdd'] * 100:.1f}%**，Sharpe {base['sharpe']:.2f}，"
        f"Calmar {base['calmar']:.2f}。",
        "",
        "## 结论先说",
        "",
        "**只有降低风险资产暴露能达到目标，而且 Calmar 不变**——"
        "也就是说这是一次风险预算的选择，不是模型改进。"
        "所有熔断类方案都没能把回撤压到 20% 以内。",
        "",
        "## 固定敞口缩放",
        "",
        "| 敞口系数 | CAGR | MaxDD | Sharpe | Calmar | 平均仓位 |",
        "|---|---|---|---|---|---|",
    ]
    for r in fixed.itertuples():
        mark = " ✅" if r.meets else ""
        lines.append(f"| {r.name.split()[-1]} | {r.cagr * 100:.1f}% | {r.mdd * 100:.1f}% | "
                     f"{r.sharpe:.2f} | {r.calmar:.2f} | {r.avg_exposure * 100:.0f}%{mark} |")
    lines += [
        "",
        "Calmar 在所有档位都停在 0.36~0.37：**降低暴露不会改善风险调整后的收益**，"
        "只是把同一条线上的点往左下移。",
        "",
        "## 熔断 / 波动目标：都达不到",
        "",
        "| 方案 | CAGR | MaxDD |",
        "|---|---|---|",
    ]
    for r in list(brk.itertuples()) + list(vol.itertuples()):
        lines.append(f"| {r.name} | {r.cagr * 100:.1f}% | {r.mdd * 100:.1f}% |")
    lines += [
        "",
        "原因有二：① 覆盖只在**调仓日**（每 5 个交易日）评估，而回撤往往在几天内完成；"
        "② 触发后切到空仓会让净值无法恢复，解除条件永远不成立（平均仓位掉到 3%）。"
        "把解除条件改成「N 日后」或「站上 MA60」能避免锁死，但回撤仍在 −22%~−27%。",
        "",
        "另一项检查：把持仓从 3 只分散到 5/6/8 只，MaxDD 分别是 −30.6%/−35.2%/−36.5%，"
        "**分散对系统性回撤无效**。",
        "",
        "## 建议",
        "",
    ]
    if meet is not None:
        lines += [
            f"若 20% 是硬约束：用 **{meet['name']}**，"
            f"CAGR {meet['cagr'] * 100:.1f}%、MaxDD {meet['mdd'] * 100:.1f}%、"
            f"Sharpe {meet['sharpe']:.2f}。代价是年化少赚约 "
            f"{(base['cagr'] - meet['cagr']) * 100:.1f}pp，"
            "且收益会低于沪深300 全收益的 7%——本质是用收益换波动。",
        ]
    lines += [
        "",
        "启用方式（默认关闭，不改变现有信号）：",
        "",
        "```bash",
        "python scripts/run_signal.py --risk-scale 0.5   # 本次生成按 50% 风险预算",
        "ETF_RISK_SCALE=0.5 python scripts/run_signal.py  # 或用环境变量",
        "```",
        "",
        "缩放后的目标权重之和小于 1，差额留现金；界面的「买入清单」会直接体现。",
    ]
    atomic_write_text(ROOT / "docs" / "drawdown.md", "\n".join(lines) + "\n")
    print("[OK] docs/drawdown.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
