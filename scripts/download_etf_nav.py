"""
下载 ETF 历史单位净值（同花顺，经 akshare），用于计算场内溢价率。

为什么需要它
------------
场内 ETF 的溢价率 = (收盘价 − IOPV) / IOPV。腾讯行情不提供 IOPV，所以
回测里一直无法判断"这笔买入是不是买在了高溢价上"。例子：159509 纳指科技在
2024-06-20 的溢价是 +20.5%——动量策略会优先选中它，然后吃下溢价收敛的亏损。

数据源
------
`akshare.fund_etf_spot_ths(date=YYYYMMDD)` 走**同花顺**基金数据中心
（fund.10jqka.com.cn，本机可达；东财源不可达）。按日期返回全市场约 1774 只 ETF 的
当日单位净值与累计净值，约 1 秒/天。

两个必须记住的坑
----------------
1. **分红会伪造折价**：价格是除权后的，而「单位净值」不含已分红部分——
   510300 在 2024-06-20 算出 −5.96%，实为分红除权所致，不是真折价。
   跨境 ETF 分红极少，用于溢价判断时影响小；境内标的要谨慎。
2. **QDII 净值有 T+1/T+2 延迟**：查"今天"时 QDII 的净值常为 NaN，
   历史日期则是完整的。所以本脚本只回补历史，不用于当日实时判断。

用法
----
python scripts/download_etf_nav.py                 # 增量补齐到最新
python scripts/download_etf_nav.py --start 2021-01-01
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from etfquant.io_utils import atomic_to_csv

OUT_FP = ROOT / "data" / "etf_nav_history.csv"
COLUMNS = ["date", "ticker", "nav", "acc_nav"]


def _load_existing() -> pd.DataFrame:
    if not OUT_FP.exists():
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_csv(OUT_FP, dtype={"ticker": str})
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df.dropna(subset=["date"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01-02")
    ap.add_argument("--sleep", type=float, default=0.35, help="每日请求间隔（秒）")
    args = ap.parse_args()

    # 只抓池内标的：全市场 1774 只但回测只用这 260 只，数据量差 7 倍
    pool = pd.read_csv(ROOT / "data" / "etf_pool.csv", dtype={"ticker": str})
    want = {t[2:] if t[:2] in ("sh", "sz") else t for t in pool["ticker"]}
    print(f"池内 {len(want)} 只；净值库已有 {len(_load_existing())} 行")

    # 交易日历取自基准 ETF 的缓存（本地已有，不额外请求）
    inv = pd.read_csv(ROOT / "data" / "etf_history_inventory.csv", dtype=str)
    vals = [v for v in inv["end"].dropna().tolist() if len(str(v)) == 10]
    end = max(set(vals), key=vals.count)
    px = pd.read_csv(ROOT / "data" / "cache" / "kline_qfq" / "sh510300.csv")
    px["date"] = pd.to_datetime(px["date"], errors="coerce")
    days = [d for d in px["date"].dropna().sort_values()
            if pd.Timestamp(args.start) <= d <= pd.Timestamp(end)]
    print(f"待补交易日 {len(days)} 天（{days[0].date()} ~ {days[-1].date()}）")

    existing = _load_existing()
    have = set(existing["date"].dt.strftime("%Y-%m-%d")) if len(existing) else set()
    todo = [d for d in days if d.strftime("%Y-%m-%d") not in have]
    print(f"其中已抓 {len(days) - len(todo)} 天，本次需抓 {len(todo)} 天"
          f"（预计 {len(todo) * (args.sleep + 1) / 60:.0f} 分钟）")
    if not todo:
        print("已是最新，无需抓取")
        return 0

    try:
        import akshare as ak
    except ImportError:
        print("[ERR] 需要 akshare：python -m pip install akshare")
        return 1

    rows, failed = [], 0
    chunk_start = time.time()

    def _flush() -> None:
        """分批落盘。

        第一版只在循环结束后写一次文件，结果：33 分钟里既看不到进度，
        一旦中断就全部作废。长任务必须边跑边存。
        """
        nonlocal rows
        if not rows:
            return
        new = pd.DataFrame(rows, columns=COLUMNS)
        base = _load_existing()
        merged = pd.concat([base, new], ignore_index=True) if len(base) else new
        merged = merged.dropna(subset=["nav"]).drop_duplicates(["date", "ticker"], keep="last")
        atomic_to_csv(merged.sort_values(["date", "ticker"]), OUT_FP,
                      index=False, encoding="utf-8-sig")
        rows = []

    for i, d in enumerate(todo, 1):
        try:
            df = ak.fund_etf_spot_ths(date=d.strftime("%Y%m%d"))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            if failed <= 3:
                print(f"  [WARN] {d.date()} 抓取失败：{type(exc).__name__} {str(exc)[:60]}",
                      flush=True)
            time.sleep(args.sleep * 2)
            continue
        if df is None or df.empty:
            failed += 1
            continue
        sub = df[df["基金代码"].astype(str).isin(want)]
        for _, r in sub.iterrows():
            rows.append({
                "date": d.strftime("%Y-%m-%d"),
                "ticker": str(r["基金代码"]),
                "nav": pd.to_numeric(r.get("当前-单位净值"), errors="coerce"),
                "acc_nav": pd.to_numeric(r.get("当前-累计净值"), errors="coerce"),
            })
        if i % 50 == 0:
            _flush()
            el = time.time() - chunk_start
            print(f"  {i}/{len(todo)} 天，已落盘，用时 {el / 60:.1f} 分钟，"
                  f"预计还需 {el / i * (len(todo) - i) / 60:.1f} 分钟", flush=True)
        time.sleep(args.sleep)

    _flush()                      # 把最后不足 50 天的那批也写进去
    if not OUT_FP.exists():
        print(f"[FAIL] 一行都没拿到（失败 {failed} 天），不写文件")
        return 2

    # 数据已在 _flush 里合并落盘，这里只报最终规模，不要再拼一次
    total = len(_load_existing())
    print(f"\n[OK] {OUT_FP.relative_to(ROOT)} 现有 {total} 行（失败 {failed} 天）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
