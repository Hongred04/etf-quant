"""
第二排序键：52 周高点距离等"只能微调"的因子。

为什么需要单独一层
------------------
主排序是 R20（20 日动量，skip 5 日）。任何**新因子直接加进主排序**都会重演
7.1/7.6/7.8 的老路——多因子合成把"尽快换到最强"稀释掉，两段子区间必然反号。
所以这里只提供"在近平局处打破平局"的能力：

    score = z20 + w · z_secondary      （w ≈ 0.05）

z 值在 ±2 内，w=0.05 时贡献最多 ±0.1，只能翻动 **z20 差 < 0.1** 的配对。
这不是妥协，是刻意的：R20 的第一名永远进，只有第 3 名与第 4 名咬得很近时才换人。

行业扩散度在本池子里是退化的
----------------------------
"同行业有多少只在涨"在**股票**上是有效宽度指标；在本项目里，同一行业的几只 ETF
跟踪的是同一个指数，日收益相关性接近 1，"扩散度"退化成"这只自己涨没涨"，
与 R20 共线。7.12 用相关系数实测确认了这一点，所以没有把它接进排序。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from etfquant import factors as F


def high_52w_gap(
    close: pd.DataFrame, window: int = 250, min_periods: int = 200
) -> pd.DataFrame:
    """
    距 52 周高点的距离：`close / rolling_max(close, 250) - 1`，取值 (−1, 0]。

    越接近 0 越靠近一年新高。George & Hwang (2004) 的原始定义用
    `price / 52w_high`，并以 52 周低点做缩放；这里不做低点缩放——缩放项会引入
    分母接近 0 的爆炸（长期阴跌的标的），而本项目只需要**横截面排序**，
    单调变换不影响排序结果。

    `min_periods=200`：不足 200 日不给值，避免新上市 ETF 用 30 天数据算出
    "距新高 0%"这种假信号。
    """
    roll = close.rolling(window, min_periods=min_periods).max()
    return close.div(roll) - 1.0


def zscore(df: pd.DataFrame) -> pd.DataFrame:
    """横截面缩尾 + 标准化，与 `FactorBank` 里的 z 值同口径（2%/98%）。"""
    return F.zscore_cs(F.winsorize_cs(df))


def score_frame(
    close: pd.DataFrame,
    dates: pd.Index,
    window: int = 250,
    min_periods: int = 200,
) -> pd.DataFrame:
    """把 52 周高点距离算成"行=调仓日、列=代码"的 z 值矩阵，供策略 `secondary` 参数使用。"""
    z = zscore(high_52w_gap(close, window=window, min_periods=min_periods))
    idx = pd.DatetimeIndex(sorted(pd.DatetimeIndex(dates).unique()))
    return z.reindex(idx)


def blend_secondary(score: pd.Series, sec: pd.Series, weight: float) -> pd.Series:
    """
    把第二排序键以 `weight` 混进主分数。缺值按 0（中性）处理，不整只剔除。

    独立成纯函数是为了能单测"只能翻动近平局"这条性质——它是这个设计的全部意义，
    一旦有人在别的分支里把 weight 调大，测试会先炸。
    """
    if weight <= 0 or sec is None or len(sec) == 0:
        return score
    s = sec.reindex(score.index).astype(float).fillna(0.0)
    return score + float(weight) * s


def industry_breadth(
    close: pd.DataFrame,
    groups: pd.Series | dict[str, str],
    window: int = 20,
    min_members: int = 4,
) -> pd.DataFrame:
    """
    行业内扩散度：同组中 **N 日收益 > 0 的比例**，广播回组内每一只。

    仅用于研究与对照。ETF 池里同组标的多为同指数产品，相关性接近 1，
    结果是"N 只一起涨或一起跌"，退化成二值量——见模块 docstring 的说明。
    """
    ret = close.pct_change(window, fill_method=None)
    up = (ret > 0).astype(float).where(ret.notna())
    g = pd.Series(groups).reindex(close.columns)
    valid = g.notna()
    if valid.sum() == 0:
        return pd.DataFrame(index=close.index, columns=close.columns, dtype=float)

    counts = g[valid].value_counts()
    big = set(counts[counts >= min_members].index)
    out = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    for name in big:
        cols = [c for c in close.columns if g.get(c) == name]
        breadth = up[cols].mean(axis=1, skipna=True)
        for c in cols:                 # 逐列赋值：整块赋 2D 数组在 pandas 里要长度对齐
            out[c] = breadth.to_numpy()
    return out
