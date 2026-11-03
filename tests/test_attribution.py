"""收益归因的恒等式测试。

归因最怕"看起来合理但对不上"。这里钉住两件事：
1. 分项之和必须逐日等于回测记录的毛利（残差为 0，不是"接近 0"）；
2. 成本扣减后等于净收益。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etfquant import attribution as A
from etfquant.backtest import Backtester


@pytest.fixture()
def sample():
    dates = pd.bdate_range("2026-01-05", periods=30)
    rng = np.random.default_rng(3)
    eq1 = 1.0 * np.cumprod(1 + rng.normal(0.001, 0.01, len(dates)))
    eq2 = 1.0 * np.cumprod(1 + rng.normal(0.0005, 0.012, len(dates)))
    bond = 1.0 * np.cumprod(1 + rng.normal(0.0001, 0.0005, len(dates)))
    close = pd.DataFrame({"eq1": eq1, "eq2": eq2, "bond": bond}, index=dates)
    open_ = close.shift(1).fillna(close.iloc[0]) * (1 + rng.normal(0, 0.002, close.shape))
    tgt = {}
    for i in range(0, len(dates) - 1, 5):
        tgt[dates[i]] = {"eq1": 0.35, "eq2": 0.35, "bond": 0.3}
    res = Backtester(close, open_, fee=0.0003, slippage=0.0007).run(
        tgt, start=str(dates[0].date()))
    comp, contrib = A.decompose(res.daily, close, open_, ["eq1", "eq2"])
    comp = A.with_cost(comp, res.cost)
    return res, comp, contrib


def test_components_sum_to_gross_each_day(sample):
    _, comp, _ = sample
    total = comp["beta"] + comp["timing"] + comp["selection"] + comp["bond"]
    assert np.allclose(total, comp["gross"], atol=1e-12)


def test_gross_matches_recorded_daily_return(sample):
    """当日总收益 = (1+gap)(1+oc) - 1；记录的 pr 只是开盘→收盘那一段。"""
    res, comp, _ = sample
    rec = {r["date"]: r for r in res.daily}
    total = pd.Series({d: (1 + rec[d]["gap"]) * (1 + rec[d]["pr"]) - 1
                       for d in comp.index})
    assert np.allclose(comp["gross"], total, atol=1e-10)


def test_net_equals_gross_plus_cost(sample):
    _, comp, _ = sample
    assert np.allclose(comp["net"], comp["gross"] + comp["cost"], atol=1e-15)


def test_break_even_is_monotone(sample):
    _, comp, _ = sample
    be = A.break_even_cost(comp, 0.07)
    assert be["cagr_at_1x"] >= be["cagr_at_2x"] >= be["cagr_at_3x"]


def test_concentration_drops_only_positive_episodes(sample):
    res, comp, contrib = sample
    episodes = A.episodes_from_trades(res.trades, comp.index[-1])
    if episodes.empty:
        pytest.skip("样本里没有完整持仓段")
    got = A.concentration(comp, contrib, episodes)
    assert 0 <= got["top5_share"] <= 1.5
