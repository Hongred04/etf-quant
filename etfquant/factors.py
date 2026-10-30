"""
因子库（Factor Library）。

全部因子均为纯历史滚动计算（rolling / shift），不引入任何未来信息：
  F(t) 仅依赖 <= t 的行情数据，回测信号在 t 日收盘生成、t+1 日开盘执行。

口径参考
--------
* 动量类因子：Jegadeesh & Titman (1993) 12-1 动量框架，A 股 ETF 因行业轮动快，
  采用 20/60/120 日多周期并剔除最近 5 日（skip week）以规避短期反转。
* 风险调整动量：收益 / 波动，等价于信息比率式的"单位风险动量"。
* 波动率倒数权重：风险平价（Risk Parity）的一阶近似，参考 etf-trend-rp-vt 实现。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def momentum(px: pd.DataFrame, window: int, skip: int = 0) -> pd.DataFrame:
    """区间收益率。skip>0 时剔除最近 skip 个交易日（规避短期反转）。"""
    if skip > 0:
        past = px.shift(skip)
    else:
        past = px
    return past / px.shift(skip + window) - 1.0


def ma(px: pd.DataFrame, window: int) -> pd.DataFrame:
    return px.rolling(window, min_periods=max(5, window // 3)).mean()


def ma_distance(px: pd.DataFrame, window: int) -> pd.DataFrame:
    """价格相对均线偏离度（>0 表示在均线之上）。"""
    return px / ma(px, window) - 1.0


def realized_vol(px: pd.DataFrame, window: int, annualize: bool = True) -> pd.DataFrame:
    ret = px.pct_change(fill_method=None)
    vol = ret.rolling(window, min_periods=max(10, window // 3)).std()
    return vol * np.sqrt(TRADING_DAYS) if annualize else vol


def downside_vol(px: pd.DataFrame, window: int, annualize: bool = True) -> pd.DataFrame:
    ret = px.pct_change(fill_method=None)
    neg = ret.where(ret < 0, 0.0)
    vol = np.sqrt((neg**2).rolling(window, min_periods=max(10, window // 3)).mean())
    return vol * np.sqrt(TRADING_DAYS) if annualize else vol


def sharpe_momentum(px: pd.DataFrame, window: int, skip: int = 0) -> pd.DataFrame:
    """风险调整动量：区间收益 / 同期年化波动。"""
    r = momentum(px, window, skip)
    v = realized_vol(px, window)
    return r / v.replace(0.0, np.nan)


def trend_slope(px: pd.DataFrame, window: int = 60) -> pd.DataFrame:
    """均线斜率（归一化），衡量趋势的持续性而非幅度。"""
    m = ma(px, window)
    return m.pct_change(20)


def max_drawdown(px: pd.DataFrame, window: int = 120) -> pd.DataFrame:
    """滚动区间最大回撤（负数）。"""
    roll_max = px.rolling(window, min_periods=window // 2).max()
    return px / roll_max - 1.0


def volume_ratio(vol: pd.DataFrame, short: int = 5, long_: int = 60) -> pd.DataFrame:
    """量比：短期均量 / 长期均量。值过高提示交易拥挤。"""
    return vol.rolling(short, min_periods=2).mean() / vol.rolling(long_, min_periods=10).mean()


def rsrs(high: pd.DataFrame, low: pd.DataFrame, window: int = 18) -> pd.DataFrame:
    """
    RSRS 阻力支撑相对强度（简化版）。

    对每 window 个交易日做 high ~ low 的 OLS 斜率，再用斜率的历史分位标准化。
    参考 Everbright Securities (2017) 的 RSRS 择时研究。
    """
    out = pd.DataFrame(np.nan, index=high.index, columns=high.columns)
    for col in high.columns:
        h = high[col]
        low_s = low[col]
        betas = []
        idx = []
        hv = h.to_numpy(dtype=float)
        lv = low_s.to_numpy(dtype=float)
        n = len(hv)
        beta_arr = np.full(n, np.nan)
        for i in range(window - 1, n):
            xs = lv[i - window + 1 : i + 1]
            ys = hv[i - window + 1 : i + 1]
            mask = np.isfinite(xs) & np.isfinite(ys)
            if mask.sum() < window * 0.8:
                continue
            x, y = xs[mask], ys[mask]
            vx = x.var()
            if vx <= 0:
                continue
            beta_arr[i] = np.cov(x, y, ddof=0)[0, 1] / vx
        betas = pd.Series(beta_arr, index=high.index)
        z = (betas - betas.rolling(120, min_periods=40).mean()) / betas.rolling(
            120, min_periods=40
        ).std()
        out[col] = z
    return out


def zscore_cs(df: pd.DataFrame) -> pd.DataFrame:
    """横截面标准化（按日）。"""
    mu = df.mean(axis=1)
    sd = df.std(axis=1).replace(0.0, np.nan)
    return df.sub(mu, axis=0).div(sd, axis=0)


def rank_cs(df: pd.DataFrame) -> pd.DataFrame:
    """横截面百分位（0~1，1 为最强）。"""
    return df.rank(axis=1, pct=True)


def winsorize_cs(df: pd.DataFrame, lower: float = 0.02, upper: float = 0.98) -> pd.DataFrame:
    """横截面缩尾，抑制极端值对打分的干扰。"""
    lo = df.quantile(lower, axis=1)
    hi = df.quantile(upper, axis=1)
    return df.clip(lower=lo, upper=hi, axis=0)


class FactorSet:
    """一次性计算并缓存全部因子，避免策略层重复计算。"""

    def __init__(self, close: pd.DataFrame, volume: pd.DataFrame | None = None,
                 high: pd.DataFrame | None = None, low: pd.DataFrame | None = None):
        self.close = close
        self.volume = volume
        self.high = high
        self.low = low
        self._cache: dict[str, pd.DataFrame] = {}

    def get(self, name: str, **kwargs) -> pd.DataFrame:
        key = name + str(sorted(kwargs.items()))
        if key in self._cache:
            return self._cache[key]
        fn = globals().get(name)
        if fn is None:
            raise KeyError(f"未知因子: {name}")
        args = {"px": self.close, **kwargs}
        if name in ("volume_ratio",):
            if self.volume is None:
                raise ValueError("缺少成交量数据")
            args = {"vol": self.volume, **kwargs}
        if name == "rsrs":
            if self.high is None or self.low is None:
                raise ValueError("缺少高低价数据")
            args = {"high": self.high, "low": self.low, **kwargs}
        out = fn(**args)
        self._cache[key] = out
        return out
