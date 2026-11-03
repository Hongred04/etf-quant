"""风险预算：缩放系数与目标权重。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etfquant.risk import resolve_scale, scale_targets


def test_cli_beats_env(monkeypatch):
    monkeypatch.setenv("ETF_RISK_SCALE", "0.7")
    assert resolve_scale(0.5) == 0.5


def test_env_used_when_no_cli(monkeypatch):
    monkeypatch.setenv("ETF_RISK_SCALE", "0.6")
    assert resolve_scale(None) == 0.6


def test_default_is_full_exposure(monkeypatch):
    monkeypatch.delenv("ETF_RISK_SCALE", raising=False)
    assert resolve_scale(None) == 1.0


@pytest.mark.parametrize("raw,expected", [("0", 0.1), ("5", 1.0), ("abc", 1.0)])
def test_clamped(monkeypatch, raw, expected):
    monkeypatch.setenv("ETF_RISK_SCALE", raw)
    assert resolve_scale(None) == expected


def test_scale_targets_multiplies_and_keeps_identity():
    tg = {1: {"a": 0.5, "b": 0.5}}
    assert scale_targets(tg, 1.0) is tg
    scaled = scale_targets(tg, 0.5)
    assert scaled[1]["a"] == pytest.approx(0.25)
    assert tg[1]["a"] == 0.5          # 不修改入参
