"""
均线金叉死叉策略（视频精简版）的纯函数实现。

规则原文
--------
1. 优先选 60 日均线走平或向上的 ETF（趋势安全垫）；
2. 5 日均线上穿 20 日均线（金叉）买入，持有；
3. 5 日均线下穿 20 日均线（死叉）离场。

怎么落成代码
------------
"金叉持有、死叉离场"就是一个状态：**fast > slow 时在场，否则空仓**——
不需要显式状态机，一条布尔矩阵就是全部持仓规则。
原文没有说明"同时有多只 ETF 出现金叉怎么办"，这里取池内全部满足条件的等权
（这等价于视频里"分批进场"的组合化版本）；
原文的"补仓摊薄"无法在组合级回测里忠实复现（需要对单只持仓做加仓记账），
只能如实标注为未测，而不是假装测了。

"走平或向上"的量化
------------------
取 MA60 相对 5 日前的差值 ≥ 0（`trend_look=5`）。这是个主观选择：用 1 日差值
噪声太大（走平的定义会天天翻转），用 20 日又太钝。取 5 日与"金叉后至少持有
一段"的节奏匹配。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(close: pd.DataFrame, n: int) -> pd.DataFrame:
    return close.rolling(n, min_periods=n).mean()


def in_position(close: pd.DataFrame, fast: int = 5, slow: int = 20) -> pd.DataFrame:
    """
    持仓状态矩阵：True = 5 日线在 20 日线上方（金叉后的持有段）。

    上穿那天起为 True，下穿那天起为 False——与"金叉买、死叉卖"逐日对齐，
    信号在 T 日收盘生成，T+1 开盘执行（由 Backtester 统一处理）。
    """
    return sma(close, fast) > sma(close, slow)


def trend_ok(close: pd.DataFrame, n: int = 60, look: int = 5) -> pd.DataFrame:
    """MA60 走平或向上：MA60 相对 `look` 日前不下降。"""
    ma = sma(close, n)
    return ma.diff(look) >= 0


def build_targets(
    close: pd.DataFrame,
    fast: int = 5,
    slow: int = 20,
    trend_look: int | None = 5,
    trend_n: int = 60,
    min_hist: int = 60,
) -> dict[pd.Timestamp, dict[str, float]]:
    """
    逐日生成目标权重：当天处于"金叉持有态"且通过趋势过滤的标的，等权。

    没有满足条件的标的时返回空权重 = 全部现金（原文未提空仓期怎么处理，
    现金是最保守且最忠实的选择）。权重只在状态变化时变，回测器对不变权重的
    再平衡不会产生交易，换手由金叉/死叉的真实进出驱动。
    """
    state = in_position(close, fast, slow)
    if trend_look is not None:
        state = state & trend_ok(close, trend_n, trend_look)
    hist = close.notna().cumsum()
    state = state & (hist > min_hist)

    out: dict[pd.Timestamp, dict[str, float]] = {}
    prev: dict[str, float] | None = None
    for dt, row in state.iterrows():
        active = row.index[row.fillna(False)]
        if len(active) == 0:
            w: dict[str, float] = {}
        else:
            w = {c: 1.0 / len(active) for c in active}
        if prev is not None and prev.keys() == w.keys() and all(
            abs(prev[k] - v) < 1e-9 for k, v in w.items()
        ):
            continue          # 权重没变就不写：省内存，回测器也不产生交易
        out[dt] = w
        prev = w
    return out


def single_etf_targets(
    close: pd.Series, fast: int = 5, slow: int = 20, trend_look: int | None = 5,
    trend_n: int = 60,
) -> dict[pd.Timestamp, dict[str, float]]:
    """视频最常见的用法：单只 ETF 全仓进出。在场 = 1.0，离场 = 现金。"""
    code = close.name
    sub = close.to_frame()
    state = in_position(sub, fast, slow)[code]
    if trend_look is not None:
        state = state & trend_ok(sub, trend_n, trend_look)[code]
    out: dict[pd.Timestamp, dict[str, float]] = {}
    prev: bool | None = None
    for dt, flag in state.items():
        if pd.isna(flag):
            continue
        w = {code: 1.0} if bool(flag) else {}
        if prev is None or prev != bool(flag):
            out[dt] = w
        prev = bool(flag)
    return out
