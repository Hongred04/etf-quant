"""
腾讯财经行情数据源（TencentQuoteFeed）。

设计说明
--------
本环境无法直连东方财富 / 新浪 / GitHub，实测可用通道为腾讯财经：
  1) 实时快照（批量）：https://qt.gtimg.cn/q=sh510300,sh510500,...
  2) 前复权日线：      https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh510300,day,2019-01-01,2020-12-31,640,qfq

接口约束（已实测）：
  * 批量快照单请求约支持 60~80 个代码，60 个耗时约 0.2s；
  * 日线单次最多返回 640 根 K 线，因此需要按时间窗口分段拼接；
  * 返回字段为 GBK 编码的 `~` 分隔串，需自行解析。

数据口径：
  价格为前复权（qfq），保证分红送转后收益率序列连续；
  涨跌幅、成交额等字段直接取接口原始值。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .io_utils import atomic_to_csv

QT_URL = "https://qt.gtimg.cn/q="
KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param="

# K 线的多个入口。**只选同厂商不同域名**：不同厂商的复权因子不同，
# 混用会在缓存拼接处产生跳空，而跳空会被当成真实收益。
# 两个入口的 qfqday 已逐位比对过（开高低收最大绝对差 0.000000、量比 1.0000）。
KLINE_HOSTS: tuple[tuple[str, str], ...] = (
    ("web.ifzq.gtimg.cn",
     "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param="),
    ("proxy.finance.qq.com",
     "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?param="),
)
# 单个入口约 600 次请求后会整段返回 501（2026-10-30 实测：260 只 × 19 窗口直接打爆），
# 所以按请求轮换，而且失败一次就冷却 5 分钟——重试同一个死入口只是白等。
_HOST_DOWN_UNTIL: dict[str, float] = {}
_HOST_RR = {"next": 0}
_HOST_LOCK = threading.Lock()
_HOST_COOLDOWN = 300.0
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Referer": "https://gu.qq.com/",
}

# 快照字段索引（腾讯 v_ 串以 ~ 分隔）
IDX = {
    "name": 1,
    "code": 2,
    "price": 3,
    "prev_close": 4,
    "open": 5,
    "volume": 6,      # 手
    "time": 30,
    "change": 31,
    "pct_chg": 32,    # %
    "high": 33,
    "low": 34,
    "amount": 37,     # 万元
    "turnover": 38,   # %
    "float_mv": 44,   # 亿元
    "total_mv": 45,   # 亿元
}


def _http_get(url: str, timeout: int = 15, encoding: str = "utf-8", retries: int = 3) -> str:
    """带重试的 GET，返回解码后的文本。"""
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode(encoding, errors="ignore")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:  # noqa: PERF203
            last_err = exc
            time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"HTTP 请求失败: {url[:100]} ... ({last_err})")


def _host_order() -> list[tuple[str, str]]:
    """给本次请求排一个入口顺序：跳过冷却中的，其余轮换起点以分摊请求量。"""
    now = time.time()
    with _HOST_LOCK:
        alive = [h for h in KLINE_HOSTS if _HOST_DOWN_UNTIL.get(h[0], 0.0) <= now]
        if not alive:                       # 全挂了就全部再试一次，别把自己锁死
            for h in KLINE_HOSTS:
                _HOST_DOWN_UNTIL.pop(h[0], None)
            alive = list(KLINE_HOSTS)
        i = _HOST_RR["next"] % len(alive)
        _HOST_RR["next"] += 1
        return alive[i:] + alive[:i]


def _mark_host_down(host: str) -> None:
    with _HOST_LOCK:
        _HOST_DOWN_UNTIL[host] = time.time() + _HOST_COOLDOWN


def endpoint_health() -> dict[str, str]:
    """给 doctor / 审计看：哪些入口还活着。"""
    now = time.time()
    out = {}
    for name, _ in KLINE_HOSTS:
        left = _HOST_DOWN_UNTIL.get(name, 0.0) - now
        out[name] = "ok" if left <= 0 else f"cooldown {left:.0f}s"
    return out


def reset_endpoint_health() -> None:
    """清空入口健康状态（测试用）。"""
    with _HOST_LOCK:
        _HOST_DOWN_UNTIL.clear()
        _HOST_RR["next"] = 0


def _to_float(x: str) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return np.nan


# --------------------------------------------------------------------------- #
# 快照
# --------------------------------------------------------------------------- #
def parse_snapshot(text: str) -> list[dict]:
    """解析 `v_sh510300="1~沪深300ETF~510300~..."` 形式的返回串。"""
    rows: list[dict] = []
    for seg in text.split(";"):
        seg = seg.strip()
        if not seg or "=" not in seg:
            continue
        key, val = seg.split("=", 1)
        fields = val.strip().strip('"').split("~")
        if len(fields) < 46:
            continue
        ticker = key.split("_", 1)[-1].strip()
        if not ticker:
            continue
        rows.append(
            {
                "ticker": ticker,
                "name": fields[IDX["name"]].strip(),
                "price": _to_float(fields[IDX["price"]]),
                "prev_close": _to_float(fields[IDX["prev_close"]]),
                "pct_chg": _to_float(fields[IDX["pct_chg"]]),
                "amount_wan": _to_float(fields[IDX["amount"]]),
                "turnover_pct": _to_float(fields[IDX["turnover"]]),
                "float_mv_yi": _to_float(fields[IDX["float_mv"]]),
                "quote_time": fields[IDX["time"]].strip(),
            }
        )
    return rows


def fetch_snapshot(
    tickers: list[str], batch: int = 60, timeout: int = 15, retries: int = 3
) -> pd.DataFrame:
    """批量抓取实时快照。不复权的当前价 + 成交额 + 换手率。

    ``timeout`` / ``retries`` 供交互式调用方收紧：Web 接口里等 45 秒
    （15×3）会让页面卡死，那种场景应该 4 秒 × 1 次，失败就走缓存兜底。
    """
    rows: list[dict] = []
    for i in range(0, len(tickers), batch):
        chunk = tickers[i : i + batch]
        text = _http_get(QT_URL + ",".join(chunk), encoding="gbk", timeout=timeout, retries=retries)
        rows.extend(parse_snapshot(text))
        time.sleep(0.08)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["amount_yuan"] = df["amount_wan"] * 1e4
    return df


# --------------------------------------------------------------------------- #
# K 线
# --------------------------------------------------------------------------- #
_KLINE_KEYS = ("qfqday", "day", "hfqday")


def _fetch_window_raw(ticker: str, start: str, end: str, adjust: str = "qfq") -> pd.DataFrame:
    """抓取单个时间窗口（<=640 根）的日线，不做任何补齐。"""
    param = f"{ticker},day,{start},{end},640,{adjust}"
    payload = None
    errors: list[str] = []
    for name, base in _host_order():
        try:
            # retries=2：入口已经轮换了，再对同一个入口死磕没有意义
            payload = json.loads(_http_get(base + param, retries=2))
            break
        except (RuntimeError, json.JSONDecodeError) as exc:
            errors.append(f"{name}: {str(exc)[:40]}")
            _mark_host_down(name)
    if payload is None:
        raise RuntimeError(f"K 线所有入口均失败（{'; '.join(errors)}）")
    node = (payload.get("data") or {}).get(ticker)
    if not isinstance(node, dict):
        return pd.DataFrame()
    bars = None
    for k in _KLINE_KEYS:
        if isinstance(node.get(k), list) and node[k]:
            bars = node[k]
            break
    if not bars:
        return pd.DataFrame()
    recs = []
    for b in bars:
        if len(b) < 6:
            continue
        recs.append(
            {
                "date": b[0],
                "open": _to_float(b[1]),
                "close": _to_float(b[2]),
                "high": _to_float(b[3]),
                "low": _to_float(b[4]),
                "volume": _to_float(b[5]),  # 手
            }
        )
    df = pd.DataFrame(recs)
    if df.empty:
        return df
    # 备用入口只认 count，会多返回 start 之前的 bar——统一裁掉，
    # 让两个入口的返回值在同一个窗口上完全一致。
    df["date"] = pd.to_datetime(df["date"])
    mask = (df["date"] >= pd.Timestamp(start)) & (df["date"] <= pd.Timestamp(end))
    return df.loc[mask].reset_index(drop=True)


# 收盘后前复权序列会滞后（2026-11-02 15:40 实测）：
#   · ifzq 的 fqkline 入口整段返回 HTTP 501（限流）
#   · proxy 的 newfqkline qfq 最后日期还停在上一交易日
#   · 同一入口的不复权 day **已经有当日 bar**，且两者在共同交易日只有复权系数之差
#     （三只实测 close 比值恰好 1.0、成交量逐位相同）
# 策略等不起几小时，所以当日 bar 用「不复权 × 系数」精确换算补上。
# 只在收盘后、且缺口不超过 TAIL_FILL_DAYS 天时启用：盘中 day 序列那根是未完成的，
# 长缺口则属于数据缺失（该报错），两者都不该走这条路径。
_TAIL_FILL_DAYS = 10


def fetch_kline_window(ticker: str, start: str, end: str, adjust: str = "qfq") -> pd.DataFrame:
    """抓取单个时间窗口（<=640 根）的日线；qfq 会补齐尚未发布的当日 bar。"""
    df = _fetch_window_raw(ticker, start, end, adjust)
    if adjust != "qfq":
        return df
    return _fill_latest_from_raw(ticker, df, start, end)


def _fill_latest_from_raw(
    ticker: str, qfq: pd.DataFrame, start: str, end: str
) -> pd.DataFrame:
    """用不复权日线补 qfq 序列末尾缺失的当日 bar（换算系数取自最后一个共同交易日）。

    系数 = qfq_close / raw_close（共同交易日）。即便当日发生分红，该系数依然成立：
    前复权只改历史价格、不改当日价格。没有共同交易日就无法定系数，原样返回，
    等下一次重试。
    """
    if qfq is None or qfq.empty or not is_market_closed():
        return qfq
    last = pd.Timestamp(qfq["date"].max())
    end_ts = pd.Timestamp(end)
    # 只处理"当下这一段"：历史窗口（研究脚本回看 2020 年）不补，
    # 否则每抓一个旧窗口都会多发一轮请求，且没有意义。
    if (pd.Timestamp.now().normalize() - end_ts).days > _TAIL_FILL_DAYS:
        return qfq
    if last >= end_ts or (end_ts - last).days > _TAIL_FILL_DAYS:
        return qfq
    try:
        raw = _fetch_window_raw(ticker, start, end, "day")
    except RuntimeError:
        return qfq                      # 不复权也拿不到：保持原样，由上层重试
    if raw is None or raw.empty:
        return qfq
    q_dates = pd.to_datetime(qfq["date"])
    r_dates = pd.to_datetime(raw["date"])
    common = sorted(set(q_dates) & set(r_dates))
    if not common:
        return qfq
    base = common[-1]
    q_close = float(qfq.loc[q_dates == base, "close"].iloc[0])
    r_close = float(raw.loc[r_dates == base, "close"].iloc[0])
    if not (q_close > 0 and r_close > 0):
        return qfq
    ratio = q_close / r_close
    if not (1e-6 < ratio < 1e6):
        return qfq
    tail = raw.loc[r_dates > last].copy()
    if tail.empty:
        return qfq
    for col in ("open", "close", "high", "low"):
        tail[col] = tail[col] * ratio
    # 成交量不做换算：复权只改价格，不改成交量（实测两序列逐位相同）
    return (
        pd.concat([qfq, tail], ignore_index=True)
        .drop_duplicates(subset="date", keep="last")
        .sort_values("date")
        .reset_index(drop=True)
    )


def _month_windows(start: str, end: str, step_years: int = 2) -> list[tuple[str, str]]:
    """把 [start, end] 切成不超过 640 个交易日（约 2.5 年）的窗口。"""
    s = pd.Timestamp(start)
    e = pd.Timestamp(end)
    out = []
    cur = s
    while cur <= e:
        nxt = min(cur + pd.DateOffset(years=step_years) - pd.Timedelta(days=1), e)
        out.append((cur.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d")))
        cur = nxt + pd.Timedelta(days=1)
    return out


def fetch_kline(ticker: str, start: str, end: str, adjust: str = "qfq") -> pd.DataFrame:
    """
    分段抓取并拼接完整日线序列。

    异常处理
    --------
    原实现用 ``except RuntimeError: continue`` 跳过失败的窗口，结果是序列中间
    缺一段（可能长达 2 年）却仍然"成功返回"，缺口处的收益率被当作 0，
    因子与回测全部失真，而且残缺结果会写入缓存被持续复用。

    现在：HTTP 失败单独记录，有任何窗口失败就抛错，由调用方决定是否跳过该标的
    （跳过时**不写缓存**）。返回空 DataFrame 仍然表示"该区间无数据"（未上市），
    这是正常情况，不算失败。
    """
    frames: list[pd.DataFrame] = []
    failed: list[str] = []
    for s, e in _month_windows(start, end):
        try:
            part = fetch_kline_window(ticker, s, e, adjust)
        except RuntimeError as exc:
            failed.append(f"{s}~{e}: {exc}")
            continue
        if not part.empty:
            frames.append(part)
        time.sleep(0.06)
    if failed:
        raise RuntimeError(f"{ticker} 有 {len(failed)} 个窗口抓取失败，拒绝使用残缺数据（{failed[0][:90]}）")
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset="date").sort_values("date").reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"])
    return df


# --------------------------------------------------------------------------- #
# 批量下载 + 本地缓存
# --------------------------------------------------------------------------- #
@dataclass
class CacheConfig:
    root: Path
    adjust: str = "qfq"

    @property
    def kline_dir(self) -> Path:
        d = self.root / f"kline_{self.adjust}"
        d.mkdir(parents=True, exist_ok=True)
        return d


def is_market_closed(now: pd.Timestamp | None = None) -> bool:
    """
    判断此刻是否**不可能存在未完成的当日 K 线**。

    语义是"今日日线是否已定型"，而不是"交易所是否在开市"：
    * 交易日 09:30~15:00（含午休）：盘中，当日 bar 未完成 → False；
    * 交易日 **00:00~09:30（早盘前）**：当日 bar 还没产生，昨日 bar 已完整 → True；
    * 15:00 之后、周六日：True。

    之前只把"15:00 之后"当收盘，于是交易日早上点「更新行情」时
    expected 被回退到前天，昨日那根**已完成的**日线永远不会被抓——
    表现就是"早盘点更新没反应，数据一直落后一天"。
    """
    now = pd.Timestamp.now() if now is None else now
    if now.weekday() >= 5:      # 周六/周日
        return True
    minutes = now.hour * 60 + now.minute
    return minutes < 9 * 60 + 30 or minutes >= 15 * 60


def drop_unfinished_today(df: pd.DataFrame, now: pd.Timestamp | None = None) -> pd.DataFrame:
    """
    剔除"今天且尚未收盘"的那根日线。

    为什么必须剔
    -----------
    日线与快照接口在盘中都会返回当天**未完成**的 K 线。一旦写进缓存：
      · close 是盘中价（10:00 与 14:50 能差 1%），不是收盘价；
      · volume 只有半天的量，量比、20 日均额全部偏小；
      · 更糟的是它只覆盖部分标的（谁先返回谁有），
        于是同一天里一部分是真数据、一部分是前一天的 ffill 值，
        横截面比较彻底失真。
    策略以收盘价为输入，所以盘中拉到的当日 bar 必须丢弃，等收盘后再取。
    """
    if df.empty:
        return df
    now = pd.Timestamp.now() if now is None else now
    if is_market_closed(now):
        return df
    today = now.normalize()
    return df[df["date"] < today].reset_index(drop=True)


def market_last_trade_date() -> pd.Timestamp | None:
    """用基准标的的实时快照推断全市场最新交易日。

    快照里的 quote_time 形如 ``20261023161459``，取前 8 位即为该笔行情的交易日。
    直接拿它当"应该有的最后一根日线"，比用 end 参数或"最近 N 天"的容差更准。
    """
    try:
        snap = fetch_snapshot(["sh510300"])
    except Exception:  # noqa: BLE001
        return None
    if snap.empty:
        return None
    raw = str(snap.iloc[0].get("quote_time", "")).strip()
    if len(raw) < 8 or not raw[:8].isdigit():
        return None
    try:
        return pd.Timestamp(f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}")
    except ValueError:
        return None


# 收盘后各 ETF 的当日 K 线并不是同时落库的。2026-10-30 15:1x 实测：260 只里
# 只有 165 只有当日 bar（63%），余下 95 只还停在昨天。若把这半天的数据写进缓存，
# `build_price_panel` 会对缺少当日 bar 的标的 **向前填充昨天收盘价**，于是：
#   · 当天的涨跌只由一部分标的贡献，横截面排名被扭曲；
#   · 波动率、量比、20 日均额同时失真（成交量少一半）；
#   · 全程没有任何报错，只是"数字看起来有点怪"。
# 所以低于这个覆盖率就整天丢弃，等下一次运行再补。
PARTIAL_DAY_MIN_COVERAGE = 0.80
# 样本太小时覆盖率没有统计意义（研究脚本只下三五只），不启用该判定。
PARTIAL_DAY_MIN_SAMPLE = 10


def latest_bar_coverage(frames: dict[str, pd.DataFrame]) -> dict:
    """最新那一天，在多大比例的标的里真的存在。"""
    newest_of: dict[str, pd.Timestamp] = {}
    for tk, df in frames.items():
        if df is None or len(df) == 0 or "date" not in df:
            continue
        d = pd.to_datetime(df["date"]).max()
        if pd.notna(d):
            newest_of[tk] = pd.Timestamp(d)
    if not newest_of:
        return {"date": None, "have": 0, "total": 0, "coverage": float("nan")}
    newest = max(newest_of.values())
    have = sum(1 for d in newest_of.values() if d == newest)
    return {"date": newest, "have": have, "total": len(newest_of),
            "coverage": have / len(newest_of)}


def strip_partial_day(
    frames: dict[str, pd.DataFrame],
    min_coverage: float = PARTIAL_DAY_MIN_COVERAGE,
    min_sample: int = PARTIAL_DAY_MIN_SAMPLE,
) -> tuple[dict[str, pd.DataFrame], dict]:
    """
    若最新一天只在少数标的里存在，就把这一天从**所有**标的里去掉。

    返回 (新 frames, info)。info 里带 `applied` 与 `dropped`（含该日的标的列表），
    调用方据此回写缓存——只从内存里删掉是不够的，磁盘上留着那天，下次运行
    会因为 `last >= expected` 直接判定"已是最新"，永远不再补。
    """
    info = latest_bar_coverage(frames)
    info["applied"] = False
    info["dropped"] = []
    if info["date"] is None or info["total"] < min_sample:
        return frames, info
    if info["coverage"] >= min_coverage:
        return frames, info

    newest = info["date"]
    out: dict[str, pd.DataFrame] = {}
    for tk, df in frames.items():
        if df is None or len(df) == 0 or "date" not in df:
            out[tk] = df
            continue
        d = pd.to_datetime(df["date"])
        if bool((d == newest).any()):
            out[tk] = df.loc[d != newest].reset_index(drop=True)
            info["dropped"].append(tk)
        else:
            out[tk] = df
    info["applied"] = True
    return out, info


def download_panel(
    tickers: list[str],
    start: str,
    end: str,
    cache: CacheConfig,
    workers: int = 6,
    refresh: bool = False,
    min_bars: int = 120,
    report: dict | None = None,
    max_gap_days: int = 45,
) -> dict[str, pd.DataFrame]:
    """并发下载一组标的的日线，写入缓存目录，返回 {ticker: df}。

    缓存命中规则
    ------------
    原实现用 ``date.max() >= end - 10 天`` 判定"够新"，这会让落后 1~10 个交易日的
    标的被当成最新而永不更新。对每日都要出下单清单的工具是致命的：昨日收盘价会被
    当作今日价使用，且没有任何提示。

    现在改为：先取全市场最新交易日（来自实时快照），缓存必须覆盖到该日才算命中；
    否则只补抓缺失的尾部（从最后缓存日往前 10 天起抓），不重新下载整段历史。
    """
    result: dict[str, pd.DataFrame] = {}
    failed: dict[str, str] = {}
    gaps: dict[str, int] = {}

    # 快照取不到时不能用 end 兜底：周末/节假日 end 会落在非交易日，
    # 缓存永远比它旧，于是每次运行都会全量重下 260 只。
    live = market_last_trade_date()
    if live is None:
        candidates = sorted(cache.kline_dir.glob("*.csv"))
        if candidates:
            try:
                last_cached = max(
                    pd.read_csv(fp, usecols=["date"])["date"].max()
                    for fp in candidates[:20]
                )
                live = pd.Timestamp(last_cached)
            except Exception:  # noqa: BLE001
                live = None
    expected = live if live is not None else pd.Timestamp(end).normalize()
    expected = min(expected, pd.Timestamp(end).normalize())
    # 盘中时"最新交易日"是今天，但今天的 K 线还没完成、也不会被写入，
    # 若拿它当应到日期，每次运行都会判定缓存不新鲜 → 反复全量重下。
    # 因此盘中把目标回退一个档，只要求覆盖到上一交易日。
    if not is_market_closed():
        expected = expected - pd.Timedelta(days=1)
        while expected.weekday() >= 5:
            expected = expected - pd.Timedelta(days=1)

    def _gap(tk: str, df: pd.DataFrame) -> None:
        """检查序列内部是否有异常长的时间缺口。"""
        if len(df) < 2:
            return
        d = df["date"].sort_values().diff().dt.days.dropna()
        if d.empty:
            return
        worst = int(d.max())
        if worst > max_gap_days:
            gaps[tk] = worst

    def _one(tk: str) -> tuple[str, pd.DataFrame | None]:
        fp = cache.kline_dir / f"{tk}.csv"
        try:
            if fp.exists() and not refresh:
                df = pd.read_csv(fp, parse_dates=["date"])
                if not df.empty:
                    last = pd.Timestamp(df["date"].max())
                    if last >= expected:
                        _gap(tk, df)
                        return tk, df
                    # 增量补尾：只抓最后 10 天到最新，避免重复下载 8 年
                    tail_start = (last - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
                    tail = fetch_kline(tk, tail_start, end, cache.adjust)
                    tail = drop_unfinished_today(tail)
                    if not tail.empty:
                        df = (
                            pd.concat([df, tail], ignore_index=True)
                            .drop_duplicates(subset="date", keep="last")
                            .sort_values("date")
                            .reset_index(drop=True)
                        )
                        atomic_to_csv(df, fp, index=False)
                    _gap(tk, df)
                    return tk, df
            df = fetch_kline(tk, start, end, cache.adjust)
            df = drop_unfinished_today(df)
            if not df.empty:
                atomic_to_csv(df, fp, index=False)
            _gap(tk, df)
            return tk, df
        except Exception as exc:  # noqa: BLE001
            # 单只失败不能中断整批（原实现用 pool.map，一只抛错会丢掉其余标的），
            # 也不写缓存——宁可下次重抓，也不留下残缺数据。
            failed[tk] = f"{type(exc).__name__}: {exc}"[:160]
            return tk, None

    fetched: dict[str, pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, tk) for tk in tickers]
        for fut in as_completed(futures):
            tk, df = fut.result()
            if df is not None and not df.empty:
                fetched[tk] = df

    # 收盘后当日 K 线分批落库：只写进去一部分会让面板对其余标的 ffill，
    # 横截面收益被扭曲且不报错。这里整天丢弃，并把磁盘上那一天也裁掉。
    fetched, partial = strip_partial_day(fetched)
    if partial["applied"]:
        for tk in partial["dropped"]:
            try:
                atomic_to_csv(fetched[tk], cache.kline_dir / f"{tk}.csv", index=False)
            except Exception as exc:  # noqa: BLE001
                failed[tk] = f"回写裁剪失败 {type(exc).__name__}: {exc}"[:160]

    for tk, df in fetched.items():
        if len(df) >= min_bars:
            result[tk] = df

    if report is not None:
        report["failed"] = failed
        report["gaps"] = gaps
        report["expected_date"] = expected.strftime("%Y-%m-%d")
        report["success"] = len(result)
        report["requested"] = len(tickers)
        report["partial_day"] = {
            "date": partial["date"].strftime("%Y-%m-%d") if partial["date"] is not None else None,
            "have": partial["have"],
            "total": partial["total"],
            "coverage": partial["coverage"],
            "dropped": len(partial["dropped"]),
            "applied": bool(partial["applied"]),
            "min_coverage": PARTIAL_DAY_MIN_COVERAGE,
        }
    return result


def build_price_panel(frames: dict[str, pd.DataFrame], field: str = "close") -> pd.DataFrame:
    """把 {ticker: df} 合并成 index=date, columns=ticker 的宽表。"""
    series = {}
    for tk, df in frames.items():
        if df.empty:
            continue
        s = df.set_index("date")[field]
        s = s[~s.index.duplicated(keep="last")]
        series[tk] = s
    panel = pd.DataFrame(series).sort_index()
    return panel
