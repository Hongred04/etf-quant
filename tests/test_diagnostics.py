"""诊断层的单测。重点只有两类：**因果性**（不能用未来数据）和**符号/口径**。

这些函数产出的是"结论性数字"（alpha、IC、执行损耗），错了不会报错，
只会让 README 里的论断全部失去依据——所以每一处约定都必须被钉住。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from etfquant import diagnostics as D


def _panel(n_days: int = 320, n_names: int = 30, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ret = rng.normal(0.0004, 0.012, size=(n_days, n_names))
    px = 1.0 + np.cumsum(ret, axis=0)
    px = np.abs(px) + 1.0
    idx = pd.bdate_range("2020-01-01", periods=n_days)
    cols = [f"t{i:02d}" for i in range(n_names)]
    return pd.DataFrame(px, index=idx, columns=cols)


# --------------------------------------------------------------------------- #
# 前视
# --------------------------------------------------------------------------- #
def test_fwd_return_is_pure_forward():
    close = _panel(20, 3)
    f = D.fwd_return(close, 5)
    assert f.iloc[0, 0] == pytest.approx(close.iloc[5, 0] / close.iloc[0, 0] - 1)
    assert f.iloc[-5:, :].isna().all().all()      # 末尾 h 行没有未来可看
    assert not f.iloc[:-5].isna().any().any()


def test_fwd_return_entry_override_uses_next_open():
    """entry 传"次日开盘"时，可收割收益 = close[t+h]/open[t+1]-1。"""
    idx = pd.bdate_range("2021-01-01", periods=10)
    close = pd.DataFrame({"a": np.arange(10.0) + 10}, index=idx)
    open_ = pd.DataFrame({"a": np.arange(10.0) + 100}, index=idx)
    got = D.fwd_return(close, 2, entry=open_.shift(-1))
    assert got["a"].iloc[0] == pytest.approx(12 / 101 - 1)


def test_momentum_factor_ignores_future():
    """改动后半段价格，前半段的因子收益必须一字不变——多空权重 shift(1) 生效。"""
    close = _panel()
    base = D.momentum_factor(close, lookback=20, min_names=20)

    bumped = close.copy()
    bumped.iloc[250:] *= 1.6                       # 只改"未来"
    after = D.momentum_factor(bumped, lookback=20, min_names=20)

    cut = close.index[245]
    pd.testing.assert_series_equal(base.loc[:cut], after.loc[:cut])


# --------------------------------------------------------------------------- #
# IC
# --------------------------------------------------------------------------- #
def test_rowwise_spearman_extremes():
    idx = pd.bdate_range("2021-01-01", periods=2)
    cols = [f"t{i}" for i in range(6)]
    sig = pd.DataFrame([np.arange(6.0), np.arange(6.0)[::-1]], index=idx, columns=cols)
    fwd = pd.DataFrame([np.arange(6.0), np.arange(6.0)], index=idx, columns=cols)
    ic = D.rowwise_spearman(sig, fwd, min_names=5)
    assert ic.iloc[0] == pytest.approx(1.0)
    assert ic.iloc[1] == pytest.approx(-1.0)


def test_rowwise_spearman_needs_enough_names():
    idx = pd.bdate_range("2021-01-01", periods=1)
    cols = list("abcd")
    sig = pd.DataFrame([np.arange(4.0)], index=idx, columns=cols)
    ic = D.rowwise_spearman(sig, sig, min_names=10)
    assert np.isnan(ic.iloc[0])


def test_ic_stats_hit_rate():
    ic = pd.Series([0.1, 0.2, -0.05, 0.03])
    st = D.ic_stats(ic)
    assert st["n"] == 4
    assert st["hit"] == pytest.approx(0.75)
    assert st["ic"] == pytest.approx(0.07)


# --------------------------------------------------------------------------- #
# Newey-West
# --------------------------------------------------------------------------- #
def test_newey_west_penalizes_autocorrelation():
    """同样的均值，正自相关必须让 t 值显著变小（朴素 t 会虚高显著性）。"""
    rng = np.random.default_rng(11)
    n = 400
    x = np.zeros(n)
    for i in range(1, n):
        x[i] = 0.8 * x[i - 1] + rng.normal(0.5, 1.0)
    naive = x.mean() / (x.std(ddof=1) / np.sqrt(n))
    nw = D.newey_west_t(pd.Series(x), lags=5)
    assert abs(nw) < abs(naive) * 0.75


def test_newey_west_constant_series_is_nan():
    assert np.isnan(D.newey_west_t(pd.Series([0.01] * 30)))


# --------------------------------------------------------------------------- #
# 回归
# --------------------------------------------------------------------------- #
def test_ols_hac_recovers_beta():
    rng = np.random.default_rng(5)
    n = 500
    x1 = rng.normal(0, 0.01, n)
    x2 = rng.normal(0, 0.01, n)
    y = 0.0004 + 0.7 * x1 - 0.3 * x2 + rng.normal(0, 0.001, n)
    got = D.ols_hac(y, np.column_stack([np.ones(n), x1, x2]))
    assert got["beta"][0] == pytest.approx(0.0004, abs=1e-4)
    assert got["beta"][1] == pytest.approx(0.7, abs=0.05)
    assert got["beta"][2] == pytest.approx(-0.3, abs=0.05)
    assert abs(got["t"][1]) > 10


def test_factor_regression_flags_pure_beta():
    """构造一条完全由 mkt 决定的"策略"：alpha 必须≈0、R²≈1。"""
    rng = np.random.default_rng(9)
    n = 400
    idx = pd.bdate_range("2020-01-01", periods=n)
    mkt = pd.Series(rng.normal(0.0003, 0.01, n), index=idx)
    nav = (1 + 1.0 * mkt).cumprod()
    fac = pd.DataFrame({
        "mkt": mkt,
        "mom": rng.normal(0, 0.01, n),
        "smb": rng.normal(0, 0.01, n),
    }, index=idx)
    got = D.factor_regression(nav, fac)
    assert abs(got["alpha_ann"]) < 0.02
    assert got["beta_mkt"] == pytest.approx(1.0, abs=0.05)
    assert got["r2"] > 0.95


# --------------------------------------------------------------------------- #
# bootstrap
# --------------------------------------------------------------------------- #
def test_block_bootstrap_deterministic_and_ordered():
    rng = np.random.default_rng(2)
    r = pd.Series(rng.normal(0.0006, 0.01, 900))
    a = D.block_bootstrap(r, block=20, n_boot=300, seed=42)
    b = D.block_bootstrap(r, block=20, n_boot=300, seed=42)
    assert a == b                                   # 同种子必须逐位相同
    c = a["cagr"]
    assert c["p2.5"] < c["p50"] < c["p97.5"]
    assert a["mdd"]["p50"] < 0                      # 回撤中位数必然为负


# --------------------------------------------------------------------------- #
# 执行与容量
# --------------------------------------------------------------------------- #
def test_execution_gap_uses_next_open_and_targets():
    """信号日收盘 → 次日开盘。买入遇跳高、卖出遇跳低，两者都是正成本。"""
    idx = pd.bdate_range("2021-01-01", periods=4)
    close = pd.DataFrame({"a": 10.0}, index=idx)
    open_ = pd.DataFrame({"a": [10.0, 10.2, 10.0, 9.8]}, index=idx)
    # 第 1 天建仓（次日开盘 10.2 成交），第 3 天清仓（第 4 天开盘 9.8 成交）
    targets = pd.DataFrame({"a": [1.0, np.nan, 0.0, np.nan]}, index=idx)
    got = D.execution_gap(targets, open_, close)["all"]
    assert got["per_unit_bp"] == pytest.approx(200, rel=1e-6)
    assert got["buy_gap_mean_bp"] == pytest.approx(200, rel=1e-6)
    assert got["sell_gap_mean_bp"] == pytest.approx(-200, rel=1e-6)


def test_execution_gap_ignores_unchanged_targets():
    """目标权重没变的日子不是成交，不能计入换手（否则每日漂移会被当成交易）。"""
    idx = pd.bdate_range("2021-01-01", periods=5)
    close = pd.DataFrame({"a": 10.0}, index=idx)
    open_ = pd.DataFrame({"a": 10.0}, index=idx)
    same = pd.DataFrame({"a": 1.0}, index=idx)
    got = D.execution_gap(same, open_, close)["all"]
    years = (idx[-1] - idx[0]).days / 365.25
    assert got["per_unit_cost"] == pytest.approx(0.0)
    # 全程只在第一行建了一次仓，累计换手 = 1.0
    assert got["turnover_per_year"] * years == pytest.approx(1.0)


def test_execution_gap_quarantines_illiquid_fills():
    """薄量日的开盘价只可能是零星成交，必须被单独隔离，不能算进"缺口"。"""
    idx = pd.bdate_range("2021-01-01", periods=4)
    close = pd.DataFrame({"a": 10.0, "b": 10.0}, index=idx)
    open_ = pd.DataFrame({"a": [10.0, 9.0, 10.0, 10.0],      # a 在薄量日"跳空" -10%
                          "b": [10.0, 10.1, 10.0, 10.0]}, index=idx)
    targets = pd.DataFrame({"a": [0.5], "b": [0.5]}, index=idx[:1])
    amount = pd.DataFrame({"a": 1e6, "b": 5e7}, index=idx)   # a 只有 100 万成交额
    got = D.execution_gap(targets, open_, close, amount=amount, min_amount=2e7)
    assert got["share_illiquid"] == pytest.approx(0.5)
    assert got["liquid"]["per_unit_bp"] == pytest.approx(100, rel=1e-6)
    assert got["all"]["per_unit_bp"] < 0                     # 混进来就变成"买入还赚钱"


def test_capacity_grows_with_aum():
    idx = pd.bdate_range("2021-01-01", periods=3)
    targets = pd.DataFrame({"a": [1.0, np.nan, np.nan]}, index=idx)
    adv = pd.DataFrame({"a": 1e8}, index=idx)
    got = D.capacity(targets, adv, aum_grid=(1e7, 1e8))
    assert got["median_days"].is_monotonic_increasing
    assert got["median_days"].iloc[0] == pytest.approx(1.0, rel=1e-6)
    assert got["median_days"].iloc[1] == pytest.approx(10.0, rel=1e-6)


def test_quantile_spread_is_monotone_when_signal_is_linear():
    idx = pd.bdate_range("2021-01-01", periods=1)
    cols = [f"t{i:02d}" for i in range(100)]
    sig = pd.DataFrame([np.arange(100.0)], index=idx, columns=cols)
    fwd = pd.DataFrame([np.arange(100.0) * 1e-4], index=idx, columns=cols)
    got = D.quantile_spread(sig, fwd, idx, n_groups=10, min_names=50)
    assert got.is_monotonic_increasing
    assert got["Q9"] > got["Q0"]


def test_tail_spread_counts_only_picked_names():
    idx = pd.bdate_range("2021-01-01", periods=1)
    cols = [f"t{i:02d}" for i in range(60)]
    sig = pd.DataFrame([np.arange(60.0)], index=idx, columns=cols)
    fwd = pd.DataFrame([np.arange(60.0) * 1e-4], index=idx, columns=cols)
    got = D.tail_spread(sig, fwd, idx, top=3, min_names=20)
    assert got["n"] == 1
    assert got["mean_bp"] > 0
    assert got["hit"] == pytest.approx(1.0)


def test_equity_exposure_excludes_bond():
    idx = pd.bdate_range("2021-01-01", periods=2)
    w = pd.DataFrame({"a": [1.0, 0.5], "z": [0.0, 0.5]}, index=idx)
    cat = pd.Series({"a": "行业", "z": "债券"})
    ex = D.equity_exposure(w, cat)
    assert ex.tolist() == pytest.approx([1.0, 0.5])


def test_exposure_split_separates_days():
    idx = pd.bdate_range("2021-01-01", periods=10)
    fr = pd.Series(np.r_[np.full(5, 0.004), np.full(5, -0.004)], index=idx)
    ex = pd.Series(np.r_[np.full(5, 1.0), np.full(5, 0.5)], index=idx)
    got = D.exposure_split(fr, ex)
    assert got["on"]["mean_ann"] > 0 > got["off"]["mean_ann"]
    assert got["on"]["n"] == 5 and got["off"]["n"] == 5


# --------------------------------------------------------------------------- #
# 趋势与分区间
# --------------------------------------------------------------------------- #
def test_trend_test_detects_linear_decay():
    """把一个确定斜率的序列喂进去，斜率必须回得来、t 必须显著为负。"""
    idx = pd.date_range("2019-01-01", periods=300, freq="W")
    yrs = (idx - idx[0]).days / 365.25
    rng = np.random.default_rng(4)
    x = 0.05 - 0.01 * yrs + rng.normal(0, 0.02, len(idx))   # 每年掉 0.01
    got = D.trend_test(pd.Series(x, index=idx))
    assert got["slope_per_year"] == pytest.approx(-0.01, abs=0.003)
    assert got["t"] < -2
    assert got["verdict"] == "有衰减证据"


def test_trend_test_reports_no_evidence_for_flat_series():
    idx = pd.date_range("2019-01-01", periods=300, freq="W")
    rng = np.random.default_rng(8)
    got = D.trend_test(pd.Series(rng.normal(0.03, 0.02, len(idx)), index=idx))
    assert abs(got["t"]) < 1.5
    assert got["verdict"] in ("无衰减证据", "弱证据")


def test_trend_test_needs_enough_points():
    idx = pd.date_range("2024-01-01", periods=10, freq="W")
    assert D.trend_test(pd.Series(np.arange(10.0), index=idx))["verdict"] == "样本不足"


def test_period_summary_partitions_dates():
    """两段加起来必须等于总决策日数，不能有漏或重。"""
    close = _panel(400, 60)
    idx = close.index[60::5]
    sig = close.pct_change(20).loc[idx]
    fwd = D.fwd_return(close, 5)
    periods = {"A": ("2020-01-01", "2020-12-31"), "B": ("2021-01-01", "2022-12-31")}
    got = D.period_summary(sig, close, idx, periods, horizon=5)
    total = sum(int(r["n"]) for _, r in got.iterrows())
    in_win = sum(1 for d in idx if pd.Timestamp("2020-01-01") <= d <= pd.Timestamp("2022-12-31"))
    assert total == in_win
    assert "ic" in got.columns and "q_spread_bp" in got.columns


def test_quantile_gap_series_matches_aggregate():
    """逐日 Q9−Q0 的均值，必须等于 quantile_spread 报出的 Q9−Q0。"""
    idx = pd.bdate_range("2021-01-01", periods=3)
    cols = [f"t{i:02d}" for i in range(100)]
    sig = pd.DataFrame(np.tile(np.arange(100.0), (3, 1)), index=idx, columns=cols)
    fwd = pd.DataFrame(np.tile(np.arange(100.0) * 1e-4, (3, 1)), index=idx, columns=cols)
    ser = D.quantile_gap_series(sig, fwd, idx, n_groups=10, min_names=50)
    agg = D.quantile_spread(sig, fwd, idx, n_groups=10, min_names=50)
    assert len(ser) == 3
    assert ser.mean() * 1e4 == pytest.approx(agg["Q9"] - agg["Q0"], rel=1e-9)


def test_snapshot_rows_covers_key_metrics():
    doc = {
        "meta": {"frozen_at": "2026-10-30 13:53", "end": "2026-10-29"},
        "regression": {"alpha_ann": 0.0958, "alpha_t": 1.378},
        "bootstrap": {"cagr": {"p2.5": -0.0185, "p97.5": 0.4092},
                      "mdd": {"p50": -0.3932}},
        "periods": [{"period": "2019-2022", "ic": 0.02, "tail_mean_bp": -2.71},
                    {"period": "2023-2026", "ic": 0.017, "tail_mean_bp": 62.46}],
        "trend": {"IC": {"slope_per_year": 0.00335, "t": 0.434, "verdict": "无衰减证据"}},
    }
    rows = dict(D.snapshot_rows(doc))
    assert rows["诊断快照"].startswith("2026-10-30 13:53")
    assert "9.6%" in rows["alpha（年化，HAC t）"] and "1.38" in rows["alpha（年化，HAC t）"]
    assert "-2.7bp / +62.5bp" == rows["top3−全池 2019-2022 / 2023-2026"]
    assert "无衰减证据" in rows["IC 趋势（年）"]


def test_snapshot_rows_degrades_on_partial_doc():
    """缺字段就少一行，绝不抛异常——这张表坏了不该让 README 同步失败。"""
    assert D.snapshot_rows({}) == []
    got = D.snapshot_rows({"meta": {"frozen_at": "2026-10-30 13:53", "end": "2026-10-29"}})
    assert len(got) == 1 and got[0][0] == "诊断快照"
