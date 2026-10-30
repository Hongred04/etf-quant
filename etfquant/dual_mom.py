"""
「普通动量 + 改进残差动量」双因子行业轮动——按外部描述原样复现，用于对照。

为什么单独成模块
----------------
外面流传的这类策略（12 个月行业动量、月度调仓、Top3 等权）描述里往往混着
一两处"手工补丁"（这里是"把日波动最高月份的收益从加分改为扣分"）。补丁长什么样、
影响多大，靠读文字判断不了，只能实现出来跑。所以：

* 每条规则都按原文的实现，**不做"我觉得应该"的修正**；
* 补丁单独做成开关（`flip_anomaly`），这样能看出它到底是 alpha 还是拟合；
* 全部是纯函数（吃 DataFrame、吐 DataFrame），不读文件——便于单测。

口径
----
* 信号时点 = 每月**最后一个交易日收盘**，次一交易日开盘成交（与主回测一致）；
* 分数只用 ≤ 信号日的数据，无前视；
* 排名并列时按原始动量降序、再按代码升序，保证同一份数据两次运行结果一致。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def month_end_dates(index: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """每个自然月的最后一个**交易日**（不是日历月末）。"""
    s = pd.Series(index, index=index)
    return [pd.Timestamp(v) for v in s.groupby(s.index.to_period("M")).max()]


def monthly_close(px: pd.DataFrame) -> pd.DataFrame:
    """日频收盘 → 月末收盘（每个月最后一个交易日的收盘价）。"""
    return px.resample("ME").last()


def momentum(mclose: pd.DataFrame, lookback: int = 12) -> pd.DataFrame:
    """普通动量：过去 `lookback` 个月的对数收益之和 = log(P_t / P_{t-lookback})。"""
    return np.log(mclose).diff(lookback)


def residual_matrix(
    mret: pd.DataFrame, factors: pd.DataFrame, lookback: int = 12
) -> pd.DataFrame:
    """
    滚动回归取残差：`r_i = a + b1*大盘 + b2*债券 + b3*黄金 + e_i`。

    残差 e_i 就是"扣掉大盘顺风、利率环境和避险情绪之后，行业自己的涨幅"。
    每月用**过去 `lookback` 个月**做一次回归（原文口径），残差逐月留在矩阵里，
    后面既可以求和（残差动量），也可以单独取某一个月（异常月修正需要）。
    """
    cols = sorted(mret.columns)
    out = pd.DataFrame(np.nan, index=mret.index, columns=cols)
    f = factors.reindex(mret.index)
    for i in range(len(mret)):
        if i < lookback - 1:
            continue
        y = mret.iloc[i - lookback + 1:i + 1]
        x = f.iloc[i - lookback + 1:i + 1]
        for c in cols:
            yy = y[c].to_numpy(dtype=float)
            mask = np.isfinite(yy) & np.isfinite(x).all(axis=1).to_numpy()
            # 必须凑满窗口：缺一个月就少一个观测，4 个参数下残差会明显偏大，
            # 而且会让第一个"有效"月份提前，样本起点悄悄提前一整年。
            if mask.sum() < lookback:
                continue
            X = np.column_stack([np.ones(mask.sum()), x.to_numpy(dtype=float)[mask]])
            try:
                beta, *_ = np.linalg.lstsq(X, yy[mask], rcond=None)
            except np.linalg.LinAlgError:
                continue
            out.iloc[i, out.columns.get_loc(c)] = yy[mask][-1] - X[-1] @ beta
    return out


def monthly_vol(px: pd.DataFrame) -> pd.DataFrame:
    """每个月的日收益标准差（用于找"日波动最高的异常月份"）。"""
    r = px.pct_change(fill_method=None)
    return r.resample("ME").std()


def residual_score(
    resid: pd.DataFrame,
    dvol: pd.DataFrame,
    lookback: int = 12,
    flip_anomaly: bool = True,
) -> pd.DataFrame:
    """
    残差动量得分 = 过去 `lookback` 个月残差之和。

    `flip_anomaly=True` 时复现原文的补丁：窗口内**日波动最高**的那个月，
    其残差从「加分」改为「扣分」（等价于总和减去 2 倍该月残差）。

    这个补丁值得单独跑一遍：一条规则如果只为一个现象服务、且没有机制解释，
    它多半是在拟合某段历史里的单次暴涨/暴跌。
    """
    cols = resid.columns
    out = pd.DataFrame(np.nan, index=resid.index, columns=cols)
    vol = dvol.reindex(resid.index)
    for i in range(len(resid)):
        if i < lookback - 1:
            continue
        win = resid.iloc[i - lookback + 1:i + 1]
        vwin = vol.iloc[i - lookback + 1:i + 1]
        for c in cols:
            w = win[c]
            # 同样必须满窗：只判断"不全为空"会让只有一个月数据的窗口也算出得分，
            # 于是残差动量在 warmup 期就"有值了"，样本区间凭空提前一年。
            if w.notna().sum() < lookback:
                continue
            total = float(w.sum())
            if flip_anomaly:
                v = vwin[c]
                if v.notna().any():
                    worst = v.idxmax()
                    x = w.get(worst, np.nan)
                    if pd.notna(x):
                        total -= 2 * float(x)
            out.iloc[i, out.columns.get_loc(c)] = total
    return out


def rank_scores(score: pd.DataFrame) -> pd.DataFrame:
    """
    横截面排名（1 = 最强）。

    列先按代码排序再排名：`method="first"` 遇到并列时按列顺序决定先后，
    如果直接吃面板给的列顺序，同一份数据在不同调用下可能选出不同标的。
    """
    s = score.reindex(columns=sorted(score.columns))
    return s.rank(axis=1, ascending=False, method="first")


def combine(rank_a: pd.DataFrame, rank_b: pd.DataFrame, w_a: float = 0.5) -> pd.DataFrame:
    """两个因子的排名加权合成；任一缺失则该行不参与。"""
    b = rank_b.reindex(index=rank_a.index, columns=rank_a.columns)
    return w_a * rank_a + (1 - w_a) * b


def pick_top(
    combined: pd.DataFrame, n: int = 3, weights: dict[str, float] | None = None
) -> dict[pd.Timestamp, dict[str, float]]:
    """
    每个调仓日取总分最低（排名最靠前）的 `n` 只，等权。

    返回 `{信号日: {代码: 权重}}`，可直接喂给 Backtester。
    """
    out: dict[pd.Timestamp, dict[str, float]] = {}
    for d, row in combined.iterrows():
        r = row.dropna()
        if len(r) < n:
            continue
        picked = sorted(r.items(), key=lambda kv: (kv[1], kv[0]))[:n]
        w = 1.0 / n
        out[pd.Timestamp(d)] = {c: w for c, _ in picked}
    return out
