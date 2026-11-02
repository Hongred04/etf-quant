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


def _write_inventory(pool: pd.DataFrame, frames: dict, root: Path) -> None:
    """把**缓存现状**写进清单（data/etf_history_inventory.csv）。

    清单必须反映"实际落盘的数据"，而不是"这次下载想拿到的数据"：
    strip_partial_day 会把覆盖不足的那一天从缓存里删掉，若此时不刷新清单，
    界面就会一直认为行情已经到那天——2026-10-30 正是这样卡住的
    （清单记 10-30，缓存实际停在 10-29，于是"回测落后"提示永远消不掉，
    用户重跑多少次回测都没用，因为缺的是行情不是回测）。

    本次没下载成功的标的保留上一次的记录，避免一次网络抖动把清单打残。
    """
    inv_fp = root / "data" / "etf_history_inventory.csv"
    prev: dict[str, dict] = {}
    if inv_fp.exists():
        try:
            old = pd.read_csv(inv_fp, dtype=str)
            if {"ticker", "end"} <= set(old.columns):
                prev = old.set_index("ticker").to_dict("index")
        except (OSError, ValueError, KeyError):
            prev = {}
    meta = pool.set_index("ticker")
    rows = []
    for tk in pool["ticker"].tolist():
        df = frames.get(tk)
        if df is not None and len(df) and "date" in df:
            rows.append({
                "ticker": tk,
                "name": meta.loc[tk, "name"] if tk in meta.index else "",
                "category": meta.loc[tk, "category"] if tk in meta.index else "",
                "start": df["date"].min().date(),
                "end": df["date"].max().date(),
                "bars": len(df),
            })
        elif tk in prev:
            rows.append({"ticker": tk, **{k: v for k, v in prev[tk].items() if k != "ticker"}})
    if not rows:
        return
    inv = pd.DataFrame(rows).sort_values("bars", ascending=False)
    atomic_to_csv(inv, inv_fp, index=False, encoding="utf-8-sig")
    end_cnt = inv["end"].value_counts()
    top = f"{end_cnt.index[0]}（{end_cnt.iloc[0]} 只）" if len(end_cnt) else "-"
    print(f"[OK] 清单已按缓存现状刷新：最新交易日 {top}")


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
              f"清单按实际缓存刷新，下游仍以清单为准。")
        # 成功的那些标的缓存已经更新了，清单要如实反映；否则界面日期会与缓存脱节
        _write_inventory(pool, frames, ROOT)
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
        # 关键：缓存已被裁剪，清单必须跟着退回上一交易日，否则界面永远认为
        # 行情已到这天，"回测落后"提示消不掉（用户重跑回测也没用）。
        _write_inventory(pool, frames, ROOT)
        return 3

    print(f"[OK] 下载完成 {n_ok}/{n_all} 只，耗时 {time.time() - t0:.1f}s"
          f"（期望覆盖到 {report.get('expected_date', '-')}）")

    # 失败与缺口必须显式暴露：这些标的的数据不可信，报告与信号都不能当它没问题
    failed = report.get("failed") or {}
    gaps = report.get("gaps") or {}
    reanchored = report.get("reanchored") or {}
    if reanchored:
        # 分红/拆分后供应商会把全部历史按新因子重算；这是自动整段重下的记录，
        # 不是错误，但必须可见——否则用户不知道缓存被整体改写过。
        print(f"[WARN] {len(reanchored)} 只标的的前复权被供应商重锚，已整段重下：")
        for tk, r in list(reanchored.items())[:10]:
            print(f"       {tk}  新旧比价 {r:.4f}")
    if failed:
        print(f"[WARN] {len(failed)} 只标的抓取失败（未写缓存，下次会重试）：")
        for tk, msg in list(failed.items())[:10]:
            print(f"       {tk}  {msg}")
    if gaps:
        print(f"[WARN] {len(gaps)} 只标的存在超过 45 天的数据缺口：")
        for tk, d in list(gaps.items())[:10]:
            print(f"       {tk}  最大缺口 {d} 天")

    # 落盘一份可读清单：代码/名称/类别/首末日期/样本数（统一走 _write_inventory）
    _write_inventory(pool, frames, ROOT)
    print(pd.read_csv(ROOT / "data" / "etf_history_inventory.csv").head(15).to_string(index=False))
    return 0


if __name__ == "__main__":
    # 必须把返回值传给退出码：只写 main() 时 return 1/2/3 会被丢弃，
    # `refresh` 看不到失败就会拿旧数据继续回测。
    raise SystemExit(main())
