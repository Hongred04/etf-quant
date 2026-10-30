"""
ETF 份额（申赎）日度采集。

为什么是"采集"而不是"因子"
--------------------------
7.14 的可达性探测结论：上交所与深交所都只提供**当日/最新一日**的份额快照，
日期参数被忽略（试过 tradeDate / queryDate / beginDate / txtDate / statisticsDate，
返回的 `STAT_DATE` 始终是最新交易日）。天天基金的"规模变动"页只有季度报告期数据。
也就是说：**历史份额序列在可达的数据源里不存在**，这条策略没法回测。

但快照本身是每天在**白白流失**的：今天不记，明天就再也拿不回来。
所以这里做的是最低限度的采集器——一天一次、追加、去重、原子写。
攒够 120 个交易日（约半年）之后，才有资格谈"资金流因子"。

口径
----
* 上交所：`COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L`，字段 `STAT_DATE / SEC_CODE / SEC_NAME / TOT_VOL`
* 深交所：`ShowReport CATALOGID=1945`（ETF 列表），字段 `dqgm`（当前规模）、`sys_key`（代码）、`kzjcurl`（简称）
* 两边单位都是**万份**（用 510300、159915 这类大标的的市值/净值反算过量级，见 README 7.14）
* 日期取**交易所返回的日期**，不是本机日期——否则在数据未发布时会把前天快照记成今天
"""
from __future__ import annotations

import gzip
import json
import re
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

from etfquant.io_utils import atomic_to_csv

SZSE_URL = ("https://www.szse.cn/api/report/ShowReport/data"
            "?SHOWTYPE=JSON&CATALOGID=1945&loading=first")
SSE_URL = ("https://query.sse.com.cn/commonQuery.do"
           "?sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L"
           "&isPagination=true&pageHelp.pageSize=1000&pageHelp.pageNo=1"
           "&pageHelp.beginPage=1&pageHelp.endPage=1")

_HEAD = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "*/*"}
_TAG = re.compile(r"<[^>]+>")


def _http_json(url: str, referer: str, retries: int = 2, pause: float = 0.6) -> dict | list:
    """带重试的 JSON 取数。失败抛 RuntimeError（调用方决定是否忽略）。"""
    last: Exception | None = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={**_HEAD, "Referer": referer})
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8", "ignore"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(pause * (i + 1))
    raise RuntimeError(f"取数失败：{url.split('?')[0]} {type(last).__name__}: {last}")


def _num(x) -> float:
    """'15,173.52' / '-' / '' → float。非数字一律 NaN，不当成 0。"""
    s = _TAG.sub("", str(x)).replace(",", "").strip()
    try:
        v = float(s)
    except (TypeError, ValueError):
        return float("nan")
    return v if np.isfinite(v) and v > 0 else float("nan")


def parse_szse(payload: list | dict, ) -> tuple[pd.DataFrame, str]:
    """
    深交所 ETF 列表 → (date, ticker, name, shares_wan)。返回 (表, 快照日期)。

    只做字段搬运，不做任何推断：`dqgm` 直接当份额（万份），名字从 `kzjcurl` 的
    `title='...'` 或 `<u>` 文本里取，取不到就留空（不编）。
    """
    block = payload[0] if isinstance(payload, list) else payload
    date = str((block.get("metadata") or {}).get("subname") or "").strip()
    rows = []
    for r in block.get("data") or []:
        code = _TAG.sub("", str(r.get("sys_key", ""))).strip()
        if not re.fullmatch(r"\d{6}", code):
            continue
        name = ""
        m = re.search(r"title=['\"]([^'\"]+)['\"]", str(r.get("kzjcurl", "")))
        if m:
            name = m.group(1)
        else:
            name = _TAG.sub("", str(r.get("kzjcurl", ""))).strip()
        rows.append({"date": date, "ticker": f"sz{code}", "name": name,
                     "shares_wan": _num(r.get("dqgm")), "source": "szse"})
    return pd.DataFrame(rows), date


def parse_sse(payload: dict) -> tuple[pd.DataFrame, str]:
    """上交所 ETF 规模 → (date, ticker, name, shares_wan)。日期取 `STAT_DATE`。"""
    res = payload.get("result") or []
    rows = []
    date = ""
    for r in res:
        code = str(r.get("SEC_CODE") or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            continue
        date = date or str(r.get("STAT_DATE") or "").strip()
        rows.append({"date": date, "ticker": f"sh{code}",
                     "name": str(r.get("SEC_NAME") or "").strip(),
                     "shares_wan": _num(r.get("TOT_VOL")), "source": "sse"})
    return pd.DataFrame(rows), date


def fetch_szse(pages: int = 90, pause: float = 0.25) -> tuple[pd.DataFrame, str]:
    """深交所一页只有 10 条，必须翻页（768 条 ≈ 77 页）。"""
    frames, date = [], ""
    for page in range(1, pages + 1):
        try:
            payload = _http_json(f"{SZSE_URL}&PAGENO={page}", "https://www.szse.cn/")
        except RuntimeError:
            break
        df, d = parse_szse(payload)
        date = date or d
        if df.empty:
            break
        frames.append(df)
        if len(df) < 10:          # 最后一页
            break
        last_date = (payload[0].get("metadata") or {}).get("subname")
        if d != last_date:
            break
        time.sleep(pause)
    if not frames:
        return pd.DataFrame(), ""
    return pd.concat(frames, ignore_index=True), date


def fetch_sse() -> tuple[pd.DataFrame, str]:
    payload = _http_json(SSE_URL, "https://www.sse.com.cn/")
    return parse_sse(payload)


def fetch_snapshot() -> pd.DataFrame:
    """取两边快照并合并成一张表。任一边失败不影响另一边（记录在 source 列）。"""
    frames = []
    errors = []
    for name, fn in (("sse", fetch_sse), ("szse", fetch_szse)):
        try:
            df, _ = fn()
            if not df.empty:
                frames.append(df)
        except RuntimeError as exc:
            errors.append(f"{name}: {exc}")
    if not frames:
        raise RuntimeError("两个交易所都没取到数据：" + "；".join(errors))
    out = pd.concat(frames, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out = out[out["date"].notna() & out["shares_wan"].notna()]
    return out.drop_duplicates(subset=["date", "ticker"], keep="last").reset_index(drop=True)


def append_snapshot(df: pd.DataFrame, fp: Path) -> dict:
    """
    追加到 CSV（按 date+ticker 去重，保留最后一次）。返回本次新增行数与总行数。

    原子写：采集是每天跑的，中途失败不能留下半张表——那会让"某天份额腰斩"
    这种假信号进入将来任何一次研究。
    """
    fp = Path(fp)
    old = pd.read_csv(fp, dtype={"ticker": str}, parse_dates=["date"]) if fp.exists() else pd.DataFrame()
    merged = pd.concat([old, df], ignore_index=True) if not old.empty else df.copy()
    merged["date"] = pd.to_datetime(merged["date"])
    merged = (merged.drop_duplicates(subset=["date", "ticker"], keep="last")
              .sort_values(["date", "ticker"]).reset_index(drop=True))
    n_new = len(merged) - len(old)
    atomic_to_csv(merged, fp, index=False)
    return {"new_rows": int(n_new), "total_rows": int(len(merged)),
            "dates": int(merged["date"].nunique())}
