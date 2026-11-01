"""
慢变量仓位上限（试验 7.17，见 docs/prereg_7_17_slow_gate.md）。

为什么单独一个模块
------------------
主线 TGAR 的闸门是**快变量**（价格趋势 + 广度，日级）。本模块提供的是
**慢变量**（月度采样、5 年滚动分位），只用来给权益仓位设上限，不参与选股，
也不替代闸门。两者是相乘关系：慢变量定"最多能拿多少"，闸门定"现在拿多少"。

为什么用股债相对强弱而不是 ERP
------------------------------
ERP = 1/PE − 10Y 国债收益率，需要历史 PE 与收益率序列；本项目数据源
（腾讯行情）只给 OHLCV，拿不到。价格版近似：股债相对强弱的分位。
它测的是趋势而非估值——这个差别写进了 7.17 的结论，不要在引用时含糊。

分层：research（只被 scripts/research/ 调用）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

DEBT_CATEGORY = "债券"
FALLBACK_BOND = "sh511260"        # 十年国债ETF，腾出的敞口默认补到它
WINDOW = 1260                      # 5 年交易日


def equity_bond_rs(equity: pd.Series, bond: pd.Series) -> pd.Series:
    """股债相对强弱：ln(股净值) − ln(债净值)，逐日。"""
    e = pd.to_numeric(equity, errors="coerce").astype(float)
    b = pd.to_numeric(bond, errors="coerce").astype(float)
    df = pd.concat([e.rename("e"), b.rename("b")], axis=1).dropna()
    if df.empty:
        return pd.Series(dtype=float)
    return np.log(df["e"]) - np.log(df["b"])


def rolling_pct(s: pd.Series, window: int = WINDOW) -> pd.Series:
    """当日值在过去 window 个交易日内的百分位（含当日）。

    min_periods 固定为 window：预热不足时返回 NaN，调用方须当作"不约束"。
    不用更短的 min_periods——那会把 5 年分位偷偷变成"可用多长算多长"，
    与预注册定义不符。
    """
    if s.empty:
        return s
    return s.rolling(window, min_periods=window).rank(pct=True)


def monthly_effective(pct: pd.Series, dates: pd.DatetimeIndex) -> pd.Series:
    """把日频分位压成"每月末取值、次一工作日生效"的日频序列。

    月末收盘才出的分位，次一交易日才能用来下单——直接用 ffill 会在月末当天
    就用到当天收盘的分位，那是**当日信息**，属于前视。
    """
    if pct.empty:
        return pd.Series(1.0, index=dates)
    monthly = pct.resample("ME").last().dropna()
    if monthly.empty:
        return pd.Series(1.0, index=dates)
    eff = monthly.copy()
    eff.index = eff.index + pd.offsets.BDay(1)
    daily = eff.reindex(eff.index.union(dates)).sort_index().ffill().reindex(dates)
    return daily.fillna(1.0)          # 无有效值（预热期）→ 不约束


def cap_for_percentile(pct: float, low: float = 0.20, strict: bool = False) -> float:
    """分位 → 权益仓位上限。strict=True 时 10% 以下归零（V2）。"""
    if not np.isfinite(pct):
        return 1.0
    if pct < 0.10 and strict:
        return 0.0
    if pct < low:
        return 0.5
    return 1.0


def apply_cap(
    targets: dict[pd.Timestamp, dict[str, float]],
    caps: pd.Series,
    debt_cols: set[str],
    fallback_bond: str = FALLBACK_BOND,
) -> dict[pd.Timestamp, dict[str, float]]:
    """按上限缩放权益权重，腾出的敞口补到债券列（无债券则补 fallback）。

    只缩放不重新选股：顺序、只数、相对比例全部保持，避免与选股逻辑纠缠。
    """
    out: dict[pd.Timestamp, dict[str, float]] = {}
    for dt, w in targets.items():
        cap = float(caps.get(dt, 1.0))
        if not np.isfinite(cap) or cap >= 1.0 - 1e-12:
            out[dt] = dict(w)
            continue
        eq = {k: v for k, v in w.items() if k not in debt_cols and v > 1e-9}
        eq_sum = sum(eq.values())
        if eq_sum <= cap + 1e-12:
            out[dt] = dict(w)
            continue
        scale = cap / eq_sum
        new_w = {k: v * scale for k, v in eq.items()}
        freed = eq_sum - cap
        old_debt = {k: v for k, v in w.items() if k in debt_cols and v > 1e-9}
        debt_sum = sum(old_debt.values())
        if debt_sum > 1e-9:
            for k, v in old_debt.items():
                new_w[k] = v + freed * (v / debt_sum)
        else:
            new_w[fallback_bond] = new_w.get(fallback_bond, 0.0) + freed
        out[dt] = new_w
    return out
