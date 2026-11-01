"""
试验 7.17 补充：股债累计收益比的**起点敏感性**（第二个外部模型的否证）。

"企业回报 vs 资金成本"模型用"红线（股）跌破蓝线（债）"作为低估信号。
本脚本证明该信号不是市场状态，而是**基期的函数**：同一组价格，只换起点，
"低于债券"的时间占比从 0.1% 到 99.9%。

同时给出费用修正口径（给 ETF 加回管理费，近似无费用的全收益指数），
排除"用 ETF 代理指数"这一混淆项。

用法：python scripts/research/startpoint_sensitivity.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from etfquant.pipeline import load_panel

EQUITY, BOND = "sh510300", "sh511260"
FEE_EQUITY, FEE_BOND = 0.005, 0.0015      # 年化管理费，用于还原"全收益指数"口径
STARTS = ["2018-01-02", "2019-01-02", "2020-01-02", "2021-01-04", "2022-01-04"]


def main() -> int:
    inv = pd.read_csv(ROOT / "data" / "etf_history_inventory.csv", dtype=str)
    vals = [v for v in inv["end"].dropna().tolist() if len(str(v)) == 10]
    end = max(set(vals), key=vals.count)

    panel = load_panel(ROOT / "data" / "etf_pool.csv", ROOT / "data" / "cache", "2018-01-02", end)
    df = pd.concat([panel.close[EQUITY].rename("e"), panel.close[BOND].rename("b")], axis=1).dropna()

    rows = []
    for start in STARTS:
        s = df[df.index >= start]
        if len(s) < 60:
            continue
        for label, adj in (("ETF前复权", False), ("加回管理费", True)):
            ne = (s["e"] / s["e"].iloc[0]).to_numpy()
            nb = (s["b"] / s["b"].iloc[0]).to_numpy()
            if adj:
                yrs = (s.index - s.index[0]).days / 365.25
                ne = ne * (1 + FEE_EQUITY) ** yrs.to_numpy()
                nb = nb * (1 + FEE_BOND) ** yrs.to_numpy()
            ratio = ne / nb
            rows.append({
                "start": start, "口径": label, "区间末日": s.index[-1].date(),
                "交易日": len(s), "期末比值": round(float(ratio[-1]), 4),
                "低于债券占比": round(float((ratio < 1).mean()), 4),
                "最小比值": round(float(ratio.min()), 4),
            })

    out = pd.DataFrame(rows)
    fp = ROOT / "output" / "experiments" / "startpoint_sensitivity.csv"
    out.to_csv(fp, index=False, encoding="utf-8-sig")
    print(out.to_string(index=False))
    span = out.groupby("口径")["低于债券占比"].agg(["min", "max"])
    print("\n同一组价格、只换起点，'低于债券'占比区间：")
    for k, r in span.iterrows():
        print(f"  {k}: {r['min'] * 100:.1f}% ~ {r['max'] * 100:.1f}%")
    print(f"\n已写入 {fp.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
