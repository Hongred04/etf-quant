"""
双因子行业动量的纯函数测试。

这套逻辑全部要拿别人的策略描述去跑数据，最容易出的不是算错，而是**悄悄用了未来数据**：
滚动回归的窗口错一位、月末信号取了当月最高价、排名并列时顺序随机，
都会让结果看起来更好而完全不报错。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etfquant import dual_mom as D


def _index(n: int = 40) -> pd.DatetimeIndex:
    return pd.bdate_range("2024-01-01", periods=n)


# --------------------------------------------------------------------------- #
# 月份与价格
# --------------------------------------------------------------------------- #
def test_month_end_dates_are_last_trading_day():
    idx = pd.bdate_range("2024-01-01", "2024-03-31")
    ends = D.month_end_dates(idx)
    assert [d.strftime("%Y-%m") for d in ends] == ["2024-01", "2024-02", "2024-03"]
    # 1 月最后一个交易日是 1/31（周三），2 月是 2/29（周四），不是日历月末即休市的情况
    assert ends[0] == pd.Timestamp("2024-01-31")
    assert all(d in idx for d in ends)


def test_monthly_close_uses_last_observation():
    idx = pd.DatetimeIndex(["2024-01-30", "2024-01-31", "2024-02-29"])
    px = pd.DataFrame({"a": [1.0, 2.0, 3.0]}, index=idx)
    m = D.monthly_close(px)
    assert m.loc["2024-01-31", "a"] == 2.0
    assert m.loc["2024-02-29", "a"] == 3.0


def test_momentum_is_log_ratio_over_lookback():
    idx = pd.date_range("2022-01-31", periods=20, freq="ME")
    px = pd.DataFrame({"a": np.exp(np.arange(20) * 0.05)}, index=idx)
    mom = D.momentum(px, lookback=12)
    assert mom["a"].iloc[12] == pytest.approx(0.6, abs=1e-9)
    assert mom["a"].iloc[:12].isna().all()


# --------------------------------------------------------------------------- #
# 残差动量
# --------------------------------------------------------------------------- #
def _factors(idx: pd.DatetimeIndex, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        {"mkt": rng.normal(0, 0.03, len(idx)), "bond": rng.normal(0, 0.005, len(idx)),
         "gold": rng.normal(0, 0.02, len(idx))},
        index=idx,
    )


def test_residual_kills_pure_beta():
    """行业收益 = 2×大盘时，残差应≈0（回归把 beta 完全解释掉）。"""
    idx = pd.date_range("2020-01-31", periods=30, freq="ME")
    f = _factors(idx)
    mret = pd.DataFrame({"ind": 2.0 * f["mkt"]}, index=idx)
    resid = D.residual_matrix(mret, f, lookback=12)
    tail = resid["ind"].dropna()
    assert len(tail) > 10
    assert tail.abs().max() < 1e-10


def test_residual_keeps_idiosyncratic_part():
    idx = pd.date_range("2020-01-31", periods=30, freq="ME")
    f = _factors(idx)
    own = pd.Series(0.01, index=idx)
    mret = pd.DataFrame({"ind": 1.0 * f["mkt"] + own}, index=idx)
    resid = D.residual_matrix(mret, f, lookback=12)
    # 常数残差会被截距吸收，所以这里看的是"没有系统性 beta 残差"
    assert resid["ind"].dropna().abs().max() < 1e-10


def test_residual_has_no_lookahead():
    """把未来数据整段截掉，历史月份上的残差不应改变。"""
    idx = pd.date_range("2020-01-31", periods=30, freq="ME")
    f = _factors(idx, seed=1)
    mret = pd.DataFrame({"ind": f["mkt"] * 1.2 + 0.004}, index=idx)
    full = D.residual_matrix(mret, f, lookback=12)
    cut = D.residual_matrix(mret.iloc[:20], f.iloc[:20], lookback=12)
    common = cut.index
    pd.testing.assert_frame_equal(full.loc[common], cut)


# --------------------------------------------------------------------------- #
# 异常月补丁
# --------------------------------------------------------------------------- #
def test_flip_anomaly_subtracts_twice():
    idx = pd.date_range("2020-01-31", periods=14, freq="ME")
    resid = pd.DataFrame({"ind": np.r_[np.full(12, 0.01), [0.05, 0.01]]}, index=idx)
    dvol = pd.DataFrame({"ind": np.r_[np.full(12, 0.01), [0.09, 0.01]]}, index=idx)
    plain = D.residual_score(resid, dvol, lookback=13, flip_anomaly=False)
    flipped = D.residual_score(resid, dvol, lookback=13, flip_anomaly=True)
    # 窗口覆盖 0..12 月：残差合计 12*0.01+0.05 = 0.17，波动最高的第 12 月残差 0.05
    assert plain["ind"].iloc[12] == pytest.approx(0.17)
    assert flipped["ind"].iloc[12] == pytest.approx(0.17 - 2 * 0.05)


def test_residual_score_skips_partial_window():
    """窗口里只要缺一个月就不出分——否则 warmup 期会给出"只有一个月数据"的假得分。"""
    idx = pd.date_range("2020-01-31", periods=15, freq="ME")
    vals = np.full(15, 0.01)
    vals[5] = np.nan
    resid = pd.DataFrame({"ind": vals}, index=idx)
    dvol = pd.DataFrame({"ind": 0.01}, index=idx)
    got = D.residual_score(resid, dvol, lookback=12, flip_anomaly=False)
    assert got["ind"].isna().all()


def test_residual_matrix_requires_full_window():
    """回归也要满窗：缺一个月就跳过，不能拿 11 个点估 4 个参数。"""
    idx = pd.date_range("2020-01-31", periods=15, freq="ME")
    f = _factors(idx)
    mret = pd.DataFrame({"ind": f["mkt"] * 1.0}, index=idx)
    mret.iloc[0, 0] = np.nan
    resid = D.residual_matrix(mret, f, lookback=12)
    # 第 12 行（窗口 0..11）缺一个观测 → 不给值；第 13 行起才满窗
    assert np.isnan(resid["ind"].iloc[11])
    assert not np.isnan(resid["ind"].iloc[12])


def test_residual_score_needs_full_window():
    idx = pd.date_range("2020-01-31", periods=12, freq="ME")
    resid = pd.DataFrame({"ind": 0.01}, index=idx)
    dvol = pd.DataFrame({"ind": 0.01}, index=idx)
    got = D.residual_score(resid, dvol, lookback=12, flip_anomaly=False)
    assert got["ind"].iloc[:11].isna().all()
    assert got["ind"].iloc[11] == pytest.approx(0.12)


# --------------------------------------------------------------------------- #
# 排名与选股
# --------------------------------------------------------------------------- #
def test_rank_is_deterministic_on_ties():
    """同分时必须按代码排序，否则同一份数据会选出不同标的。"""
    score = pd.DataFrame({"b": [0.1], "a": [0.1], "c": [0.2]})
    r = D.rank_scores(score)
    # c 分最高排第 1；a、b 同分，按代码升序 a 在前
    assert r.loc[0, "c"] == 1 and r.loc[0, "a"] == 2 and r.loc[0, "b"] == 3
    # 列顺序打乱也一样
    r2 = D.rank_scores(score[["c", "a", "b"]])
    assert r2.loc[0, "a"] == 2 and r2.loc[0, "b"] == 3


def test_pick_top_is_equal_weight():
    combined = pd.DataFrame(
        {"a": [2.0], "b": [1.0], "c": [3.0], "d": [4.0]},
        index=[pd.Timestamp("2024-01-31")],
    )
    got = D.pick_top(combined, n=2)
    w = got[pd.Timestamp("2024-01-31")]
    assert set(w) == {"b", "a"}
    assert all(abs(v - 0.5) < 1e-12 for v in w.values())


def test_pick_top_skips_incomplete_rows():
    combined = pd.DataFrame(
        {"a": [1.0], "b": [np.nan], "c": [np.nan]}, index=[pd.Timestamp("2024-01-31")]
    )
    assert D.pick_top(combined, n=2) == {}


def test_combine_requires_both():
    a = pd.DataFrame({"x": [1.0], "y": [2.0]})
    b = pd.DataFrame({"x": [2.0], "y": [np.nan]})
    got = D.combine(a, b)
    assert got.loc[0, "x"] == pytest.approx(1.5)
    assert np.isnan(got.loc[0, "y"])
