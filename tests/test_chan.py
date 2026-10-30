"""
缠论结构实现的测试。

这个模块的价值全在"能不能实时用"上，所以测试的重点不是画得对不对，
而是**因果性**：t 时刻的状态只能由 ≤ t 的数据决定。
一旦这条破了，回测会好得离谱，而且不会报错——本模块的离线版就是这么虚高 8~10 倍的。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etfquant import chan as C


def _bars(n: int = 200, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    px = 10 * np.cumprod(1 + rng.normal(0, 0.012, n))
    idx = pd.bdate_range("2020-01-01", periods=n)
    return pd.DataFrame({"open": px, "close": px,
                         "high": px * 1.004, "low": px * 0.996}, index=idx)


def test_merge_resolves_containment():
    """包含关系要合并成一根，合并后的K线数量应少于原始。"""
    idx = pd.bdate_range("2020-01-01", periods=3)
    df = pd.DataFrame({"high": [10.0, 9.9, 10.1], "low": [9.0, 9.2, 8.9]}, index=idx)
    merged = C.merge_bars(df)
    assert len(merged) < 3


def test_online_states_is_causal():
    """核心不变量：截断未来不改变历史状态。"""
    df = _bars()
    full = C.online_states(df["high"], df["low"])
    cut = df.index[120]
    part = C.online_states(df.loc[:cut, "high"], df.loc[:cut, "low"])
    pd.testing.assert_series_equal(full.loc[:cut], part, check_names=False)


def test_online_states_flags_alternate():
    """状态机只会在"底→顶→底"的顺序上翻转；连续两天同向不重复计入。"""
    df = _bars(seed=3)
    st = C.online_states(df["high"], df["low"]).to_numpy()
    flips = int((st[1:] != st[:-1]).sum())
    assert 2 <= flips <= len(st) / 2


def test_online_vs_offline_differ():
    """
    离线（全样本端点回放）与在线状态必然不同——这正是未来函数的量。
    如果这条测试哪天不成立了，说明状态机被改成了"偷看未来"的版本。
    """
    df = _bars(seed=5)
    on = C.online_states(df["high"], df["low"])
    merged = C.merge_bars(df)
    pv = C.pivots(C.fractals(merged))
    off = pd.Series(False, index=df.index)
    for i in range(len(pv) - 1):
        a = df.index.searchsorted(pv[i]["date"], side="right") - 1 + 2
        b = df.index.searchsorted(pv[i + 1]["date"], side="right") - 1 + 2
        off.iloc[max(a, 0):min(max(b, a), len(df))] = pv[i]["kind"] == "bottom"
    assert int((on != off.ffill().fillna(False).astype(bool)).sum()) > 0
