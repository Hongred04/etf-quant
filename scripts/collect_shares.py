"""
采集 ETF 份额（申赎）日快照，追加到 data/etf_shares.csv。

为什么做这个（而不是做策略）
----------------------------
7.14 的探测结论：上交所/深交所都只给**最新一日**的份额，历史不可得，
所以"资金流因子"现在无法回测。但快照每天在流失——今天不记，明天拿不回来。
这里只做最低限度的事：一天一次、追加、去重、原子写。

用法
----
python scripts/collect_shares.py            # 采一次
python -m etfquant shares                   # 同上（CLI 入口）
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from etfquant import shares as S  # noqa: E402

FP = ROOT / "data" / "etf_shares.csv"


def main() -> int:
    t0 = time.time()
    try:
        snap = S.fetch_snapshot()
    except RuntimeError as exc:
        print(f"[FAIL] {exc}")
        return 1
    if snap.empty:
        print("[FAIL] 取到 0 行（接口改版或限流），未写入。")
        return 1

    got = S.append_snapshot(snap, FP)

    # 与当前池对照：覆盖多少只是"这个数据将来能不能用"的直接指标
    n_pool, n_hit = 0, 0
    pool_fp = ROOT / "data" / "etf_pool.csv"
    if pool_fp.exists():
        pool = pd.read_csv(pool_fp, dtype={"ticker": str})["ticker"]
        n_pool = len(pool)
        n_hit = int(snap["ticker"].isin(set(pool)).sum())

    dates = sorted(snap["date"].dt.strftime("%Y-%m-%d").unique())
    print(f"[OK] 快照日期 {', '.join(dates)}；本次 {len(snap)} 条，"
          f"池内覆盖 {n_hit}/{n_pool} 只")
    print(f"     写入 {FP.name}：新增 {got['new_rows']} 行，累计 {got['total_rows']} 行 / "
          f"{got['dates']} 个交易日（{time.time()-t0:.1f}s）")
    if got["dates"] < 120:
        print(f"     提示：满 120 个交易日（约半年）才具备做因子检验的样本量，"
              f"当前 {got['dates']} 天。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
