"""
数据装配管线：把缓存的日线 CSV 组装成回测所需的宽表面板。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .datafeed import CacheConfig
from .strategy import Panel


def load_panel(
    pool_fp: str | Path,
    cache_root: str | Path,
    start: str,
    end: str,
    adjust: str = "qfq",
) -> Panel:
    """从缓存目录读取日线，构建 Panel。"""
    pool = pd.read_csv(pool_fp, dtype={"ticker": str})
    cache = CacheConfig(root=Path(cache_root), adjust=adjust)

    fields = ("close", "open", "high", "low", "volume")
    series: dict[str, dict[str, pd.Series]] = {f: {} for f in fields}

    for tk in pool["ticker"]:
        fp = cache.kline_dir / f"{tk}.csv"
        if not fp.exists():
            continue
        df = pd.read_csv(fp, parse_dates=["date"])
        if df.empty:
            continue
        df = df[(df["date"] >= pd.Timestamp(start)) & (df["date"] <= pd.Timestamp(end))]
        if df.empty:
            continue
        df = df.drop_duplicates(subset="date").set_index("date").sort_index()
        for f in fields:
            if f in df.columns:
                series[f][tk] = df[f]

    closes = {}
    opens = {}
    highs = {}
    lows = {}
    vols = {}
    for f, store in zip(fields, (closes, opens, highs, lows, vols)):
        store.update(series[f])

    if not closes:
        raise RuntimeError("缓存为空，请先运行 scripts/download_history.py")

    close = pd.DataFrame(closes).sort_index()
    open_ = pd.DataFrame(opens).reindex(columns=close.columns).sort_index()
    high = pd.DataFrame(highs).reindex(columns=close.columns).sort_index()
    low = pd.DataFrame(lows).reindex(columns=close.columns).sort_index()
    volume = pd.DataFrame(vols).reindex(columns=close.columns).sort_index()

    # 在 ffill 之前记录每只标的的真实最后一个交易日
    last_date = close.apply(lambda s: s.last_valid_index())

    # 交易日期取所有标的的并集，价格缺口前向填充（停牌/未上市）
    close = close.ffill()
    open_ = open_.ffill().fillna(close)
    high = high.ffill().fillna(close)
    low = low.ffill().fillna(close)
    volume = volume.fillna(0.0)

    meta = pool.set_index("ticker")[["name", "category"]].reindex(close.columns)
    return Panel(
        close=close, open_=open_, high=high, low=low, volume=volume, meta=meta,
        last_date=last_date,
    )


def latest_data_date(cache_root: str | Path) -> str:
    """缓存里日线的**众数**最新日期，返回 YYYY-MM-DD。

    实验脚本（grid2 / ablation）原来把 END 写成 "2026-10-23"。数据更新到 11-02 之后，
    这两张表仍停在 10-23：不仅少了两周的行情，更严重的是它们比引用了新回测器的
    perf_main.csv 更早生成——同一页上「完整模型 17.3%」对着 KPI 卡片的 12.4%，
    读者会以为是两个策略。日期必须从数据反推。

    取众数而不是最大值：单只标的停牌或补数据滞后会把 max 带偏，而绝大多数标的
    的最后一根日线才是面板真正的截止日。
    """
    root = Path(cache_root)
    dirs = [root / "kline_qfq", root / "kline"] if (root / "kline_qfq").exists() else [root]
    vals: list[str] = []
    for d in dirs:
        for fp in d.glob("*.csv"):
            try:
                # 只读最后一行的 date 列，避免把 260 只的完整日线读进内存
                tail = pd.read_csv(fp, usecols=["date"]).tail(1)
            except (OSError, ValueError, pd.errors.EmptyDataError):
                continue
            if len(tail):
                vals.append(str(tail["date"].iloc[0])[:10])
    if not vals:
        raise RuntimeError(f"{cache_root} 下没有可用的日线缓存，请先运行 scripts/download_history.py")
    return max(set(vals), key=vals.count)


def benchmark_series(close: pd.DataFrame, ticker: str = "sh510300") -> pd.Series:
    """基准净值（归一到 1）。缺省时退回宽基等权。"""
    if ticker in close.columns:
        s = close[ticker].dropna()
        return s / s.iloc[0]
    return (close.mean(axis=1) / close.mean(axis=1).iloc[0]).dropna()
