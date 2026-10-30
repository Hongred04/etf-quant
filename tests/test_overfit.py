"""
过拟合检验工具的单元测试。

这类函数一旦算错，结论会从"没通过"悄悄变成"通过"，比策略本身出错更难发现，
所以这里用**已知答案的合成数据**把它钉住。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from etfquant.overfit import cscv_pbo, deflated_sharpe, walk_forward_select


# --------------------------------------------------------------------------- #
# CSCV / PBO
# --------------------------------------------------------------------------- #
def test_pbo_high_for_pure_noise():
    """全是噪声时，"样本内冠军"到样本外应当接近随机 → PBO ≈ 0.5。

    单次估计的方差很大（N=8、70 个组合时可能落到 0 或 0.87），所以取 5 个种子平均。
    """
    vals = []
    for seed in range(5):
        rets = np.random.default_rng(seed).normal(0, 0.01, size=(1200, 8))
        vals.append(cscv_pbo(rets, n_blocks=8)["pbo"])
    assert 0.3 < float(np.mean(vals)) < 0.7, vals
    assert len(cscv_pbo(rets, n_blocks=8)["omega"]) == 70      # C(8,4)


def test_pbo_low_when_one_variant_really_wins():
    """某个变体每段都多赚一点（真实边际），挑冠军就不该算过拟合。"""
    rng = np.random.default_rng(11)
    rets = rng.normal(0, 0.01, size=(1200, 8))
    rets[:, 3] += 0.0012        # 稳定正边际，不是只在某一段
    res = cscv_pbo(rets, n_blocks=8)
    assert res["pbo"] < 0.1, res["pbo"]


def test_pbo_uses_all_combinations():
    rng = np.random.default_rng(3)
    rets = rng.normal(0, 0.01, size=(400, 6))
    res = cscv_pbo(rets, n_blocks=6)
    assert res["n_combos"] == 20                            # C(6,3)
    assert np.all((res["omega"] >= 0) & (res["omega"] <= 1))


def test_pbo_rejects_odd_blocks():
    with pytest.raises(ValueError):
        cscv_pbo(np.zeros((100, 3)), n_blocks=5)


def test_pbo_handles_zero_variance_variant():
    """某个变体全程空仓（收益恒为 0）时不该除零。"""
    rng = np.random.default_rng(5)
    rets = rng.normal(0, 0.01, size=(400, 5))
    rets[:, 0] = 0.0
    res = cscv_pbo(rets, n_blocks=4)
    assert 0 <= res["pbo"] <= 1
    assert np.all(np.isfinite(res["oos_sharpe"]))


# --------------------------------------------------------------------------- #
# Deflated Sharpe
# --------------------------------------------------------------------------- #
def _returns(sr_annual: float, n: int = 1500, seed: int = 1) -> np.ndarray:
    """构造 Sharpe **恰好**等于目标的收益序列（先去均值再加回目标均值）。"""
    rng = np.random.default_rng(seed)
    r = rng.normal(0, 0.01, size=n)
    r = r - r.mean()
    sd = r.std(ddof=1)
    return r + sr_annual / np.sqrt(252) * sd


def test_dsr_drops_as_trials_grow():
    """试的次数越多，同样的 Sharpe 越不可信。"""
    r = _returns(1.2)
    low = deflated_sharpe(r, n_trials=1, sr_variance=0.0)["dsr"]
    mid = deflated_sharpe(r, n_trials=50, sr_variance=(0.05 / np.sqrt(252)) ** 2)["dsr"]
    high = deflated_sharpe(r, n_trials=500, sr_variance=(0.05 / np.sqrt(252)) ** 2)["dsr"]
    assert low > mid > high


def test_dsr_low_for_weak_sharpe():
    """Sharpe 只有 0.3、又试过上百次时，不该被判为显著。"""
    var = (0.05 / np.sqrt(252)) ** 2
    weak = deflated_sharpe(_returns(0.3, seed=9), n_trials=100, sr_variance=var)["dsr"]
    strong = deflated_sharpe(_returns(1.5, seed=9), n_trials=100, sr_variance=var)["dsr"]
    assert weak < 0.75, weak          # 明显不到 95% 的把握
    assert strong > 0.95, strong      # 同样的试验次数下，Sharpe 1.5 依然显著
    assert strong - weak > 0.2


def test_dsr_threshold_scales_with_trials():
    r = _returns(1.0)
    var = (0.05 / np.sqrt(252)) ** 2
    sr0_10 = deflated_sharpe(r, n_trials=10, sr_variance=var)["sr0"]
    sr0_100 = deflated_sharpe(r, n_trials=100, sr_variance=var)["sr0"]
    assert sr0_100 > sr0_10 > 0


def test_dsr_without_trial_variance_equals_plain_tstat():
    """不传试验方差时退化为普通 t 检验，别静默给出虚高的显著性。"""
    r = _returns(2.0, n=1000, seed=4)
    res = deflated_sharpe(r, n_trials=1, sr_variance=0.0)
    t = r.mean() / r.std(ddof=1) * np.sqrt(len(r) - 1)
    from statistics import NormalDist
    # DSR 的分母含 (峰度-1)/4·SR² 修正，正态样本下与纯 t 统计量差 1e-5 量级
    assert abs(res["dsr"] - NormalDist().cdf(t)) < 1e-3


# --------------------------------------------------------------------------- #
# 滚动前向选择
# --------------------------------------------------------------------------- #
def test_walk_forward_windows_are_non_overlapping():
    idx = pd.bdate_range("2019-01-02", periods=800)
    df = pd.DataFrame({"a": np.zeros(800), "b": np.zeros(800)}, index=idx)
    wf = walk_forward_select(df, is_days=300, oos_days=100)
    assert len(wf) == 5                      # (800-300)/100
    # 样本内窗口是滚动的（会重叠），要保证的是**样本外段**首尾相接、不重叠
    for prev, cur in zip(wf.itertuples(), list(wf.itertuples())[1:]):
        assert cur.oos_start > prev.oos_end


def test_walk_forward_picks_best_in_sample():
    """样本内最好的那个变体必须被选中。"""
    idx = pd.bdate_range("2019-01-02", periods=600)
    rng = np.random.default_rng(2)
    df = pd.DataFrame({
        "bad": rng.normal(0, 0.01, 600),
        "good": rng.normal(0.004, 0.01, 600),   # 稳定正边际（日均 +0.4%，盖过噪声）
    }, index=idx)
    wf = walk_forward_select(df, is_days=400, oos_days=100)
    assert set(wf["picked"]) == {"good"}
    # 单个 100 天窗口的噪声很大（σ≈10%），看两窗平均是否为正更可靠
    assert wf["oos_ret"].mean() > 0


def test_walk_forward_empty_when_too_short():
    idx = pd.bdate_range("2019-01-02", periods=50)
    df = pd.DataFrame({"a": np.zeros(50)}, index=idx)
    assert walk_forward_select(df, is_days=300, oos_days=100).empty
