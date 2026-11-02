"""前复权重锚检测的单元测试。

背景：分红/拆分后，供应商会把**全部历史**按新复权因子重算（重锚）。
增量更新只补最后 10 天并 keep="last"，老数据仍停在旧锚上，接缝处于是出现
一个等于分红幅度的假跳空，而且永远不会自愈——它会直接进入动量、波动率与
回测收益，全程不报错。所以每次补尾都要比对重叠区价格比。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etfquant import datafeed as DF


def _frame(dates, closes, opens=None):
    opens = opens or closes
    return pd.DataFrame({
        "date": pd.to_datetime(dates),
        "open": opens,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": closes,
        "volume": [1000] * len(closes),
    })


# --------------------------------------------------------------------------- #
# 纯函数：比价
# --------------------------------------------------------------------------- #
def test_ratio_is_one_for_same_scale():
    d = pd.bdate_range("2026-10-01", periods=6)
    a = _frame(d, [1.0, 1.1, 1.2, 1.1, 1.3, 1.4])
    b = a.copy()
    assert DF.qfq_reanchor_ratio(a, b) == pytest.approx(1.0)


def test_ratio_detects_rescale():
    d = pd.bdate_range("2026-10-01", periods=6)
    old = _frame(d, [1.0, 1.1, 1.2, 1.1, 1.3, 1.4])
    new = _frame(d, [0.98, 1.078, 1.176, 1.078, 1.274, 1.372])   # 分红 2%
    assert DF.qfq_reanchor_ratio(old, new) == pytest.approx(0.98, abs=1e-9)


def test_ratio_uses_median_so_one_bad_point_does_not_trigger():
    d = pd.bdate_range("2026-10-01", periods=6)
    old = _frame(d, [1.0, 1.1, 1.2, 1.1, 1.3, 1.4])
    new = old.copy()
    new.loc[2, "close"] = 0.5          # 单个错价：中位数仍是 1
    assert DF.qfq_reanchor_ratio(old, new) == pytest.approx(1.0)


def test_ratio_needs_enough_overlap():
    d = pd.bdate_range("2026-10-01", periods=3)
    a = _frame(d, [1.0, 1.1, 1.2])
    assert DF.qfq_reanchor_ratio(a, a.copy()) is None


def test_ratio_ignores_non_positive_prices():
    d = pd.bdate_range("2026-10-01", periods=6)
    a = _frame(d, [1.0, 1.1, 1.2, 1.1, 1.3, 1.4])
    b = a.copy()
    b["close"] = 0.0
    assert DF.qfq_reanchor_ratio(a, b) is None


# --------------------------------------------------------------------------- #
# 集成：检测到重锚必须整段重下
# --------------------------------------------------------------------------- #
@pytest.fixture()
def cache_dir(tmp_path):
    return DF.CacheConfig(root=tmp_path)


def test_download_panel_refetches_whole_history_on_reanchor(tmp_path, monkeypatch):
    cache = DF.CacheConfig(root=tmp_path)
    kdir = cache.kline_dir
    old_dates = pd.bdate_range("2026-04-01", periods=150)
    old = _frame(old_dates, [1.0 + i * 0.001 for i in range(150)])
    old.to_csv(kdir / "sh510300.csv", index=False)
    last = old_dates[-1]

    today = pd.Timestamp("2026-11-02")
    monkeypatch.setattr(DF, "market_last_trade_date", lambda: today)
    monkeypatch.setattr(DF, "is_market_closed", lambda now=None: True)

    calls: list[tuple[str, str]] = []

    base = [1.0 + i * 0.001 for i in range(150)]

    def fake_fetch(ticker, start, end, adjust="qfq"):
        calls.append((start, end))
        if start >= (last - pd.Timedelta(days=10)).strftime("%Y-%m-%d"):
            # 尾部：重叠日按新锚（0.98）给出，再加当日
            dates = list(old_dates[-6:]) + [today]
            return _frame(dates, [0.98 * c for c in base[-6:]] + [0.98 * 1.5])
        # 整段：全部历史都按新锚
        return _frame(list(old_dates) + [today],
                      [0.98 * c for c in base] + [0.98 * 1.5])

    monkeypatch.setattr(DF, "fetch_kline", fake_fetch)

    report: dict = {}
    out = DF.download_panel(["sh510300"], "2026-04-01", "2026-11-02", cache,
                            workers=1, min_bars=100, report=report)

    assert "sh510300" in out
    assert report["reanchored"]["sh510300"] == pytest.approx(0.98, abs=1e-6)
    # 必须发生"整段重下"：至少有一次调用的 start 不是补尾起点
    assert any(s == "2026-04-01" for s, _ in calls)
    cached = pd.read_csv(kdir / "sh510300.csv", parse_dates=["date"])
    assert cached["close"].iloc[0] == pytest.approx(0.98, abs=1e-9)   # 旧锚被整体替换


def test_download_panel_keeps_tail_merge_when_scale_matches(tmp_path, monkeypatch):
    cache = DF.CacheConfig(root=tmp_path)
    kdir = cache.kline_dir
    old_dates = pd.bdate_range("2026-04-01", periods=150)
    old = _frame(old_dates, [1.0 + i * 0.001 for i in range(150)])
    old.to_csv(kdir / "sh510300.csv", index=False)
    last = old_dates[-1]

    today = pd.Timestamp("2026-11-02")
    monkeypatch.setattr(DF, "market_last_trade_date", lambda: today)
    monkeypatch.setattr(DF, "is_market_closed", lambda now=None: True)

    calls: list[str] = []

    base = [1.0 + i * 0.001 for i in range(150)]

    def fake_fetch(ticker, start, end, adjust="qfq"):
        calls.append(start)
        dates = list(old_dates[-6:]) + [today]
        return _frame(dates, base[-6:] + [1.5])

    monkeypatch.setattr(DF, "fetch_kline", fake_fetch)
    report: dict = {}
    DF.download_panel(["sh510300"], "2026-04-01", "2026-11-02", cache,
                      workers=1, min_bars=100, report=report)

    assert "reanchored" not in report
    assert len(calls) == 1                     # 只补尾，没有整段重下
    assert calls[0] != "2026-04-01"
