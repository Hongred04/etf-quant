"""第二排序键（52 周高点距离）与"只能微调"这条性质的测试。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from etfquant import secondary as S


def _series(vals, start="2023-01-02"):
    idx = pd.bdate_range(start, periods=len(vals))
    return pd.DataFrame({"a": vals}, index=idx, dtype=float)


# --------------------------------------------------------------------------- #
# 52 周高点距离
# --------------------------------------------------------------------------- #
def test_gap_is_zero_at_new_high():
    px = _series(np.linspace(1.0, 2.0, 260))
    got = S.high_52w_gap(px, window=250, min_periods=200)["a"].dropna()
    assert got.iloc[-1] == pytest.approx(0.0, abs=1e-12)


def test_gap_matches_drawdown_from_peak():
    """高点 100，现价 80 → −20%（不做 52 周低点缩放，所以是精确值）。"""
    vals = np.concatenate([np.linspace(80, 100, 230), np.linspace(99, 80, 30)])
    got = S.high_52w_gap(_series(vals), window=250, min_periods=200)["a"].dropna()
    assert got.iloc[-1] == pytest.approx(-0.20, abs=1e-9)


def test_gap_requires_enough_history():
    px = _series(np.linspace(1.0, 1.2, 260))
    got = S.high_52w_gap(px, window=250, min_periods=200)["a"]
    assert got.iloc[:199].isna().all()
    assert got.iloc[199:].notna().all()


def test_gap_has_no_lookahead():
    """截断未来不能改变历史值——这是所有因子必须过的因果性检查。"""
    rng = np.random.default_rng(7)
    px = _series(100 * np.cumprod(1 + rng.normal(0, 0.01, 300)))
    full = S.high_52w_gap(px, window=250, min_periods=200)["a"]
    cut = S.high_52w_gap(px.iloc[:220], window=250, min_periods=200)["a"]
    pd.testing.assert_series_equal(full.iloc[:220], cut, check_names=False)


# --------------------------------------------------------------------------- #
# 标准化与索引
# --------------------------------------------------------------------------- #
def test_zscore_is_cross_sectionally_standardized():
    idx = pd.bdate_range("2024-01-01", periods=5)
    z = S.zscore(pd.DataFrame(np.arange(15.0).reshape(5, 3), index=idx))
    assert z.mean(axis=1).abs().max() < 1e-9
    assert (z.std(axis=1) - 1).abs().max() < 1e-6


def test_score_frame_indexes_to_requested_dates_only():
    # 必须多列：横截面 z 值在单列上标准差为 0，会全部变成 NaN
    idx = pd.bdate_range("2023-01-02", periods=300)
    px = pd.DataFrame({"a": np.linspace(1.0, 2.0, 300),
                       "b": np.linspace(2.0, 1.0, 300),
                       "c": np.linspace(1.5, 1.6, 300)}, index=idx)
    days = idx[[210, 250, 290]]
    got = S.score_frame(px, days, window=250, min_periods=200)
    assert list(got.index) == list(days)
    assert got.notna().all().all()


# --------------------------------------------------------------------------- #
# 混合：只能翻动近平局
# --------------------------------------------------------------------------- #
def test_blend_flips_near_tie():
    score = pd.Series({"a": 1.00, "b": 0.95})
    sec = pd.Series({"a": -2.0, "b": 2.0})
    out = S.blend_secondary(score, sec, 0.05)
    assert out["b"] > out["a"], "z 差 0.05 应该被 ±0.1 的贡献翻过来"


def test_blend_cannot_override_a_real_gap():
    """z 差 0.5 是真实强弱，不能被次要因子翻掉——这是它区别于"混合打分"的关键。"""
    score = pd.Series({"a": 1.50, "b": 1.00})
    sec = pd.Series({"a": -2.0, "b": 2.0})
    out = S.blend_secondary(score, sec, 0.05)
    assert out["a"] > out["b"]


def test_blend_treats_missing_as_neutral():
    score = pd.Series({"a": 1.0, "b": 0.9})
    sec = pd.Series({"a": 2.0})                     # b 没有值
    out = S.blend_secondary(score, sec, 0.05)
    assert out["b"] == pytest.approx(0.9)            # 中性，不剔除也不报错
    assert out["a"] == pytest.approx(1.1)


def test_blend_is_noop_without_weight():
    score = pd.Series({"a": 1.0, "b": 0.9})
    out = S.blend_secondary(score, pd.Series({"a": 9.0}), 0.0)
    assert out.equals(score)


# --------------------------------------------------------------------------- #
# 行业扩散度（只作对照，未接入）
# --------------------------------------------------------------------------- #
def test_industry_breadth_broadcasts_within_group():
    idx = pd.bdate_range("2024-01-01", periods=30)
    up, down = np.linspace(1.0, 1.5, 30), np.linspace(1.0, 0.8, 30)
    close = pd.DataFrame({"a": up, "b": up, "c": down, "d": down}, index=idx)
    got = S.industry_breadth(close, {"a": "T", "b": "T", "c": "T", "d": "T"},
                             window=20, min_members=3)
    assert got["a"].iloc[-1] == got["b"].iloc[-1] == pytest.approx(0.5)
    assert got["c"].iloc[-1] == pytest.approx(0.5)


def test_industry_breadth_skips_small_groups():
    idx = pd.bdate_range("2024-01-01", periods=30)
    close = pd.DataFrame({"a": np.linspace(1, 1.5, 30), "b": np.linspace(1, 1.4, 30)}, index=idx)
    got = S.industry_breadth(close, {"a": "T", "b": "T"}, window=20, min_members=4)
    assert got.isna().all().all()
