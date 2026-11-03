"""核心-卫星组合：混合逻辑与边界。

组合的收益来自两条低相关收益流的叠加，前提是**权重算对**：
核心腿拿 core_weight 平分，卫星腿按剩余比例缩放，总和不能超过 1。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etfquant import combo as C


def test_zero_core_returns_original():
    sat = {pd.Timestamp("2026-11-02"): {"sh516620": 0.5}}
    assert C.blend_targets(sat, 0.0) is sat


def test_blend_splits_weights():
    sat = {pd.Timestamp("2026-11-02"): {"a": 0.25, "b": 0.25}}
    out = C.blend_targets(sat, 0.6, codes=["c1", "c2", "c3", "c4"])
    w = out[pd.Timestamp("2026-11-02")]
    assert w["c1"] == pytest.approx(0.15)      # 0.6 / 4
    assert w["a"] == pytest.approx(0.10)       # 0.25 * 0.4
    assert sum(w.values()) == pytest.approx(0.6 + 0.4 * 0.5)


def test_total_never_exceeds_one():
    sat = {pd.Timestamp("2026-11-02"): {"a": 0.5, "b": 0.5}}
    out = C.blend_targets(sat, 0.6, codes=["c1", "c2"])
    assert sum(out[pd.Timestamp("2026-11-02")].values()) == pytest.approx(1.0)


def test_core_always_present_even_when_satellite_empty():
    sat = {pd.Timestamp("2026-11-02"): {}}
    out = C.blend_targets(sat, 0.6, codes=["c1", "c2"])
    w = out[pd.Timestamp("2026-11-02")]
    assert w["c1"] == pytest.approx(0.3) and w["c2"] == pytest.approx(0.3)


@pytest.mark.parametrize("bad", [-0.1, 1.1])
def test_out_of_range_rejected(bad):
    with pytest.raises(ValueError):
        C.blend_targets({}, bad)


def test_unknown_basket():
    with pytest.raises(ValueError):
        C.core_basket("nope")


def test_core_nav_is_equal_weighted():
    idx = pd.bdate_range("2026-01-05", periods=3)
    close = pd.DataFrame({"c1": [1.0, 1.1, 1.1], "c2": [1.0, 0.9, 0.9]}, index=idx)
    nav = C.core_nav(close, ["c1", "c2"])
    assert nav.iloc[-1] == pytest.approx(1.0)   # +10% 与 -10% 等权
