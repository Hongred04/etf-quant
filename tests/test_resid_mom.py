"""
残差动量（5 日框架版）测试。

三条必须守住：纯 beta 必须被剥干净、未来数据不能影响历史分数、样本不够时不许给分。
第三条尤其重要——短样本的残差最极端，一旦漏进去，横截面排名会把"新上市 + 没数据"
的标的排到第一。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etfquant import resid_mom as R


def _panel(n: int = 320, seed: int = 0) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2020-01-01", periods=n)
    mkt = rng.normal(0, 0.012, n)
    bond = rng.normal(0, 0.0015, n)
    gold = rng.normal(0, 0.008, n)
    close = pd.DataFrame(index=idx)
    close["sh510300"] = 4.0 * np.cumprod(1 + mkt)
    close["sh511010"] = 100.0 * np.cumprod(1 + bond)
    close["sh518880"] = 5.0 * np.cumprod(1 + gold)
    close["beta2"] = 3.0 * np.cumprod(1 + 2 * mkt)          # 纯 2 倍 beta
    close["mix"] = 3.0 * np.cumprod(1 + 0.5 * mkt + rng.normal(0, 0.004, n))
    factors = R.factor_matrix(close, ["sh510300", "sh511010", "sh518880"])
    return close, factors


def test_pure_beta_is_stripped():
    close, f = _panel()
    ret = R.daily_returns(close)
    got = R.residual_sum_at(ret, f, close.index[-1], lookback=250, window=120, min_obs=200)
    assert abs(got["beta2"]) < 1e-8       # 2 倍 beta 应被完全解释掉
    assert abs(got["mix"]) > 1e-4         # 真正的特质部分要留下来


def test_no_lookahead():
    close, f = _panel()
    ret = R.daily_returns(close)
    cut = close.index[250]
    a = R.residual_sum_at(ret, f, cut, lookback=250, window=120, min_obs=200)
    b = R.residual_sum_at(R.daily_returns(close.loc[:cut]), f.loc[:cut], cut,
                          lookback=250, window=120, min_obs=200)
    pd.testing.assert_series_equal(a.sort_index(), b.sort_index())


def test_short_history_is_skipped():
    close, f = _panel(n=80)
    ret = R.daily_returns(close)
    got = R.residual_sum_at(ret, f, close.index[-1], lookback=250, window=120, min_obs=200)
    assert got.empty


def test_zscore_row_standardizes_and_winsorizes():
    s = pd.Series({"a": 1.0, "b": 2.0, "c": 3.0, "d": 100.0})
    z = R.zscore_row(s)
    assert abs(z.mean()) < 1e-9
    assert z.std(ddof=1) == pytest.approx(1.0, abs=1e-9)
    # 100 会被缩到 98 分位，不至于把其它三只压成 -1.7 / 0 / 1.7
    assert z.abs().max() < 1.5


def test_zscore_row_handles_all_equal_and_empty():
    assert (R.zscore_row(pd.Series({"a": 1.0, "b": 1.0, "c": 1.0})) == 0).all()
    assert R.zscore_row(pd.Series(dtype=float)).empty


def test_score_frame_index_is_dates():
    close, f = _panel()
    dates = [close.index[260], close.index[280], close.index[300]]
    fr = R.score_frame(close, f, dates, lookback=250, window=120, min_obs=200)
    assert list(fr.index) == dates
    assert set(fr.columns) == set(close.columns)
    assert fr["mix"].notna().all()
