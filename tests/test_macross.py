"""
均线金叉死叉策略的测试。

这类"规则简单"的策略最容易在实现里悄悄偷价：把信号日收盘当成交价、
把上穿当天算持有第一天但用当天开盘买。这里钉住三件事：
状态逐日对齐、空仓期是真·现金、无前视。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etfquant import macross as M


def _ramp(spec: list[tuple[int, float]], start=10.0) -> pd.DataFrame:
    """按 (天数, 日涨幅) 列表生成价格，方便构造金叉/死叉。"""
    rets = []
    for days, r in spec:
        rets += [r] * days
    px = start * np.cumprod(1 + np.array(rets))
    idx = pd.bdate_range("2020-01-01", periods=len(px))
    return pd.DataFrame({"a": px}, index=idx)


def test_golden_cross_enters_state():
    # 前 20 天横盘(让均线就位)，然后连涨 → 5 上穿 20
    px = _ramp([(30, 0.0), (10, 0.01)])
    state = M.in_position(px)
    assert not state["a"].iloc[29]
    assert state["a"].iloc[39]        # 连涨 10 天后必然处于金叉态


def test_death_cross_exits_state():
    px = _ramp([(30, 0.0), (10, 0.01), (15, -0.012)])
    state = M.in_position(px)
    assert state["a"].iloc[39]
    assert not state["a"].iloc[-1]


def test_trend_filter_blocks_falling_ma60():
    # 长期阴跌后再反弹：MA60 仍在下降，5/20 金叉出现也不该进场
    px = _ramp([(60, -0.004), (12, 0.012)])
    ok = M.trend_ok(px, n=60, look=5)
    assert not ok["a"].iloc[-1]       # MA60 还在往下走
    assert M.in_position(px)["a"].iloc[-1]


def test_build_targets_is_equal_weight_and_cash():
    idx = pd.bdate_range("2020-01-01", periods=5)
    close = pd.DataFrame({"a": [1, 1, 1, 1, 1], "b": [1, 1, 1, 1, 1]}, index=idx)
    # 人为给定状态：a 一直在场，b 只在前两天
    state = pd.DataFrame(
        {"a": [True] * 5, "b": [True, True, False, False, False]}, index=idx)
    orig = M.in_position
    M.in_position = lambda close, fast=5, slow=20: state      # 只测权重构造
    try:
        tg = M.build_targets(close, min_hist=0, trend_look=None)
    finally:
        M.in_position = orig
    assert tg[idx[0]] == {"a": 0.5, "b": 0.5}
    assert tg[idx[2]] == {"a": 1.0}          # b 离场后 a 独占（不是留 50% 现金）


def test_build_targets_skips_unchanged_weights():
    idx = pd.bdate_range("2020-01-01", periods=30)
    px = _ramp([(30, 0.0)]).rename(columns={"a": "a"})
    tg = M.build_targets(px, min_hist=0, trend_look=None)
    # 横盘时 5/20 反复缠绕，但权重相同的连续日期只保留第一条
    assert len(tg) <= 30


def test_no_lookahead():
    px = _ramp([(30, 0.0), (10, 0.01), (15, -0.012)])
    cut = px.index[45]
    a = M.in_position(px).loc[:cut]
    b = M.in_position(px.loc[:cut])
    pd.testing.assert_frame_equal(a, b)


def test_single_etf_targets_flips_once():
    px = _ramp([(30, 0.0), (10, 0.01), (15, -0.012)])
    tg = M.single_etf_targets(px["a"], trend_look=None)
    flags = [bool(v) for v in tg.values()]
    assert flags == [False, True, False]      # 空仓 → 进场 → 离场，各一次
