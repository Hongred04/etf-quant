"""
试验 7.17 的随机对照（预注册第三节判据 2）。

做法：把股债相对强弱序列做**循环平移**（保留自相关与分布、破坏与股票收益的时间对齐），
按同样规则（分位 <20% → 权益上限 50%）生成 200 个"安慰剂方案"，各自回测。
真实方案的卡玛提升若落在随机分布的常见区间内，即为噪音。

为什么单独成脚本：200 次回测 × 4.3 秒 ≈ 14 分钟，单进程太慢，这里用多进程跑。

用法：python scripts/research/placebo_slow_gate.py --n 200 --workers 4
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
from etfquant.pipeline import load_panel
from etfquant.strategy import TrendGatedAdaptive

PANEL_START = "2018-01-02"
START = "2019-01-02"
EQUITY_PROXY, BOND_PROXY = "sh510300", "sh511260"
SEED = 20261101
_G: dict = {}


def _init() -> None:
    """每个 worker 只加载一次面板与基线信号。"""
    inv = pd.read_csv(ROOT / "data" / "etf_history_inventory.csv", dtype=str)
    vals = [v for v in inv["end"].dropna().tolist() if len(str(v)) == 10]
    end = max(set(vals), key=vals.count)
    panel = load_panel(ROOT / "data" / "etf_pool.csv", ROOT / "data" / "cache", PANEL_START, end)
    _G["panel"] = panel
    _G["targets"] = TrendGatedAdaptive(panel).generate()
    _G["rs"] = SG.equity_bond_rs(panel.close[EQUITY_PROXY], panel.close[BOND_PROXY])
    _G["debt"] = {t for t in panel.close.columns
                  if str(panel.meta["category"].get(t, "")) == SG.DEBT_CATEGORY}
    _G["dates"] = panel.close.index
    r = Backtester(panel.close, panel.open_).run(_G["targets"], start=START)
    _G["base_calmar"] = metrics.calmar(r.nav)


def _one(k: int) -> float:
    rs, dates = _G["rs"], _G["dates"]
    pct = SG.rolling_pct(rs.shift(int(k)))
    daily = SG.monthly_effective(pct, dates)
    caps = daily.map(lambda p: SG.cap_for_percentile(p, strict=False))
    tg = SG.apply_cap(_G["targets"], caps, _G["debt"])
    r = Backtester(_G["panel"].close, _G["panel"].open_).run(tg, start=START)
    return metrics.calmar(r.nav) - _G["base_calmar"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    rng = np.random.default_rng(SEED)
    n_days = 1900
    shifts = rng.integers(60, n_days - 60, size=args.n)

    from concurrent.futures import ProcessPoolExecutor  # noqa: PLC0415

    with ProcessPoolExecutor(max_workers=args.workers, initializer=_init) as ex:
        deltas = list(ex.map(_one, shifts.tolist(), chunksize=4))

    d = np.array(deltas, dtype=float)
    summary_fp = ROOT / "output" / "experiments" / "ab_slow_gate_summary.json"
    summary = json.loads(summary_fp.read_text(encoding="utf-8")) if summary_fp.exists() else {}
    real = float((summary.get("variants", {}).get("V1-分位<20%上限50%", {}) or {}).get("d_calmar", float("nan")))
    p = float((d >= real).mean())
    out = {
        "n": int(len(d)), "seed": SEED,
        "real_delta_v1": real,
        "placebo_mean": float(d.mean()),
        "placebo_p50": float(np.quantile(d, .50)),
        "placebo_p90": float(np.quantile(d, .90)),
        "placebo_p95": float(np.quantile(d, .95)),
        "p_value_one_sided": p,
        "criterion_significant": bool(p < 0.10),
    }
    summary["placebo"] = out
    summary_fp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"随机对照 {len(d)} 次（V1 规则，循环平移）")
    print(f"  真实卡玛提升 {real:+.3f}")
    print(f"  随机分布   中位 {out['placebo_p50']:+.3f}  均值 {out['placebo_mean']:+.3f}  "
          f"90分位 {out['placebo_p90']:+.3f}  95分位 {out['placebo_p95']:+.3f}")
    print(f"  单侧 p = {p:.3f} → {'显著（<0.10）' if p < 0.10 else '不显著（≥0.10）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
