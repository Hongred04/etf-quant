"""前向监控：选股贡献按期汇总与集中度。

这个指标用来回答"2026 那种一年吃掉一半 alpha 的情况会不会重演"。
边界必须钉死：相邻两期的分界日只能算一次，否则累计贡献会虚高。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etfquant.track import selection_by_period, selection_concentration


def _comp(values, start="2026-01-05"):
    idx = pd.bdate_range(start, periods=len(values))
    return pd.DataFrame({"selection": values}, index=idx)


def test_periods_split_without_double_counting():
    comp = _comp([0.01, 0.02, 0.03, 0.04, 0.05])
    d = list(comp.index)
    out = selection_by_period(comp, [d[0], d[2]])
    assert len(out) == 2
    assert out[0]["selection"] == pytest.approx(0.01 + 0.02)
    assert out[1]["selection"] == pytest.approx(0.03 + 0.04 + 0.05)


def test_last_period_runs_to_end():
    comp = _comp([0.01, -0.02, 0.03])
    d = list(comp.index)
    out = selection_by_period(comp, [d[1]])
    assert out[0]["selection"] == pytest.approx(-0.02 + 0.03)


def test_concentration_reports_top_share():
    periods = [{"exec_date": "a", "selection": 0.10},
               {"exec_date": "b", "selection": 0.01},
               {"exec_date": "c", "selection": 0.01}]
    got = selection_concentration(periods)
    assert got["cum"] == pytest.approx(0.12)
    assert got["max_share"] == pytest.approx(0.10 / 0.12)
    assert got["n"] == 3


def test_concentration_handles_negative_cum():
    got = selection_concentration([{"exec_date": "a", "selection": -0.02}])
    assert got["cum"] == pytest.approx(-0.02)
    assert got["max_share"] is None


def test_empty_inputs():
    assert selection_by_period(pd.DataFrame(), []) == []
    assert selection_concentration([])["n"] == 0
