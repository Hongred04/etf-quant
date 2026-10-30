"""alpha 跟踪层的单测。

这一层的输出是"结论性数字"（alpha 还在不在），算错了不会报错，
只会让人对一条正在衰减的策略保持乐观——所以每个口径都要钉死。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from etfquant import alpha_track as A


def _factors(idx: pd.DatetimeIndex, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return pd.DataFrame({
        "mkt": rng.normal(0.0003, 0.01, len(idx)),
        "mom": rng.normal(0.0002, 0.01, len(idx)),
        "smb": rng.normal(0.0001, 0.008, len(idx)),
    }, index=idx)


def test_residual_is_zero_when_nav_follows_model_exactly():
    """净值严格等于 α + Σβ·因子 时，残差必须逐月为 0。"""
    idx = pd.bdate_range("2024-01-01", periods=120)
    f = _factors(idx)
    betas = {"mkt": 0.7, "mom": 0.3, "smb": 0.2}
    alpha_ann = 0.06
    r = alpha_ann / 252 + sum(betas[k] * f[k] for k in betas)
    nav = (1 + r).cumprod()
    got = A.monthly_residual(nav, f, alpha_ann, betas)
    assert len(got) >= 4
    assert np.allclose(got.to_numpy(), 0.0, atol=1e-10)


def test_residual_captures_only_the_unexplained_part():
    """额外加一个 5% 的单月冲击，残差只应当在那一个月出现。"""
    idx = pd.bdate_range("2024-01-01", periods=120)
    f = _factors(idx)
    betas = {"mkt": 0.7, "mom": 0.3, "smb": 0.2}
    alpha_ann = 0.0
    r = sum(betas[k] * f[k] for k in betas)
    shock = pd.Series(0.0, index=idx)
    jan_days = (idx.year == 2024) & (idx.month == 3)
    shock[jan_days] = 0.05 / jan_days.sum()      # 3 月整月多涨 5%
    nav = (1 + r + shock).cumprod()
    got = A.monthly_residual(nav, f, alpha_ann, betas)
    assert got["2024-03"] == pytest.approx(0.05, abs=2e-3)
    others = got.drop(index="2024-03")
    assert np.allclose(others.to_numpy(), 0.0, atol=1e-10)


def test_percentile_rank_bounds():
    hist = pd.Series([-0.1, 0.0, 0.05, 0.1])
    assert A.percentile_rank(-1.0, hist) == pytest.approx(0.0)
    assert A.percentile_rank(1.0, hist) == pytest.approx(1.0)
    assert A.percentile_rank(0.02, hist) == pytest.approx(0.5)


def test_se_shrinks_with_sqrt_n():
    """标准误必须按 √n 收敛：4 倍月数 → 一半误差。这是全文所有"还要多久"的来源。"""
    s1 = A.se_annualized(0.05, 12)
    s2 = A.se_annualized(0.05, 48)
    assert s2 == pytest.approx(s1 / 2, rel=1e-9)
    assert A.se_annualized(0.05, 12) == pytest.approx(12 * 0.05 / np.sqrt(12))


def test_months_helpers_are_consistent():
    sigma, target = 0.055, 0.05
    n = A.months_to_se(sigma, target)
    assert A.se_annualized(sigma, int(round(n))) == pytest.approx(target, rel=1e-3)
    # 要分辨的效应越大，需要的月数越少
    assert A.months_to_detect(sigma, 0.20) < A.months_to_detect(sigma, 0.10)
    # z 越严，需要越久
    assert A.months_to_detect(sigma, 0.10, z=3) > A.months_to_detect(sigma, 0.10, z=2)


def test_build_block_needs_enough_history():
    idx = pd.bdate_range("2024-01-01", periods=60)     # 只有 3 个月
    f = _factors(idx)
    nav = (1 + f["mkt"] * 0.7).cumprod()
    assert A.build_block(nav, f, 0.06, {"mkt": 0.7, "mom": 0.0, "smb": 0.0}, "2024-02") is None


def test_build_block_accumulates_from_live_start_only():
    """回填期不能被算进"前向"累计——前向证据只能从真实归档那天开始。"""
    idx = pd.bdate_range("2024-01-01", periods=260)
    f = _factors(idx)
    betas = {"mkt": 0.0, "mom": 0.0, "smb": 0.0}
    shock = pd.Series(0.0, index=idx)
    mar = (idx.year == 2024) & (idx.month == 3)
    shock[mar] = 0.05 / mar.sum()                 # 3 月有一个 5% 的冲击（回填期）
    nav = (1 + 0.08 / 252 + shock).cumprod()
    got = A.build_block(nav, f, 0.08, betas, "2024-09", live_start="2024-07")
    assert got["window_start"] == "2024-07"
    assert got["window_n"] == 3                   # 7/8/9 三个月
    assert got["window_cum"] == pytest.approx(0.0, abs=1e-9)   # 3 月的冲击被排除在外
    assert got["hist_n"] > got["window_n"]        # 历史分布仍用全样本
    assert got["window_se"] > 0
