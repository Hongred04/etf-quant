"""元标签：标签口径、走前向验证的因果性、仓位映射的边界。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from etfquant import meta as M


def _xy(n=200, seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2019-01-02", periods=n)
    x = pd.DataFrame({
        "regime": rng.uniform(0, 1, n),
        "vol": rng.uniform(0.10, 0.45, n),
        "hit": rng.uniform(0.2, 0.9, n),
    }, index=idx)
    # 让 y 依赖 hit（正）与 vol（负），并加噪声
    logit = 2.0 * (x["hit"] - 0.5) - 3.0 * (x["vol"] - 0.25)
    p = 1 / (1 + np.exp(-logit))
    y = pd.Series((rng.uniform(size=n) < p).astype(float), index=idx)
    return x, y


# --------------------------------------------------------------------------- #
# 逻辑回归
# --------------------------------------------------------------------------- #
def test_fit_logit_recovers_signs():
    x, y = _xy(400)
    mu, sd = x.mean(), x.std(ddof=0)
    w = M.fit_logit(((x - mu) / sd).to_numpy(), y.to_numpy())
    assert w[3] > 0, "hit 越高越该赚 → 系数应为正"
    assert w[2] < 0, "波动越高越该亏 → 系数应为负"


def test_fit_logit_is_bitwise_deterministic():
    """同一份数据两次拟合必须逐位相同——策略的可复现性依赖这一点。"""
    x, y = _xy(250)
    a = M.fit_logit(x.to_numpy(), y.to_numpy())
    b = M.fit_logit(x.to_numpy(), y.to_numpy())
    assert np.array_equal(a, b)


def test_fit_logit_survives_separable_data():
    """完全可分时 Hessian 会退化，必须靠 L2 撑住而不是抛异常。"""
    x = np.column_stack([np.linspace(-1, 1, 40), np.linspace(1, -1, 40)])
    y = (x[:, 0] > 0).astype(float)
    w = M.fit_logit(x, y)
    assert np.all(np.isfinite(w))


# --------------------------------------------------------------------------- #
# 走前向验证
# --------------------------------------------------------------------------- #
def test_walk_forward_is_causal():
    """截断未来不能改变历史预测——这是整个设计成立的前提。"""
    x, y = _xy(220)
    full = M.walk_forward(x, y, min_train=100)
    cut = M.walk_forward(x.iloc[:170], y.iloc[:170], min_train=100)
    pd.testing.assert_series_equal(full.iloc[:170], cut, check_names=False)


def test_walk_forward_requires_training_history():
    x, y = _xy(150)
    got = M.walk_forward(x, y, min_train=100)
    assert got.iloc[:100].isna().all()
    assert got.iloc[100:].notna().any()


def test_walk_forward_returns_probabilities():
    x, y = _xy(220)
    got = M.walk_forward(x, y, min_train=100).dropna()
    assert got.between(0, 1).all()


# --------------------------------------------------------------------------- #
# 标签与特征
# --------------------------------------------------------------------------- #
def test_rolling_hit_never_uses_current_label():
    idx = pd.bdate_range("2020-01-01", periods=6)
    labels = pd.Series([1, 1, 0, 1, 0, 0], index=idx, dtype=float)
    got = M.rolling_hit(labels, window=3, shift=1)
    # 第 4 个点 = labels[1..3] 的均值（不含 labels[4] 自己）
    assert got.iloc[4] == pytest.approx((1 + 0 + 1) / 3)


def test_build_features_drops_incomplete_rows():
    idx = pd.bdate_range("2020-01-01", periods=4)
    reg = pd.Series([0.5, 0.6, np.nan, 0.7], index=idx)
    vol = pd.Series([0.2, 0.2, 0.2, 0.2], index=idx)
    hit = pd.Series([0.5, 0.5, 0.5, np.nan], index=idx)
    got = M.build_features(reg, vol, hit)
    assert list(got.index) == [idx[0], idx[1]]


def test_top_basket_return_averages_picked_names():
    idx = pd.bdate_range("2021-01-04", periods=6)
    # 至少 20 列：top_basket_return 有"横截面不足 20 只不出标签"的保护
    close = pd.DataFrame({f"t{i:02d}": np.linspace(1.0, 1.0 + i * 0.05, 6) for i in range(30)},
                         index=idx)
    score = pd.DataFrame([np.arange(30.0)], index=[idx[0]], columns=close.columns)
    got = M.top_basket_return(close, score, [idx[0]], top=3, horizon=5)
    manual = ((close.iloc[5] / close.iloc[0] - 1).reindex(["t27", "t28", "t29"]).mean())
    assert got.iloc[0] == pytest.approx(manual)


# --------------------------------------------------------------------------- #
# 仓位映射
# --------------------------------------------------------------------------- #
def test_to_exposure_threshold_boundaries():
    p = pd.Series([0.70, 0.60, 0.50, 0.45, 0.30])
    got = M.to_exposure(p, "threshold")
    assert list(got) == [1.0, 1.0, 0.75, 0.75, 0.5]


def test_to_exposure_is_bounded_and_never_levers():
    p = pd.Series(np.linspace(0, 1, 21))
    for mode in ("threshold", "linear", "downside"):
        got = M.to_exposure(p, mode)
        assert got.min() >= 0.5 - 1e-12 and got.max() <= 1.0 + 1e-12


def test_to_exposure_downside_only_never_increases():
    p = pd.Series([0.2, 0.44, 0.46, 0.9])
    assert list(M.to_exposure(p, "downside")) == [0.5, 0.5, 1.0, 1.0]


def test_to_exposure_fills_missing_with_full():
    """没有预测（训练期）时保持满仓：不能因为"还不会预测"就先减一半。"""
    got = M.to_exposure(pd.Series([np.nan, 0.7]))
    assert got.iloc[0] == 1.0
