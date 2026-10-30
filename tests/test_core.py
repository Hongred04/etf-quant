"""
核心模块单元测试。

重点验证三件事：
1. 因子无前视偏差（look-ahead free）—— 篡改未来数据不应改变历史因子值；
2. 回测引擎的净值计算与成本扣减正确；
3. 绩效指标口径正确。

运行：python -m pytest tests -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etfquant import factors as F
from etfquant import metrics
from etfquant.backtest import Backtester


@pytest.fixture()
def px() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    n = 400
    idx = pd.bdate_range("2020-01-01", periods=n)
    data = {}
    for i, col in enumerate(["A", "B", "C"]):
        steps = rng.normal(0.0006 * (i + 1), 0.012, n)
        data[col] = 10 * np.exp(np.cumsum(steps))
    return pd.DataFrame(data, index=idx)


def test_momentum_no_lookahead(px: pd.DataFrame) -> None:
    """篡改 t 之后的价格，t 之前的动量值必须完全一致。"""
    m = F.momentum(px, 20, skip=5)
    t = px.index[300]

    tampered = px.copy()
    tampered.loc[tampered.index > t] *= 3.0  # 未来价格全部改掉
    m2 = F.momentum(tampered, 20, skip=5)

    pd.testing.assert_series_equal(m.loc[t], m2.loc[t])


def test_ma_distance_no_lookahead(px: pd.DataFrame) -> None:
    d = F.ma_distance(px, 60)
    t = px.index[350]
    tampered = px.copy()
    tampered.loc[tampered.index > t] = np.nan
    d2 = F.ma_distance(tampered, 60)
    pd.testing.assert_series_equal(d.loc[t], d2.loc[t])


def test_momentum_value(px: pd.DataFrame) -> None:
    """手工核对单一数值。"""
    m = F.momentum(px, 5, skip=0)
    t, t5 = px.index[100], px.index[95]
    expected = px.loc[t, "A"] / px.loc[t5, "A"] - 1
    assert m.loc[t, "A"] == pytest.approx(expected)


def test_backtester_buy_and_hold(px: pd.DataFrame) -> None:
    """
    满仓持有单一标的：建仓日收益按 open→close 计（T+1 开盘执行），
    其后每日收益应与标的 close-to-close 收益完全一致。
    """
    single = px[["A"]]
    bt = Backtester(single, single, fee=0.0, slippage=0.0)
    targets = {single.index[0]: {"A": 1.0}}
    res = bt.run(targets)

    r_nav = res.nav.pct_change().dropna()
    r_px = single["A"].pct_change().dropna()
    np.testing.assert_allclose(r_nav.iloc[1:].to_numpy(), r_px.iloc[1:].to_numpy(), rtol=1e-9)

    # 建仓日收益 = open -> close
    day0 = single["A"].iloc[0] / single["A"].open.iloc[0] - 1 if hasattr(single["A"], "open") else None
    if day0 is not None:
        assert r_nav.iloc[0] == pytest.approx(day0, rel=1e-9)


def test_backtester_cost_applied(px: pd.DataFrame) -> None:
    single = px[["A"]]
    free = Backtester(single, single, fee=0.0, slippage=0.0).run({single.index[0]: {"A": 1.0}})
    paid = Backtester(single, single, fee=0.001, slippage=0.0).run({single.index[0]: {"A": 1.0}})
    assert paid.nav.iloc[1] < free.nav.iloc[1]


def test_backtester_full_cash(px: pd.DataFrame) -> None:
    """空权重时应保持净值不变。"""
    bt = Backtester(px, px)
    res = bt.run({px.index[10]: {}})
    assert res.nav.nunique() == 1


def test_metrics_basic() -> None:
    idx = pd.bdate_range("2021-01-01", periods=252)
    nav = pd.Series(1.0 * (1.001 ** np.arange(252)), index=idx)
    assert metrics.total_return(nav) == pytest.approx(1.001**251 - 1)
    assert metrics.max_drawdown(nav) == pytest.approx(0.0)
    assert metrics.cagr(nav) > 0


def test_max_drawdown() -> None:
    nav = pd.Series([1.0, 1.2, 0.6, 0.9], index=pd.bdate_range("2022-01-03", periods=4))
    assert metrics.max_drawdown(nav) == pytest.approx(0.6 / 1.2 - 1)


def test_zscore_cs_shape(px: pd.DataFrame) -> None:
    z = F.zscore_cs(px)
    row = z.dropna().iloc[-1]
    assert row.mean() == pytest.approx(0.0, abs=1e-9)
    assert row.std() == pytest.approx(1.0, abs=1e-6)


# --------------------------------------------------------------------------- #
# 回归：本轮代码审查发现的问题（防止再次退化）
# --------------------------------------------------------------------------- #
def test_backtester_first_day_return_counted() -> None:
    """
    首日即调仓时，必须计入当日 open→close 的涨跌。

    原实现在 ``if i == 0`` 分支里只扣成本、不算收益，
    导致满仓 +5% 的首日净值仍是 1.0（收益被吞、成本照扣）。
    """
    idx = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])
    close = pd.DataFrame({"A": [1.00, 1.05, 1.05, 1.05]}, index=idx)
    open_ = pd.DataFrame({"A": [1.00, 1.00, 1.05, 1.05]}, index=idx)

    # 信号在 01-02，T+1 即 01-03 开盘建仓 —— 01-03 正是回测首个交易日，
    # 触发的是 i == 0 分支。
    res = Backtester(close, open_, fee=0.0, slippage=0.0).run(
        {idx[0]: {"A": 1.0}}, start=str(idx[1].date())
    )
    assert res.nav.index[0] == idx[1]
    assert res.nav.iloc[0] == pytest.approx(1.05, rel=1e-12)


def test_backtester_first_day_cost_still_applied() -> None:
    """首日建仓仍应扣一次成本，且成本与收益不能互相抵消。"""
    idx = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])
    close = pd.DataFrame({"A": [1.00, 1.00, 1.00, 1.00]}, index=idx)
    open_ = pd.DataFrame({"A": [1.00, 1.00, 1.00, 1.00]}, index=idx)

    res = Backtester(close, open_, fee=0.0003, slippage=0.0007).run(
        {idx[0]: {"A": 1.0}}, start=str(idx[1].date())
    )
    # 从空仓建到满仓：Σ|Δw| = 1.0（成交额 = 100% 净值），
    # 单边换手 turn = 0.5 → 成本 = 2 × turn × 0.001 = 0.1%
    assert res.turnover.iloc[0] == pytest.approx(0.5)
    assert res.nav.iloc[0] == pytest.approx(0.999, rel=1e-12)


def test_cagr_uses_calendar_years() -> None:
    """
    CAGR 必须按日历天数折算。

    A 股每年约 242 个交易日，按 252 折算会低估年数、高估 CAGR。
    这里构造一段恰好 2 个日历年、净值翻倍的序列：正确年化为 √2-1。
    """
    idx = pd.DatetimeIndex(["2020-01-01", "2020-12-31", "2022-01-01"])
    nav = pd.Series([1.0, 1.4, 2.0], index=idx)
    years = (idx[-1] - idx[0]).days / 365.25
    assert metrics.years_elapsed(nav) == pytest.approx(years)
    assert metrics.cagr(nav) == pytest.approx(2.0 ** (1 / years) - 1)


def test_zscore_cs_single_column_is_nan() -> None:
    """
    单列横截面 z-score 为 NaN（ddof=1 下 std 无定义）。

    记录该行为：策略层必须对"只剩 1 只候选"单独兜底，
    否则会因 score 全 NaN 而空仓。
    """
    one = pd.DataFrame({"A": [0.05]}, index=pd.to_datetime(["2024-01-02"]))
    assert F.zscore_cs(one).isna().all().all()


def test_multifactor_single_candidate_not_empty() -> None:
    """多因子策略在只剩 1 只合格标的时不能返回空仓。"""
    from etfquant.strategy import MultiFactorRotation, Panel

    n = 320
    idx = pd.bdate_range("2020-01-01", periods=n)
    px = pd.DataFrame({"A": 10 * np.exp(np.cumsum(np.full(n, 0.001)))}, index=idx)
    panel = Panel(
        close=px,
        open_=px.copy(),
        high=px.copy(),
        low=px.copy(),
        # 成交额 = close × volume × 100，需过 2000 万门槛
        volume=pd.DataFrame({"A": np.full(n, 5e5)}, index=idx),
        meta=pd.DataFrame({"name": ["测试A"], "category": ["宽基"]}, index=["A"]),
    )
    strat = MultiFactorRotation(panel)
    w = strat.target_weights(idx[-1], strat.eligible_at(idx[-1]))
    assert w, "单候选不应空仓"
    assert all(v <= strat.max_weight + 1e-9 for v in w.values())


def test_download_panel_does_not_cache_partial(monkeypatch, tmp_path) -> None:
    """抓取失败时不得写入缓存，否则残缺序列会被后续运行反复复用。"""
    from etfquant import datafeed

    cache = datafeed.CacheConfig(root=tmp_path)

    def boom(*_a, **_k):
        raise RuntimeError("模拟 HTTP 失败")

    monkeypatch.setattr(datafeed, "fetch_kline", boom)
    monkeypatch.setattr(datafeed, "market_last_trade_date", lambda: pd.Timestamp("2024-01-05"))

    report: dict = {}
    frames = datafeed.download_panel(
        ["sh510300"], "2020-01-01", "2024-01-05", cache, workers=1, report=report
    )
    assert frames == {}
    assert "sh510300" in report["failed"]
    assert not list((tmp_path / "kline_qfq").glob("*.csv")), "失败时不应留下缓存文件"
