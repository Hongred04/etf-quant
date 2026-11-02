"""绩效指标（Performance Metrics）。

口径说明
--------
* CAGR      = (期末净值/期初净值)^(365.25/日历天数) - 1
              （不用"交易日数/252"：A 股实际每年约 242 个交易日，
               252 折算会把 7.81 年压缩成 7.51 年，CAGR 高估约 0.8pct）
* Sharpe    = (年化收益 - 无风险利率) / 年化波动，无风险利率默认 1.5%
* Sortino   = (年化收益 - 无风险利率) / 年化下行波动
* MaxDD     = min(净值/历史峰值 - 1)
* Calmar    = CAGR / |MaxDD|
* 年化系数按 252 交易日。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252
DEFAULT_RF = 0.015


def to_returns(nav: pd.Series) -> pd.Series:
    return nav.pct_change(fill_method=None).dropna()


def total_return(nav: pd.Series) -> float:
    if len(nav) < 2:
        return np.nan
    return nav.iloc[-1] / nav.iloc[0] - 1.0


def years_elapsed(nav: pd.Series) -> float:
    """净值序列跨越的年数。

    优先用**日历天数**（CAGR 本是日历概念）；索引不是日期时退回交易日折算。
    """
    if len(nav) < 2:
        return 0.0
    idx = nav.index
    if isinstance(idx, pd.DatetimeIndex) and idx[-1] > idx[0]:
        return (idx[-1] - idx[0]).days / 365.25
    return len(nav) / TRADING_DAYS


def cagr(nav: pd.Series) -> float:
    if len(nav) < 2 or nav.iloc[0] <= 0:
        return np.nan
    years = years_elapsed(nav)
    if years <= 0:
        return np.nan
    return (nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1.0


def ann_vol(nav: pd.Series) -> float:
    r = to_returns(nav)
    if r.empty:
        return np.nan
    return r.std() * np.sqrt(TRADING_DAYS)


def sharpe(nav: pd.Series, rf: float = DEFAULT_RF) -> float:
    """
    标准夏普：日超额收益均值 / 日超额收益标准差 × √252。

    注意不使用 (CAGR - rf) / 年化波动 —— 几何收益低于算术收益，
    那种写法会系统性低估夏普（本策略约低估 0.05）。
    """
    r = to_returns(nav)
    if len(r) < 20:
        return np.nan
    excess = r - rf / TRADING_DAYS
    sd = excess.std()
    if not np.isfinite(sd) or sd == 0:
        return np.nan
    return float(excess.mean() / sd * np.sqrt(TRADING_DAYS))


def sortino(nav: pd.Series, rf: float = DEFAULT_RF) -> float:
    """
    标准索提诺：年化超额收益 / 年化下行偏差。

    下行偏差 = √( mean( min(r, 0)² ) ) × √252，只惩罚下跌波动。
    """
    r = to_returns(nav)
    if len(r) < 20:
        return np.nan
    excess = r - rf / TRADING_DAYS
    downside = float(np.sqrt((np.minimum(r, 0.0) ** 2).mean()) * np.sqrt(TRADING_DAYS))
    if not np.isfinite(downside) or downside == 0:
        return np.nan
    return float(excess.mean() * TRADING_DAYS / downside)


def max_drawdown(nav: pd.Series) -> float:
    if nav.empty:
        return np.nan
    peak = nav.cummax()
    return float((nav / peak - 1.0).min())


def max_dd_detail(nav: pd.Series) -> tuple[float, int, str, str]:
    """返回 (最大回撤, 最长回撤天数, 峰值日, 谷底日)。"""
    peak = nav.cummax()
    dd = nav / peak - 1.0
    if dd.empty:
        return np.nan, 0, "", ""
    trough = dd.idxmin()
    peak_date = nav.loc[:trough].idxmax()
    seg = dd.loc[peak_date:trough]
    return float(dd.min()), int(len(seg)), str(peak_date.date()), str(trough.date())


def calmar(nav: pd.Series) -> float:
    mdd = max_drawdown(nav)
    if not np.isfinite(mdd) or mdd == 0:
        return np.nan
    return cagr(nav) / abs(mdd)


def underwater_stats(nav: pd.Series) -> dict:
    """水下时间统计：多久没回本，而不只是跌多深。

    与 ``max_drawdown`` 互补，不能互相替代——回撤 20% 持续三年，
    比回撤 30% 六个月恢复更难受，但 MDD 看不出来这件事。
    外部经验值（趋势跟踪类）：平均恢复 8 个月、最长约 50 个月；
    标普 500 买入持有：平均 14 个月、最长 90 个月。

    返回字段
    --------
    max_days      : 最长一次水下持续的自然日天数（峰→恢复；未恢复算到最后一日）
    max_start     : 该次水下的起始峰值日
    max_end       : 恢复日；未恢复为空串
    max_drawdown  : 该次水下的最大回撤深度
    episodes      : 水下段数（每次跌破前高算一段）
    current_days  : 当前已连续水下天数（0 = 正处于新高）
    current_dd    : 当前相对前高的回撤（0 = 新高）
    recovery_gain : 回到前高还需要涨多少（0 = 已在新高）
    """
    out = {"max_days": 0, "max_start": "", "max_end": "", "max_drawdown": 0.0,
           "episodes": 0, "current_days": 0, "current_dd": 0.0, "recovery_gain": 0.0}
    if nav is None or len(nav) < 2:
        return out

    nav = nav.dropna()
    peak = nav.cummax()
    dd = nav / peak - 1.0
    under = dd < -1e-9

    episodes: list[tuple[int, pd.Timestamp, pd.Timestamp | None, float]] = []
    grp = (under != under.shift()).cumsum()
    for _, idx in under[under].groupby(grp[under]).groups.items():
        i0 = nav.index.get_loc(idx[0])
        i1 = nav.index.get_loc(idx[-1])
        # 峰值日 = 跌破前高的前一个交易日（序列开头就在水下的，从首日起算）
        start = nav.index[max(0, i0 - 1)]
        end = nav.index[i1 + 1] if i1 + 1 < len(nav) else None
        span_end = end if end is not None else nav.index[-1]
        episodes.append((int((span_end - start).days), start, end, float(dd.loc[idx].min())))

    out["episodes"] = len(episodes)
    if episodes:
        days, start, end, deepest = max(episodes, key=lambda e: e[0])
        out.update({"max_days": days, "max_start": str(start.date()),
                    "max_end": str(end.date()) if end is not None else "",
                    "max_drawdown": deepest})

    if bool(under.iloc[-1]):
        last_peak = peak.iloc[-1]
        i_last = len(nav) - 1
        i0 = i_last
        while i0 > 0 and bool(under.iloc[i0 - 1]):
            i0 -= 1
        start = nav.index[max(0, i0 - 1)]
        out["current_days"] = int((nav.index[-1] - start).days)
        out["current_dd"] = float(dd.iloc[-1])
        out["recovery_gain"] = float(last_peak / nav.iloc[-1] - 1.0)
    return out


def win_rate(nav: pd.Series, freq: str = "D") -> float:
    r = to_returns(nav)
    if r.empty:
        return np.nan
    if freq == "M":
        r = (1 + r).resample("ME").prod() - 1
    return float((r > 0).mean())


def annual_returns(nav: pd.Series) -> pd.Series:
    r = to_returns(nav)
    if r.empty:
        return pd.Series(dtype=float)
    return (1 + r).resample("YE").prod() - 1


def monthly_return_table(nav: pd.Series) -> pd.DataFrame:
    r = to_returns(nav)
    if r.empty:
        return pd.DataFrame()
    m = (1 + r).resample("ME").prod() - 1
    df = pd.DataFrame({"year": m.index.year, "month": m.index.month, "ret": m.values})
    return df.pivot(index="year", columns="month", values="ret")


def summarize(nav: pd.Series, bench: pd.Series | None = None, rf: float = DEFAULT_RF) -> pd.Series:
    """生成一行核心指标。"""
    mdd, dur, _, _ = max_dd_detail(nav)
    out = {
        "累计收益": total_return(nav),
        "年化收益(CAGR)": cagr(nav),
        "年化波动": ann_vol(nav),
        "夏普比率": sharpe(nav, rf),
        "索提诺比率": sortino(nav, rf),
        "最大回撤": mdd,
        "最大回撤持续(交易日)": dur,
        "卡玛比率": calmar(nav),
        "日胜率": win_rate(nav, "D"),
        "月胜率": win_rate(nav, "M"),
    }
    if bench is not None and len(bench) > 2:
        b = bench.reindex(nav.index).ffill()
        out["基准累计收益"] = total_return(b)
        out["超额收益"] = total_return(nav) - total_return(b)
        br = to_returns(b)
        ar = to_returns(nav)
        n = min(len(ar), len(br))
        if n > 30:
            cov = np.cov(ar.iloc[-n:], br.iloc[-n:])
            beta = cov[0, 1] / cov[1, 1] if cov[1, 1] > 0 else np.nan
            out["Beta"] = beta
            out["Alpha(年化)"] = cagr(nav) - (
                rf + (beta or 0) * (cagr(b) - rf)
            ) if np.isfinite(beta) else np.nan
            out["信息比率"] = (
                (ar.iloc[-n:] - br.iloc[-n:]).mean() / (ar.iloc[-n:] - br.iloc[-n:]).std()
                * np.sqrt(TRADING_DAYS)
                if (ar.iloc[-n:] - br.iloc[-n:]).std() > 0
                else np.nan
            )
    return pd.Series(out)
