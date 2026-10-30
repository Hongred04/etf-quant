"""随机对照：抽签口径、持有期实现、年化与分位（全部离线）。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from etfquant import placebo as P


def test_pick_random_respects_pool_and_top():
    rng = np.random.default_rng(0)
    pool = [f"t{i}" for i in range(20)]
    for _ in range(20):
        got = P.pick_random(pool, 3, rng)
        assert len(got) == 3
        assert len(set(got)) == 3, "不允许重复抽同一只"
        assert set(got) <= set(pool)


def test_pick_random_returns_all_when_pool_is_small():
    rng = np.random.default_rng(0)
    assert P.pick_random(["a", "b"], 3, rng) == ["a", "b"]


def test_random_leg_holds_until_next_rebalance():
    """两期之间不换仓：同一期内每天的组合成员必须相同。"""
    idx = pd.bdate_range("2024-01-01", periods=6)
    # 每日收益率恒定：任何固定的两只组合，其日均收益都是常数 →
    # 若持有期内成员不变，则每天的收益必然相同。用 pct_change 会在首行产生 0，
    # 破坏"恒定"这个前提，所以直接构造收益率。
    ret = pd.DataFrame({"a": 0.01, "b": 0.02, "c": 0.0, "d": 0.0}, index=idx)
    dates = pd.DatetimeIndex([idx[0], idx[3]])
    got = P.random_leg_returns(ret, {d: ["a", "b", "c", "d"] for d in dates},
                               top=2, n_draws=3, seed=1)
    seg = got.iloc[0:3, 0]          # 第一期 3 天
    assert seg.nunique() == 1, "同一期内每天的组合收益应相同（成员没换）"


def test_random_leg_is_deterministic_with_seed():
    idx = pd.bdate_range("2024-01-01", periods=40)
    rng = np.random.default_rng(2)
    ret = pd.DataFrame(rng.normal(0, 0.01, (40, 10)),
                       index=idx, columns=[f"t{i}" for i in range(10)])
    dates = pd.DatetimeIndex([idx[0], idx[20]])
    a = P.random_leg_returns(ret, {d: list(ret.columns) for d in dates}, top=3, n_draws=5, seed=7)
    b = P.random_leg_returns(ret, {d: list(ret.columns) for d in dates}, top=3, n_draws=5, seed=7)
    pd.testing.assert_frame_equal(a, b)


def test_cagr_matches_constant_growth():
    """日收益 r 复利 252 次 = (1+r)^252 − 1，不是 r×252（简单年化）。"""
    r = 0.10 / 252
    daily = pd.Series(r, index=pd.bdate_range("2020-01-01", periods=252))
    assert P.cagr_of(daily) == pytest.approx((1 + r) ** 252 - 1, abs=1e-6)


def test_percentile_of_is_rank_based():
    sample = np.arange(100.0)
    assert P.percentile_of(50.0, sample) == pytest.approx(50.0)
    assert P.percentile_of(200.0, sample) == pytest.approx(100.0)


def test_percentile_handles_empty_sample():
    assert np.isnan(P.percentile_of(1.0, np.array([])))
