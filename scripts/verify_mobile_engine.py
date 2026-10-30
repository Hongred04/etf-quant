"""
手机端策略引擎 vs 桌面端 Python 引擎的对拍脚本。

为什么需要这个
--------------
手机上重写了一遍策略逻辑，只要有一处不一致（min_periods、ddof、skip 的位置），
出来的信号就是错的，而且不会有任何报错。所以必须用同一份输入分别跑两个实现，
逐项比对数值，而不是"看起来差不多"。

用法
----
    python scripts/verify_mobile_engine.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server  # noqa: E402
from etfquant.strategy import Panel, TrendGatedAdaptive  # noqa: E402

NODE = Path("C:/Users/macro/.workbuddy/binaries/node/versions/22.22.2-6/node.exe")


def build_panel(hist: dict) -> Panel:
    """用与手机端完全相同的历史数据重建 Panel，消除数据源差异。"""
    dates = pd.DatetimeIndex(pd.to_datetime(hist["dates"]))
    cols = hist["codes"]

    close = pd.DataFrame(
        {c: hist["close"][i] for i, c in enumerate(cols)}, index=dates, dtype=float
    )
    vol = pd.DataFrame(
        {c: hist["vol"][i] for i, c in enumerate(cols)}, index=dates, dtype=float
    )

    # 与 pipeline.load_panel 完全一致：close ffill，volume fillna(0)
    close = close.ffill()
    vol = vol.fillna(0.0)

    pool = pd.read_csv(ROOT / "data" / "etf_pool.csv", dtype={"ticker": str})
    meta = pool.set_index("ticker")[["name", "category"]].reindex(close.columns)

    return Panel(
        close=close, open_=close.copy(), high=close.copy(), low=close.copy(),
        volume=vol, meta=meta,
    )


def run_python(hist: dict, t: int) -> dict:
    panel = build_panel(hist)
    strat = TrendGatedAdaptive(panel)
    dt = panel.close.index[t]
    elig = strat.eligible_at(dt)
    w = strat.target_weights(dt, elig)
    regime = strat.regime_at(dt)
    expo = strat._exposure(regime)
    total = sum(w.values())
    return {
        "date": dt.strftime("%Y-%m-%d"),
        "regime": round(float(regime), 6),
        "exposure": round(float(expo), 6),
        "weights": {k: round(float(v), 6) for k, v in sorted(w.items())},
        "total": round(float(total), 6),
    }


JS_RUNNER = r"""
const fs = require('fs');
const EtfEngine = require(process.argv[2]);
const payload = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const t = parseInt(process.argv[4], 10);
const prep = EtfEngine.prepare(payload.history);
const sig = EtfEngine.signalAt(prep, t, payload.meta, []);
const out = {
  date: sig.date,
  regime: Math.round(sig.regime * 1e6) / 1e6,
  exposure: Math.round(sig.exposure * 1e6) / 1e6,
  weights: {},
  total: 0
};
sig.rows.forEach(function (r) {
  out.weights[r.code] = Math.round(r.weight * 1e6) / 1e6;
  out.total += r.weight;
});
out.total = Math.round(out.total * 1e6) / 1e6;
const keys = Object.keys(out.weights).sort();
const sorted = {};
keys.forEach(function (k) { sorted[k] = out.weights[k]; });
out.weights = sorted;
out.diag = sig.diag;
out.candidates = sig.candidates.slice(0, 6).map(function (c) {
  return { code: c.code, r20: c.r20 === null ? null : Math.round(c.r20 * 1e6) / 1e6 };
});
console.log(JSON.stringify(out));
"""


def run_js(hist: dict, meta: dict, t: int) -> dict:
    payload = {"history": hist, "meta": meta}
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        (tdp / "payload.json").write_text(json.dumps(payload), encoding="utf-8")
        (tdp / "runner.js").write_text(JS_RUNNER, encoding="utf-8")
        engine = ROOT / "mobile" / "app" / "assets" / "www" / "js" / "engine.js"
        proc = subprocess.run(
            [str(NODE), str(tdp / "runner.js"), str(engine), str(tdp / "payload.json"), str(t)],
            capture_output=True, text=True, encoding="utf-8",
        )
    if proc.returncode != 0:
        raise RuntimeError(f"node 失败：{proc.stderr[:600]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main() -> None:
    print("=" * 78)
    print("手机端引擎对拍：JS 结果 vs Python 结果")
    print("=" * 78)

    hist = server._history_payload()
    pool = pd.read_csv(ROOT / "data" / "etf_pool.csv", dtype={"ticker": str})
    meta = {
        r["ticker"]: {"name": r["name"], "category": r["category"]}
        for _, r in pool.iterrows()
    }

    fails = 0
    # 抽 5 个不同时点，覆盖不同市场状态（最近、100 天前、200 天前…）
    n = len(hist["dates"])
    points = [n - 1, n - 6, n - 21, n - 61, n - 121]
    for t in points:
        py = run_python(hist, t)
        js = run_js(hist, meta, t)

        tag = f"{py['date']} (t={t})"
        ok_date = py["date"] == js["date"]
        ok_regime = abs(py["regime"] - js["regime"]) < 1e-9
        ok_expo = abs(py["exposure"] - js["exposure"]) < 1e-9
        ok_total = abs(py["total"] - js["total"]) < 1e-9

        py_keys = sorted(py["weights"])
        js_keys = sorted(js["weights"])
        ok_set = py_keys == js_keys

        max_diff = 0.0
        if ok_set:
            for k in py_keys:
                max_diff = max(max_diff, abs(py["weights"][k] - js["weights"][k]))
        ok_w = ok_set and max_diff < 1e-9

        mark = lambda b: "PASS" if b else "FAIL"  # noqa: E731
        print(f"\n{tag}")
        print(f"  regime  {py['regime']:.3f} vs {js['regime']:.3f}   [{mark(ok_regime)}]")
        print(f"  敞口    {py['exposure']:.1%} vs {js['exposure']:.1%}   [{mark(ok_expo)}]")
        print(f"  标的    {py_keys}")
        print(f"          {js_keys}   [{mark(ok_set)}]")
        if ok_set:
            print(f"  单只最大差 {max_diff:.2e}   [{mark(ok_w)}]")
        print(f"  合计    {py['total']:.4f} vs {js['total']:.4f}   [{mark(ok_total)}]")

        if not (ok_date and ok_regime and ok_expo and ok_total and ok_w):
            fails += 1
            print(f"  !! 不一致  python={py['weights']}  js={js['weights']}")

    # ---- 因子级抽查：候选的 r20 必须逐个相同 ----
    print("\n" + "-" * 78)
    print("因子级抽查（最后一日候选 R20）")
    print("-" * 78)
    t = n - 1
    py_hist = build_panel(hist)
    strat = TrendGatedAdaptive(py_hist)
    dt = py_hist.close.index[t]
    py_r20 = strat.fb.r20.loc[dt]
    js = run_js(hist, meta, t)
    bad = 0
    for c in js["candidates"]:
        pv = py_r20.get(c["code"])
        pv = None if (pv is None or pd.isna(pv)) else round(float(pv), 6)
        same = (pv is None and c["r20"] is None) or (
            pv is not None and c["r20"] is not None and abs(pv - c["r20"]) < 1e-6
        )
        if not same:
            bad += 1
            print(f"  [FAIL] {c['code']}  python={pv}  js={c['r20']}")
        else:
            print(f"  [PASS] {c['code']:<10} R20 = {pv if pv is not None else '—'}")
    if bad:
        fails += 1

    print("\n" + "=" * 78)
    if fails == 0:
        print("结论：JS 引擎与 Python 引擎完全一致（5 个时点 + 因子抽查）")
    else:
        print(f"结论：{fails} 处不一致，必须修正后再打包")
    print("=" * 78)
    sys.exit(0 if fails == 0 else 1)


if __name__ == "__main__":
    main()
