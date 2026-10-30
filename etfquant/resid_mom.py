"""
残差动量（5 日框架版）：把"扣掉大盘顺风后行业自己的涨幅"做成可以按日更新的因子。

为什么要单独做一版
------------------
外部那套双因子策略用的是**月度**残差：滚动 12 个月回归、再取 12 个月残差求和。
它在本项目里已经跑过（README 7.6），结论是"12 个月 + 月度"这个组合本身就是瓶颈。
但残差因子本身是那套里唯一表现好的单因子（同起点 9.4%，回撤 −39.5%，比纯 12 月动量好），
所以值得问一个更具体的问题：**把它接到 5 日调仓的框架里，还加分吗？**

和月度版的三个区别
------------------
1. 频率：每天都算（调仓日只需其中 380 个），回归用**日收益**而不是月收益；
2. 窗口：回归 252 日、残差求和 `window` 日（默认 120），比月度的 12 个月短一档；
3. 用途：仍按横截面 z 值参与打分，不改变策略其它任何一层。

没有前视
--------
`asof` 之后的数据一行都不取；回归和求和都只用 `≤ asof` 的收益。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def daily_returns(close: pd.DataFrame) -> pd.DataFrame:
    return close.pct_change(fill_method=None)


def factor_matrix(close: pd.DataFrame, codes: list[str]) -> pd.DataFrame:
    """共同市场因子（大盘 / 债券 / 黄金）的日收益，列名固定为 mkt/bond/gold。"""
    out = pd.DataFrame(index=close.index)
    for name, code in zip(("mkt", "bond", "gold"), codes, strict=True):
        if code not in close.columns:
            raise KeyError(f"缺少因子标的 {code}")
        out[name] = close[code].pct_change(fill_method=None)
    return out


def residual_sum_at(
    ret: pd.DataFrame,
    factors: pd.DataFrame,
    asof,
    lookback: int = 252,
    window: int = 120,
    min_obs: int = 200,
) -> pd.Series:
    """
    单个时点的残差动量：`sum(resid[-window:])`，`resid` 来自 `lookback` 日的滚动回归。

    逐列做最小二乘（设计矩阵相同、缺失掩码不同）。用满窗要求兜住"新股只有几十根 K 线
    也给出一个煞有介事的分数"——那种分数在横截面里会被排到最前面，因为样本越短残差越极端。
    """
    asof = pd.Timestamp(asof)
    sub = ret.loc[:asof].tail(lookback)
    if len(sub) < min_obs:
        return pd.Series(dtype=float)
    f = factors.reindex(sub.index)
    fok = f.notna().all(axis=1).to_numpy()
    if fok.sum() < min_obs:
        return pd.Series(dtype=float)
    fv = f.to_numpy(dtype=float)
    out: dict[str, float] = {}
    for c in ret.columns:
        y = sub[c].to_numpy(dtype=float)
        m = fok & np.isfinite(y)
        if m.sum() < min_obs:
            continue
        Xm = np.column_stack([np.ones(m.sum()), fv[m]])
        try:
            beta, *_ = np.linalg.lstsq(Xm, y[m], rcond=None)
        except np.linalg.LinAlgError:
            continue
        resid = y[m] - Xm @ beta
        out[c] = float(resid[-window:].sum())
    return pd.Series(out)


def zscore_row(s: pd.Series, lo: float = 0.02, hi: float = 0.98) -> pd.Series:
    """横截面缩尾 + z 值，口径与 `FactorBank.z_r20` 一致（否则两个因子不可加）。"""
    s = s.replace([np.inf, -np.inf], np.nan).dropna()
    if len(s) < 3:
        return pd.Series(dtype=float)
    lo_v, hi_v = s.quantile(lo), s.quantile(hi)
    s = s.clip(lo_v, hi_v)
    sd = s.std(ddof=1)
    if not np.isfinite(sd) or sd == 0:
        return pd.Series(0.0, index=s.index)
    return (s - s.mean()) / sd


def score_frame(
    close: pd.DataFrame,
    factors: pd.DataFrame,
    dates,
    lookback: int = 252,
    window: int = 120,
    min_obs: int = 200,
) -> pd.DataFrame:
    """
    在给定日期序列上逐点算残差动量并转成 z 值。

    返回的行索引就是传入的 `dates`（策略的调仓日），所以策略层可以按日期直接取用，
    不必再关心频率。
    """
    ret = daily_returns(close)
    rows = {}
    for d in dates:
        raw = residual_sum_at(ret, factors, d, lookback=lookback, window=window, min_obs=min_obs)
        if not raw.empty:
            rows[pd.Timestamp(d)] = zscore_row(raw)
    if not rows:
        return pd.DataFrame(columns=close.columns)
    return pd.DataFrame(rows).T.reindex(columns=close.columns)
