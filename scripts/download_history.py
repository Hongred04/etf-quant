"""
下载 ETF 历史前复权日线并落盘缓存。

用法
----
python scripts/download_history.py --start 2018-01-01 --end 2026-10-23 --top 260
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from etfquant.datafeed import CacheConfig, download_panel
from etfquant.io_utils import atomic_to_csv
from etfquant.universe import classify_etf, dedup_same_index, universe_summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2018-01-01")
    # 默认取今天：写死日期会让"更新行情"永远停在某一天之前，越用越旧
    ap.add_argument("--end", default=pd.Timestamp.today().strftime("%Y-%m-%d"))
    ap.add_argument("--top", type=int, default=260)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    snap_fp = ROOT / "data" / "etf_snapshot.csv"
    snap = pd.read_csv(snap_fp, dtype={"ticker": str})
    snap["category"] = snap["name"].map(classify_etf)

    # 剔除货币类（收益极低，只作为现金替代，不参与轮动）
    pool = snap[snap["category"] != "货币"].copy()
    n_before = len(pool)
    pool = dedup_same_index(pool)
    print(f"[INFO] 剔除货币后 {n_before} 只，同指数去重后 {len(pool)} 只")

    pool = pool.sort_values("amount_yuan", ascending=False).head(args.top).reset_index(drop=True)
    pool.to_csv(ROOT / "data" / "etf_pool.csv", index=False, encoding="utf-8-sig")
    print(f"[INFO] 待下载 {len(pool)} 只，区间 {args.start} ~ {args.end}")
    print(universe_summary(pool).to_string())

    cache = CacheConfig(root=ROOT / "data" / "cache")
    t0 = time.time()
    report: dict = {}
    frames = download_panel(
        pool["ticker"].tolist(), args.start, args.end, cache,
        workers=8, refresh=args.refresh, min_bars=60, report=report,
    )
    n_ok, n_all = len(frames), len(pool)
    # 全挂 / 大半挂必须当失败：否则 refresh 会拿旧数据继续回测并"成功"，
    # 最后表现为"更新了但数字没变"，比直接报错难查得多。
    # 2026-10-30 腾讯对 fqkline 整段返回 501 时就是这种情况。
    if n_ok == 0:
        print("[FAIL] 一只都没抓到——接口限流或网络不通。缓存与产物均未改动，稍后重试。")
        return 1
    if n_ok < n_all * 0.5:
        print(f"[FAIL] 只抓到 {n_ok}/{n_all}（超过半数失败），按失败处理："
              f"不写清单，避免下游把残缺数据当成最新。")
        return 2

    # 当日 K 线收得比别家慢：只落库一部分时整天丢弃（缓存已同步裁掉那一天），
    # 并按失败返回——否则 refresh 会继续拿昨天的数据回测，表现为"更新了但数字没变"。
    partial = report.get("partial_day") or {}
    if partial.get("applied"):
        print(f"[FAIL] {partial['date']} 的日线只落库 {partial['have']}/{partial['total']} 只"
              f"（{partial['coverage'] * 100:.0f}%，门槛 {partial['min_coverage'] * 100:.0f}%），"
              f"已整天丢弃（{partial['dropped']} 只缓存已回写裁剪）。")
        print("       收盘后数据是分批落库的，等 5~10 分钟再点一次更新即可。"
              "现在回测会停在上一交易日，不会拿半天数据算收益。")
        return 3

    print(f"[OK] 下载完成 {n_ok}/{n_all} 只，耗时 {time.time() - t0:.1f}s"
          f"（期望覆盖到 {report.get('expected_date', '-')}）")

    # 失败与缺口必须显式暴露：这些标的的数据不可信，报告与信号都不能当它没问题
    failed = report.get("failed") or {}
    gaps = report.get("gaps") or {}
    if failed:
        print(f"[WARN] {len(failed)} 只标的抓取失败（未写缓存，下次会重试）：")
        for tk, msg in list(failed.items())[:10]:
            print(f"       {tk}  {msg}")
    if gaps:
        print(f"[WARN] {len(gaps)} 只标的存在超过 45 天的数据缺口：")
        for tk, d in list(gaps.items())[:10]:
            print(f"       {tk}  最大缺口 {d} 天")

    # 落盘一份可读清单：代码/名称/类别/首末日期/样本数
    rows = []
    meta = pool.set_index("ticker")
    for tk, df in frames.items():
        rows.append(
            {
                "ticker": tk,
                "name": meta.loc[tk, "name"] if tk in meta.index else "",
                "category": meta.loc[tk, "category"] if tk in meta.index else "",
                "start": df["date"].min().date(),
                "end": df["date"].max().date(),
                "bars": len(df),
            }
        )
    inv = pd.DataFrame(rows).sort_values("bars", ascending=False)
    atomic_to_csv(inv, ROOT / "data" / "etf_history_inventory.csv", index=False, encoding="utf-8-sig")
    print(inv.head(15).to_string(index=False))
    print(f"\n[OK] 清单写入 data/etf_history_inventory.csv")
    return 0


if __name__ == "__main__":
    # 必须把返回值传给退出码：只写 main() 时 return 1/2/3 会被丢弃，
    # `refresh` 看不到失败就会拿旧数据继续回测。
    raise SystemExit(main())
