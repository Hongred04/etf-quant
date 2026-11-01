"""
试验 7.17 — 慢变量仓位上限（股债相对强弱分位）。

预注册见 docs/prereg_7_17_slow_gate.md（hash 冻结在 data/prereg_7_17_slow_gate.sha256）。
本脚本只实现预注册里写死的三个变体与判据，不搜索参数、不追加变体。

用法
----
python scripts/research/ab_slow_gate.py                 # 三个变体 + 子样本
python scripts/research/ab_slow_gate.py --placebo 200   # 追加随机对照（慢）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from etfquant import metrics, slow_gate as SG
from etfquant.backtest import Backtester
from etfquant.pipeline import benchmark_series, load_panel
from etfquant.strategy import TrendGatedAdaptive

PANEL_START = "2018-01-02"
START, END = "2019-01-02", None
SUBS = [("20-22", "2020-01-01", "2022-12-31"), ("23-26", "2023-01-01", "2026-12-31")]
EQUITY_PROXY, BOND_PROXY = "sh510300", "sh511260"
SEED = 20261101                      # 随机对照的种子，写死保证可复现


def latest_date() -> str:
    inv = ROOT / "data" / "etf_history_inventory.csv"
    df = pd.read_csv(inv, dtype=str)
    vals = [v for v in df["end"].dropna().tolist() if len(str(v)) == 10]
    return max(set(vals), key=vals.count) if vals else "2026-10-30"


def summarize(r, bench) -> dict:
    st = metrics.summarize(r.nav, bench).to_dict()
    return {
        "年化": st["年化收益(CAGR)"],
        "累计": st["累计收益"],
        "最大回撤": st["最大回撤"],
        "夏普": st["夏普比率"],
        "卡玛": st["卡玛比率"],
        "年化波动": st["年化波动"],
        "换手": r.stats["年均换手(单边)"],
    }


def run_variant(targets, panel, bench, label: str) -> dict:
    r = Backtester(panel.close, panel.open_).run(targets, start=START, end=END)
    rec = summarize(r, bench)
    for name, s0, s1 in SUBS:
        rr = Backtester(panel.close, panel.open_).run(targets, start=s0, end=s1)
        rec[f"{name}年化"] = metrics.cagr(rr.nav)
        rec[f"{name}回撤"] = metrics.max_drawdown(rr.nav)
        rec[f"{name}卡玛"] = metrics.calmar(rr.nav)
    rec["变体"] = label
    return rec


def caps_from_pct(pct: pd.Series, dates: pd.DatetimeIndex, strict: bool) -> pd.Series:
    daily = SG.monthly_effective(pct, dates)
    return daily.map(lambda p: SG.cap_for_percentile(p, strict=strict))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--placebo", type=int, default=0, help="随机对照次数（预注册 200）")
    args = ap.parse_args()

    end = END or latest_date()
    panel = load_panel(ROOT / "data" / "etf_pool.csv", ROOT / "data" / "cache", PANEL_START, end)
    bench = benchmark_series(panel.close, EQUITY_PROXY)
    print(f"区间 {START} ~ {end} · 标的 {panel.close.shape[1]} 只\n")

    # 股债相对强弱：用 ETF 收盘（前复权）构造，5 年滚动分位
    rs = SG.equity_bond_rs(panel.close[EQUITY_PROXY], panel.close[BOND_PROXY])
    pct = SG.rolling_pct(rs)
    valid = pct.dropna()
    print(f"RS 分位可用起点：{valid.index[0].date() if len(valid) else '无'} "
          f"（5 年预热，共 {len(valid)} 个交易日）")

    strat = TrendGatedAdaptive(panel)
    base_targets = strat.generate()
    debt_cols = {t for t in panel.close.columns
                 if str(panel.meta["category"].get(t, "")) == SG.DEBT_CATEGORY}
    dates = panel.close.index

    variants = {"V0-基线": base_targets}
    for label, strict in (("V1-分位<20%上限50%", False), ("V2-再<10%归零", True)):
        caps = caps_from_pct(pct, dates, strict)
        trig = int((caps < 1.0).sum())
        print(f"{label}: 触发约束 {trig} 个交易日（{trig / len(dates) * 100:.0f}%）")
        variants[label] = SG.apply_cap(base_targets, caps, debt_cols)

    rows = {}
    for label, tg in variants.items():
        rows[label] = run_variant(tg, panel, bench, label)
        r = rows[label]
        print(f"  {label:<22} CAGR {r['年化'] * 100:5.2f}%  MaxDD {r['最大回撤'] * 100:6.2f}%  "
              f"Sharpe {r['夏普']:4.2f}  卡玛 {r['卡玛']:5.3f}")

    df = pd.DataFrame(rows).T
    out = ROOT / "output" / "experiments" / "ab_slow_gate.csv"
    df.to_csv(out, encoding="utf-8-sig")
    print(f"\n已写入 {out.relative_to(ROOT)}")

    # ---------- 判据（预注册第三节，逐条打印，不做事后润色） ----------
    b = rows["V0-基线"]
    summary = {"generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
               "data_end": end, "variants": {}}
    for label in ("V1-分位<20%上限50%", "V2-再<10%归零"):
        v = rows[label]
        d_calmar = v["卡玛"] - b["卡玛"]
        d_mdd = v["最大回撤"] - b["最大回撤"]          # 正数=回撤变小=改善
        d_cagr = v["年化"] - b["年化"]
        ok1 = d_calmar >= 0.05 and d_mdd >= 0.01 and d_cagr >= -0.005
        sub1 = v["20-22卡玛"] - b["20-22卡玛"]
        sub2 = v["23-26卡玛"] - b["23-26卡玛"]
        ok3 = (sub1 == 0 and sub2 == 0) or (sub1 * sub2 > 0)
        summary["variants"][label] = {
            "d_calmar": d_calmar, "d_mdd": d_mdd, "d_cagr": d_cagr,
            "criterion_effect": bool(ok1),
            "sub_calmar_delta": {"20-22": sub1, "23-26": sub2},
            "criterion_robust": bool(ok3),
        }
        print(f"\n{label}")
        print(f"  卡玛 {d_calmar:+.3f}（门槛 ≥ +0.05）  MaxDD {d_mdd * 100:+.2f}pp（门槛 ≥ +1.00pp）  "
              f"CAGR {d_cagr * 100:+.2f}pp（门槛 ≥ -0.50pp）  → 有效性 {'满足' if ok1 else '不满足'}")
        print(f"  子样本卡玛变化 20-22 {sub1:+.3f} / 23-26 {sub2:+.3f}  → 稳健性 {'满足' if ok3 else '不满足'}")

    # ---------- 随机对照（可选，预注册 200 次） ----------
    if args.placebo:
        rng = np.random.default_rng(SEED)
        shifts = rng.integers(60, len(rs) - 60, size=args.placebo)
        base_calmar = b["卡玛"]
        r = Backtester(panel.close, panel.open_)
        observed = rows["V1-分位<20%上限50%"]  # noqa: F841
        deltas = []
        for i, k in enumerate(shifts, 1):
            shifted = rs.shift(int(k))
            p2 = SG.rolling_pct(shifted)
            caps = caps_from_pct(p2, dates, strict=False)
            tg = SG.apply_cap(base_targets, caps, debt_cols)
            try:
                rr = r.run(tg, start=START, end=END)
                deltas.append(metrics.calmar(rr.nav) - base_calmar)
            except (KeyError, ValueError) as exc:
                print(f"  [WARN] 第 {i} 次对照失败：{exc}")
            if i % 50 == 0:
                print(f"  随机对照 {i}/{args.placebo} …")
        d = np.array(deltas, dtype=float)
        real = summary["variants"]["V1-分位<20%上限50%"]["d_calmar"]
        # p 值：随机方案里"改善至少和真实方案一样多"的比例（单侧）
        p = float((d >= real).mean()) if len(d) else float("nan")
        summary["placebo"] = {"n": int(len(d)), "real_delta": real,
                              "p_value": p, "mean": float(d.mean()), "p90": float(np.quantile(d, .9))}
        print(f"\n随机对照 {len(d)} 次：真实卡玛提升 {real:+.3f}，"
              f"随机均值 {d.mean():+.3f}，90 分位 {np.quantile(d, .9):+.3f}，单侧 p = {p:.3f}")
        print(f"  → 显著性 {'满足（p<0.10）' if p < 0.10 else '不满足（p≥0.10）'}")

    sjson = ROOT / "output" / "experiments" / "ab_slow_gate_summary.json"
    sjson.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入 {sjson.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
