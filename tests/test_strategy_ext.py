"""
可选增强开关的单元测试：同质化去重 / 错峰子组合 / 波动率目标。

这三个开关默认全关（主线 S4 行为不变），但既然留在代码里，就要锁住行为，
避免以后有人调参时把它们悄悄改成默认生效。

设计约束（都来自实盘教训）：
1. 去重只把重复标的**排到队尾**，不删除——否则候选不足时会把仓位打空；
2. 错峰按「已有子组合数」平均，不从第一天就留现金；
3. 波动目标只减不增，且缩放的敞口要转成债券，不是留现金。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etfquant.strategy import Panel, TrendGatedAdaptive, _average_sleeves


def _panel(prices: dict[str, np.ndarray], cats: dict[str, str] | None = None) -> Panel:
    idx = pd.bdate_range("2020-01-01", periods=len(next(iter(prices.values()))))
    close = pd.DataFrame(prices, index=idx)
    vol = pd.DataFrame({k: np.full(len(idx), 5e5) for k in prices}, index=idx)
    cats = cats or {k: "宽基" for k in prices}
    meta = pd.DataFrame(
        {"name": list(prices), "category": [cats[k] for k in prices]}, index=list(prices)
    )
    return Panel(close=close, open_=close.copy(), high=close.copy(), low=close.copy(),
                 volume=vol, meta=meta)


def _walk(n: int, mu: float, sd: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return 10 * np.exp(np.cumsum(rng.normal(mu, sd, n)))


# --------------------------------------------------------------------------- #
# 错峰：等权平均
# --------------------------------------------------------------------------- #
def test_average_sleeves_basic():
    a = {"X": 0.5, "Y": 0.5}
    b = {"Y": 0.5, "Z": 0.5}
    out = _average_sleeves([a, b])
    assert out["X"] == pytest.approx(0.25)
    assert out["Y"] == pytest.approx(0.5)   # 两期都有，权重相加
    assert out["Z"] == pytest.approx(0.25)
    assert sum(out.values()) == pytest.approx(1.0)


def test_average_sleeves_uses_available_count():
    """只有 1 份信号时不能除以 N，否则第一天就自动留出 3/4 现金。"""
    one = {"X": 1.0}
    assert _average_sleeves([one])["X"] == pytest.approx(1.0)


def test_average_sleeves_empty():
    assert _average_sleeves([]) == {}


def test_sleeve_reduces_turnover_flag_default_off():
    n = 320
    px = {"A": _walk(n, 0.001, 0.01, 1), "B": _walk(n, 0.0005, 0.012, 2),
          "C": _walk(n, 0.0002, 0.011, 3), "D": _walk(n, -0.0005, 0.01, 4)}
    panel = _panel(px)
    s = TrendGatedAdaptive(panel)
    assert s.sleeve_n == 1, "默认必须是不错峰，保证现网行为不变"


# --------------------------------------------------------------------------- #
# 去重：只把重复标的排到队尾
# --------------------------------------------------------------------------- #
def _dedup_fixture() -> tuple[Panel, TrendGatedAdaptive]:
    n = 320
    base = _walk(n, 0.001, 0.012, 7)
    px = {
        "A": base,                    # 与 B 完全同源（如同一指数的两只 ETF）
        "B": base * 1.001,
        "C": _walk(n, 0.0008, 0.013, 8),
        "D": _walk(n, 0.0006, 0.011, 9),
    }
    panel = _panel(px)
    strat = TrendGatedAdaptive(panel, dedup_corr=0.95)
    return panel, strat


def test_dedup_pushes_duplicate_to_back():
    panel, strat = _dedup_fixture()
    dt = panel.close.index[-1]
    ranked = ["A", "B", "C", "D"]
    out = strat._dedup_rank(dt, ranked)
    assert len(out) == 4, "去重只能重排，不能丢标的"
    assert out[0] == "A", "同质组里保留分数最高的那只"
    assert out.index("B") > out.index("C"), "重复标的应排到独立标的之后"


def test_dedup_disabled_keeps_order():
    panel, _ = _dedup_fixture()
    strat = TrendGatedAdaptive(panel)          # dedup_corr=None
    dt = panel.close.index[-1]
    assert strat._dedup_rank(dt, ["A", "B", "C"]) == ["A", "B", "C"]


def test_dedup_keeps_all_names_when_insufficient():
    """去重后仍要能凑满仓位：全是同源标的时，队尾的仍会被补位逻辑捡回来。"""
    n = 320
    base = _walk(n, 0.001, 0.01, 11)
    px = {"A": base, "B": base * 1.001, "C": base * 1.002}
    panel = _panel(px)
    strat = TrendGatedAdaptive(panel, dedup_corr=0.95)
    dt = panel.close.index[-1]
    out = strat._dedup_rank(dt, ["A", "B", "C"])
    assert set(out) == {"A", "B", "C"}


# --------------------------------------------------------------------------- #
# 波动率目标：只减不增，且减下来的敞口转债券
# --------------------------------------------------------------------------- #
def test_vol_target_only_reduces_exposure():
    n = 400
    px = {"A": _walk(n, 0.001, 0.03, 21), "B": _walk(n, 0.001, 0.031, 22),
          "C": _walk(n, 0.001, 0.029, 23), "BOND": _walk(n, 0.0001, 0.002, 24)}
    panel = _panel(px, cats={"A": "宽基", "B": "政策", "C": "政策", "BOND": "债券"})
    dt = panel.close.index[-1]
    elig = None

    plain = TrendGatedAdaptive(panel)
    elig = plain.eligible_at(dt)
    w_plain = plain.target_weights(dt, elig)
    eq_plain = sum(v for k, v in w_plain.items() if k != "BOND")

    gated = TrendGatedAdaptive(panel, vol_target=0.10, vol_floor=0.2)
    w_gated = gated.target_weights(dt, elig)
    eq_gated = sum(v for k, v in w_gated.items() if k != "BOND")

    assert eq_gated <= eq_plain + 1e-9, "波动闸只能减仓，不能加杠杆"
    assert w_gated.get("BOND", 0) > w_plain.get("BOND", 0), "减下来的敞口应转成债券"


def test_vol_target_off_by_default():
    n = 320
    panel = _panel({"A": _walk(n, 0.001, 0.01, 31)})
    assert TrendGatedAdaptive(panel).vol_target is None


# --------------------------------------------------------------------------- #
# 池子口径：剔除宽基不能顺手把基准删掉
# --------------------------------------------------------------------------- #
def test_apply_pool_marks_tradable_without_dropping_benchmark():
    """
    这是踩过的坑：直接删列会把沪深300 一起删掉，`_bench_series()` 退化为
    "宽基等权代理"，闸门静默换了判据，回测结果看着变好但策略已不是原来那个。
    """
    from etfquant.universe import apply_pool

    n = 320
    px = {"sh510300": _walk(n, 0.0004, 0.012, 41),   # 纯大盘宽基
          "sh588000": _walk(n, 0.0008, 0.02, 42),    # 科创（也叫宽基，但要保留）
          "sh516620": _walk(n, 0.0009, 0.018, 43),   # 行业
          "sh511090": _walk(n, 0.0001, 0.003, 44)}   # 债券
    panel = _panel(px, cats={"sh510300": "宽基", "sh588000": "宽基",
                             "sh516620": "行业", "sh511090": "债券"})
    # is_broad_beta 按**中文名**识别，测试数据里要把名字补齐
    panel.meta.loc["sh510300", "name"] = "沪深300ETF华泰柏瑞"
    panel.meta.loc["sh588000", "name"] = "科创50ETF华夏"
    panel.meta.loc["sh516620", "name"] = "影视ETF国泰"
    panel.meta.loc["sh511090", "name"] = "30年国债ETF鹏扬"
    core = apply_pool(panel, "core")

    assert set(core.close.columns) == set(panel.close.columns), "列必须全部保留"
    assert core.meta.loc["sh510300", "tradable"] is False or not core.meta.loc["sh510300", "tradable"]
    assert core.meta.loc["sh588000", "tradable"], "创业板/科创 不该被剔除"
    assert core.meta.loc["sh516620", "tradable"]

    strat = TrendGatedAdaptive(core)
    dt = core.close.index[-1]
    elig = strat.eligible_at(dt)
    assert not elig["sh510300"], "基准不应参与选股"
    assert elig["sh588000"] and elig["sh516620"]


def test_apply_pool_full_is_identity():
    from etfquant.universe import apply_pool

    n = 320
    panel = _panel({"A": _walk(n, 0.001, 0.01, 51)})
    assert apply_pool(panel, "full") is panel


def test_apply_pool_rejects_unknown_mode():
    from etfquant.universe import apply_pool

    n = 320
    panel = _panel({"A": _walk(n, 0.001, 0.01, 61)})
    with pytest.raises(ValueError):
        apply_pool(panel, "cor")


# --------------------------------------------------------------------------- #
# 闸门档位：默认两档，第三档需显式开启
# --------------------------------------------------------------------------- #
def _gate_panel():
    n = 320
    return _panel({"sh510300": _walk(n, 0.0004, 0.011, 71),
                   "sh516620": _walk(n, 0.0009, 0.02, 72),
                   "sh511090": _walk(n, 0.0001, 0.003, 73)},
                  cats={"sh510300": "宽基", "sh516620": "行业", "sh511090": "债券"})


def test_regime_gate_is_two_tier_by_default():
    """默认 `regime_low=None`：只有"满敞口/半敞口"两档，不存在第三档。"""
    s = TrendGatedAdaptive(_gate_panel())
    assert s.regime_low is None
    assert s.expo_map[1] == s.expo_map[2], "两档时第三档应复用 mid，而不是另立一个值"
    assert {s._exposure(x) for x in (0.0, 0.2, 0.35, 0.5, 0.69)} == {0.5}
    assert s._exposure(0.7) == 1.0
    assert s._exposure(1.0) == 1.0


def test_regime_third_tier_opt_in():
    """显式传 regime_low 才恢复三档（用于复现早期消融：0/0.5/1）。"""
    s = TrendGatedAdaptive(_gate_panel(), regime_low=0.35, expo_low=0.0)
    assert s._exposure(0.2) == 0.0
    assert s._exposure(0.5) == 0.5
    assert s._exposure(0.8) == 1.0


def test_expo_low_alone_is_noop():
    """只传 expo_low 而不传 regime_low 时第三档并未启用——避免"以为改了空仓"。

    这是这次简化的核心：`regime_low` 是开关，`expo_low` 只是档位取值。
    """
    s = TrendGatedAdaptive(_gate_panel(), expo_low=0.0)
    assert s.regime_low is None
    assert s._exposure(0.0) == 0.5, "未启用第三档时，低分区仍是半仓"


def test_gate_disable_still_full_position():
    """关闸那个消融变体（三项全 1.0）不受两档化影响。"""
    s = TrendGatedAdaptive(_gate_panel(), expo_high=1.0, expo_mid=1.0,
                           expo_low=1.0, regime_low=-1.0)
    assert {s._exposure(x) for x in (0.0, 0.5, 1.0)} == {1.0}
