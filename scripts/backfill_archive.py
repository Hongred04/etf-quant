"""
回填历史信号归档（source=backfill）。

用途：归档机制是 2026-10 才加的，之前没有任何"当时的建议"可查。
为了让「实盘跟踪」页一开始不是空白，可以用当前策略把历史调仓日的信号回放一遍。

**这些记录不等于当时真实给出的建议**：数据修订、参数调整之后重算会得到不同结果。
所以写库时 source 标成 backfill，页面上与 live 分开显示，且 backfill 永远不会覆盖
同一天的 live 记录。

用法：
    python scripts/backfill_archive.py                  # 回填全部历史调仓日
    python scripts/backfill_archive.py --since 2026-01-01
    python scripts/backfill_archive.py --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from etfquant.pipeline import load_panel
from etfquant.strategy import TrendGatedAdaptive
from etfquant.track import archive_path, load_archive, upsert_signal


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="只回填该日期之后（含）的调仓日")
    ap.add_argument("--strategy", default="s4_tgar")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    panel = load_panel(ROOT / "data" / "etf_pool.csv", ROOT / "data" / "cache",
                       "2018-01-02", "2026-12-31")
    strat = TrendGatedAdaptive(panel)
    targets = strat.generate()
    fp = archive_path(ROOT / "data")
    existing = load_archive(fp)
    live_dates = set(existing.loc[existing["source"] == "live", "signal_date"].astype(str))

    idx = list(panel.close.index)
    n_periods = n_rows = 0
    for d, w in sorted(targets.items()):
        ds = str(d.date())
        if args.since and ds < args.since:
            continue
        if ds in live_dates:
            continue
        pos = idx.index(d) if d in idx else -1
        if pos < 0 or pos >= len(idx) - 1:
            continue
        rows = []
        for code, wt in w.items():
            if wt <= 1e-6:
                continue
            name = panel.meta.loc[code, "name"] if code in panel.meta.index else code
            cat = panel.meta.loc[code, "category"] if code in panel.meta.index else ""
            px = panel.close.at[d, code]
            rows.append({
                "signal_date": ds,
                "exec_date": str(idx[pos + 1].date()),
                "code": str(code),
                "name": str(name),
                "category": str(cat),
                "weight": float(wt),
                "ref_price": float(px) if pd.notna(px) else None,
            })
        if not rows:
            continue
        n_periods += 1
        n_rows += len(rows)
        if not args.dry_run:
            upsert_signal(fp, rows, source="backfill")

    tail = "（dry-run，未写入）" if args.dry_run else ""
    print(f"[OK] 回填 {n_periods} 期 / {n_rows} 条 → {fp.relative_to(ROOT)}{tail}")
    print("     这些记录的 source=backfill，页面上与 live 分开显示；不会覆盖同日的 live 记录。")


if __name__ == "__main__":
    main()
