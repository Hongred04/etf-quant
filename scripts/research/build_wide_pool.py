"""
试验 7.19 第一步：生成更宽的候选池（400 / 721），**不触碰现有池与清单**。

为什么单独一个脚本
------------------
`scripts/download_history.py` 会覆盖 `data/etf_pool.csv` 并重写
`data/etf_history_inventory.csv`，而这两个文件是 server 与全部既有结论的输入。
扩池实验只应该往 `data/cache/` 里加数据，绝不能改动基线池。

流程
----
1. 从快照取「非货币 + 去同指数重复 + 成交额前 N」的候选；
2. 只下载尚未缓存的部分（已有缓存走增量）；
3. 用完整缓存重新跑 build_universe，生成 etf_pool_400.csv / etf_pool_721.csv。

用法：python scripts/research/build_wide_pool.py --sizes 400 721
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from etfquant.datafeed import CacheConfig, download_panel
from etfquant.io_utils import atomic_to_csv
from etfquant.pipeline import load_panel
from etfquant.universe import build_universe, dedup_same_index

START = "2018-01-01"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", type=int, nargs="+", default=[400, 721])
    ap.add_argument("--top", type=int, default=721, help="候选池上限（按成交额）")
    args = ap.parse_args()

    inv = pd.read_csv(ROOT / "data" / "etf_history_inventory.csv", dtype=str)
    vals = [v for v in inv["end"].dropna().tolist() if len(str(v)) == 10]
    end = max(set(vals), key=vals.count)

    snap = pd.read_csv(ROOT / "data" / "etf_snapshot.csv", dtype={"ticker": str})
    pool = snap[snap["category"] != "货币"].copy()
    pool = dedup_same_index(pool)
    pool = pool.sort_values("amount_yuan", ascending=False).head(args.top).reset_index(drop=True)

    cached = {p.stem for p in (ROOT / "data" / "cache" / "kline_qfq").glob("*.csv")}
    todo = [t for t in pool["ticker"] if t not in cached]
    print(f"候选 {len(pool)} 只，已缓存 {len(pool) - len(todo)} 只，需下载 {len(todo)} 只")

    if todo:
        report: dict = {}
        frames = download_panel(todo, START, end, CacheConfig(root=ROOT / "data" / "cache"),
                               workers=8, min_bars=60, report=report)
        print(f"下载完成 {len(frames)}/{len(todo)}；"
              f"partial_day={bool((report.get('partial_day') or {}).get('applied'))}")
        failed = report.get("failed") or {}
        if failed:
            print(f"  [WARN] {len(failed)} 只失败（不写缓存，池子会相应变小）")

    # 用「已被缓存」的实际标的集合重建，而不是信任候选名单
    panel = load_panel(ROOT / "data" / "etf_pool.csv", ROOT / "data" / "cache", START, end)
    have = set(panel.close.columns)
    for size in args.sizes:
        uni = build_universe(snap, panel.close, max_size=size)
        uni = uni[uni["ticker"].isin(have)]
        fp = ROOT / "data" / f"etf_pool_{size}.csv"
        atomic_to_csv(uni, fp, index=False, encoding="utf-8-sig")
        print(f"etf_pool_{size}.csv：{len(uni)} 只（max_size={size}，受实际缓存限制）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
