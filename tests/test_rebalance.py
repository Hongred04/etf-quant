"""
调仓日历的单元测试。

这一组测试守的是「日更信号 ≠ 5 日调仓」这个曾经真实发生过的 bug：
`run_signal.py` 原先取数据最后一个交易日重算目标，连续三天运行给出三份不同持仓
（2026-10-26 地产 / 10-27 红利低波 / 10-28 纳指），照做等于每天换仓。
"""
from __future__ import annotations

import pandas as pd
import pytest

from etfquant import rebalance as R


def _idx(n: int = 40, start: str = "2026-01-05") -> pd.DatetimeIndex:
    return pd.bdate_range(start, periods=n)


# --------------------------------------------------------------------------- #
# 找最近调仓日
# --------------------------------------------------------------------------- #
def test_last_rebalance_takes_latest_not_after():
    idx = _idx(30)
    reb = list(idx[::5])
    # 落在两次调仓之间的日子，必须退回到前一个调仓日
    assert R.last_rebalance(reb, idx[7]) == idx[5]
    # 正好是调仓日，取当天
    assert R.last_rebalance(reb, idx[10]) == idx[10]


def test_last_rebalance_returns_none_before_first():
    idx = _idx(20)
    reb = list(idx[10::5])
    assert R.last_rebalance(reb, idx[0]) is None
    assert R.last_rebalance([], idx[0]) is None


# --------------------------------------------------------------------------- #
# 下一个调仓日
# --------------------------------------------------------------------------- #
def test_next_rebalance_exact_when_data_covers():
    idx = _idx(40)
    nxt, est = R.next_rebalance(idx, idx[10], 5)
    assert nxt == idx[15]
    assert est is False


def test_next_rebalance_estimated_beyond_last_bar():
    idx = _idx(20)
    nxt, est = R.next_rebalance(idx, idx[18], 5)
    assert est is True, "数据不够时必须标成估算，界面要如实说明"
    assert nxt > idx[-1]


def test_next_rebalance_skips_weekend():
    # 2026-10-30 是周五，往后 1 个工作日应跳到 11-02 周一
    idx = pd.bdate_range("2026-10-26", periods=5)      # 10-26 ~ 10-30
    nxt, est = R.next_rebalance(idx, idx[-1], 1)
    assert est is True
    assert nxt == pd.Timestamp("2026-11-02")
    assert nxt.weekday() == 0


# --------------------------------------------------------------------------- #
# 执行日与交易日计数
# --------------------------------------------------------------------------- #
def test_next_trading_day_uses_index():
    idx = _idx(10)
    d, est = R.next_trading_day(idx, idx[3])
    assert d == idx[4] and est is False


def test_next_trading_day_beyond_data():
    idx = _idx(10)
    d, est = R.next_trading_day(idx, idx[-1])
    assert est is True and d > idx[-1]


def test_trading_days_between_inside_and_outside():
    idx = _idx(20)
    # 10-05(周一) 之后到 10-09 共 4 个交易日
    assert R.trading_days_between(idx, idx[0], idx[4]) == 4
    assert R.trading_days_between(idx, idx[4], idx[0]) == 0      # 反序返回 0
    assert R.trading_days_between(idx, idx[-1], idx[-1]) == 0
    # 超出数据范围时按工作日补
    assert R.trading_days_between(idx, idx[-1], pd.Timestamp("2026-02-06")) > 0


# --------------------------------------------------------------------------- #
# 整体日程
# --------------------------------------------------------------------------- #
def test_schedule_is_consistent():
    idx = _idx(40)
    s = R.schedule(idx, idx[10], 5)
    assert s["rebalance_date"] == idx[10]
    assert s["exec_date"] == idx[11]
    assert s["next_rebalance"] == idx[15]
    assert s["next_estimated"] is False


def test_same_window_yields_same_rebalance_day():
    """同一调仓窗口内的任意两天，都必须指向同一个调仓日。

    这是"每天跑出三份清单"的直接防线：只要这条成立，中间几天的输出就不会变。
    """
    idx = _idx(40)
    reb = list(idx[::5])
    got = {R.last_rebalance(reb, idx[i]) for i in (10, 11, 12, 13, 14)}
    assert got == {idx[10]}


def test_freq_one_means_every_day():
    idx = _idx(10)
    reb = list(idx[::1])
    assert R.last_rebalance(reb, idx[4]) == idx[4]
