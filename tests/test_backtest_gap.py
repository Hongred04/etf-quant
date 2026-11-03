"""调仓日隔夜跳空的记账回归测试。

这是 2026-11-03 发现并修复的真实缺陷：旧实现只把旧权重按跳空归一化，
却没有给净值乘上 (1+gap)，等于把"持股过夜"的盈亏整段丢掉。
合成用例把正确值钉死：持有 A 过夜，A 开盘从 1.00 跳到 1.10 后卖出，净值必须是 1.10。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etfquant.backtest import Backtester


def _panel(a_close, a_open, b_close=None):
    dates = pd.bdate_range("2026-01-05", periods=len(a_close))
    b_close = b_close or [1.0] * len(a_close)
    close = pd.DataFrame({"A": a_close, "B": b_close}, index=dates)
    open_ = pd.DataFrame({"A": a_open, "B": b_close}, index=dates)
    return dates, close, open_


def test_overnight_gap_on_rebalance_day_is_booked():
    dates, close, open_ = _panel(
        a_close=[1.0, 1.0, 1.10, 1.10],
        a_open=[1.0, 1.0, 1.10, 1.10],
    )
    # 01-05 收盘定 A（01-06 开盘买），01-06 收盘定 B（01-07 开盘换）
    tgt = {dates[0]: {"A": 1.0}, dates[1]: {"B": 1.0}}
    res = Backtester(close, open_, fee=0, slippage=0).run(tgt, start=str(dates[0].date()))
    assert res.nav.iloc[-1] == 1.10


def test_gap_is_recorded_separately():
    dates, close, open_ = _panel(
        a_close=[1.0, 1.0, 1.10, 1.10],
        a_open=[1.0, 1.0, 1.10, 1.10],
    )
    tgt = {dates[0]: {"A": 1.0}, dates[1]: {"B": 1.0}}
    res = Backtester(close, open_, fee=0, slippage=0).run(tgt, start=str(dates[0].date()))
    gaps = {d["date"]: d["gap"] for d in res.daily}
    assert abs(gaps[dates[2]] - 0.10) < 1e-12


def test_no_gap_no_extra_return():
    dates, close, open_ = _panel(
        a_close=[1.0, 1.0, 1.0, 1.0],
        a_open=[1.0, 1.0, 1.0, 1.0],
    )
    tgt = {dates[0]: {"A": 1.0}, dates[1]: {"B": 1.0}}
    res = Backtester(close, open_, fee=0, slippage=0).run(tgt, start=str(dates[0].date()))
    assert res.nav.iloc[-1] == 1.0
