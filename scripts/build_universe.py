"""扫描全市场 ETF 并落盘标的池快照。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etfquant.universe import discover_etfs, universe_summary

OUT = Path(__file__).resolve().parents[1] / "data"


def main() -> None:
    t0 = time.time()
    df = discover_etfs()
    OUT.mkdir(parents=True, exist_ok=True)
    fp = OUT / "etf_snapshot.csv"
    df.to_csv(fp, index=False, encoding="utf-8-sig")
    print(f"[OK] 扫描耗时 {time.time() - t0:.1f}s，命中 ETF {len(df)} 只 -> {fp}")
    print("\n=== 资产类别分布 ===")
    print(universe_summary(df).to_string())
    print("\n=== 成交额前 20 ===")
    cols = ["ticker", "name", "price", "pct_chg", "amount_yuan", "category"]
    print(df.head(20)[cols].to_string(index=False))


if __name__ == "__main__":
    main()
