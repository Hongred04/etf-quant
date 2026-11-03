"""live 归档 vs 回测目标的保真度比对。

前向验证只有在「记录的信号 = 策略当日输出」时才成立。这里钉住三件事：
1. 同权重判一致，换一只标的判不一致；
2. 归档权的舍入（0.1667 vs 1/6）不能算成偏差；
3. backfill 期不参与——它是用当前策略回放的，拿来证明保真属于循环论证。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etfquant import track as T


def _archive(rows):
    return pd.DataFrame(rows, columns=["signal_date", "code", "name", "weight", "source"])


def test_identical_signals_match():
    a = _archive([
        ("2026-11-02", "sh516620", "影视", 1 / 6, "live"),
        ("2026-11-02", "sh511520", "政金债", 0.5, "live"),
    ])
    tg = {pd.Timestamp("2026-11-02"): {"sh516620": 1 / 6, "sh511520": 0.5}}
    out = T.signal_fidelity(a, tg)
    assert len(out) == 1
    assert out[0]["match"] is True
    assert out[0]["l1"] == 0


def test_different_holding_is_not_a_match():
    a = _archive([
        ("2026-11-02", "sh516620", "影视", 1 / 6, "live"),
        ("2026-11-02", "sh511520", "政金债", 0.5, "live"),
    ])
    tg = {pd.Timestamp("2026-11-02"): {"sz159915": 1 / 6, "sh511520": 0.5}}
    out = T.signal_fidelity(a, tg)
    assert out[0]["match"] is False
    assert out[0]["overlap"] == 1
    assert out[0]["l1"] > 0.1


def test_rounding_is_within_tolerance():
    a = _archive([("2026-11-02", "sh516620", "影视", 0.1667, "live")])
    tg = {pd.Timestamp("2026-11-02"): {"sh516620": 1 / 6}}
    out = T.signal_fidelity(a, tg)
    assert out[0]["match"] is True
    assert out[0]["max_diff"] < 1e-4


def test_backfill_rows_are_ignored():
    a = _archive([
        ("2026-10-26", "sh513310", "旧", 0.3, "backfill"),
        ("2026-11-02", "sh516620", "影视", 0.5, "live"),
    ])
    tg = {pd.Timestamp("2026-11-02"): {"sh516620": 0.5}}
    out = T.signal_fidelity(a, tg)
    assert [r["signal_date"] for r in out] == ["2026-11-02"]


def test_empty_archive_returns_empty():
    assert T.signal_fidelity(_archive([]), {}) == []
    assert T.signal_fidelity(None, {}) == []


def test_stale_archive_price_is_flagged():
    """权重一致但归档价与当日收盘差 3%：说明归档用了陈旧价，必须单独标出来。"""
    a = pd.DataFrame([
        ("2026-11-02", "sh516620", "影视", 0.5, 0.90, "live"),
        ("2026-11-02", "sh511520", "政金债", 0.5, 119.0, "live"),
    ], columns=["signal_date", "code", "name", "weight", "ref_price", "source"])
    tg = {pd.Timestamp("2026-11-02"): {"sh516620": 0.5, "sh511520": 0.5}}
    close = pd.DataFrame(
        {"sh516620": [0.927], "sh511520": [119.747]},
        index=[pd.Timestamp("2026-11-02")],
    )
    out = T.signal_fidelity(a, tg, close=close)
    assert out[0]["match"] is True          # 权重没问题
    assert out[0]["price_stale"] is True    # 价格有问题
    assert out[0]["price_diff"] > 0.02
