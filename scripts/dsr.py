"""
用**登记的试验次数**重算 Deflated Sharpe，替换 overfit_check 里拍脑袋的"N=60"。

为什么需要这个脚本
------------------
DSR 的关键输入是"你试了多少次"。之前 `overfit_check.py` 里写死 `N=60`，
既没有来源，也不会随新实验更新。这个脚本：
* 从 `data/trials.json`（试验登记簿）取 N；
* 从 `output/overfit_grid.csv` 取各变体的 Sharpe 方差 V[SR]（这是唯一有完整分布的实验）；
* 对**实际部署的 S4**（`nav_main.csv`）重算 DSR，并给出 N=1 / 29 / 全体的敏感性。

用法
----
python -m etfquant dsr
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from etfquant import metrics  # noqa: E402
from etfquant.io_utils import atomic_write_text  # noqa: E402
from etfquant.overfit import deflated_sharpe  # noqa: E402

OUT = ROOT / "output"


def main() -> int:
    nav = pd.read_csv(OUT / "nav_main.csv", index_col=0, parse_dates=True)["S4-TGAR(自研)"].dropna()
    r = nav.pct_change(fill_method=None).dropna()
    excess = r - metrics.DEFAULT_RF / 252

    grid = pd.read_csv(OUT / "overfit_grid.csv")
    daily_sr = grid["sharpe"] / np.sqrt(252)
    sr_var = float(np.var(daily_sr, ddof=1))

    reg = json.loads((ROOT / "data" / "trials.json").read_text(encoding="utf-8"))
    n_grid = int(reg["grid_variants"])
    n_total = int(reg["total"])
    assert n_total == sum(int(t["n"]) for t in reg["trials"]), "登记簿 total 与各条之和不符"

    rows = []
    print(f"实际部署 S4 的年化 Sharpe = {deflated_sharpe(excess.to_numpy(), 1, 0)['sr']:.2f}"
          f"（V[SR] 取自参数网格，日频方差 {sr_var:.5f}）\n")
    for label, n in (("不搜索（N=1）", 1), ("只算参数网格", n_grid),
                     ("计入登记的全部试验", n_total)):
        ds = deflated_sharpe(excess.to_numpy(), n_trials=n, sr_variance=sr_var)
        rows.append({"label": label, "n_trials": n, "sr0": ds["sr0"], "sr": ds["sr"],
                     "dsr": ds["dsr"]})
        print(f"  {label:<18} N={n:<3} 临界 Sharpe {ds['sr0']:.2f} · "
              f"实际 {ds['sr']:.2f} · DSR = {ds['dsr']:.1%}")

    # 敏感性：网格的 V[SR] 极小（29 个近邻参数，日频方差 ~3e-5），是个下界。
    # 若把登记簿里 68 次试验当作"彼此独立的尝试"，Sharpe 的离散会大得多——
    # 用年化 σ=0.5 的保守假设再算一次，给出 DSR 的可信区间上界。
    cons_var = (0.5 / np.sqrt(252)) ** 2
    ds_c = deflated_sharpe(excess.to_numpy(), n_trials=n_total, sr_variance=cons_var)
    rows.append({"label": "保守方差（σ=0.5）", "n_trials": n_total, "sr0": ds_c["sr0"],
                 "sr": ds_c["sr"], "dsr": ds_c["dsr"]})
    print(f"  {'保守方差(σ=0.5)':<18} N={n_total:<3} 临界 Sharpe {ds_c['sr0']:.2f} · "
          f"实际 {ds_c['sr']:.2f} · DSR = {ds_c['dsr']:.1%}")

    atomic_write_text(OUT / "dsr.json",
                      json.dumps({"frozen_at": reg["frozen_at"], "sr_variance": sr_var,
                                  "rows": rows}, ensure_ascii=False, indent=2))
    print(f"\n[OK] 已写 output/dsr.json；登记试验总数 N = {n_total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
