"""
元标签（meta-labeling）：不改选股，只决定"这一期该下多重的手"。

思路
----
López de Prado 的两层结构：一级模型（这里是 R20 排序）负责**选什么**，
二级模型只负责**下多大**——预测"这一期的多头组合会不会赚"，再映射成仓位系数。
本项目的动机很直接：7.10 显示 alpha 的 t 值只有 1.38，而 2020-22 与 2023-26
两段形状完全不同；如果存在"什么时候不该满仓"的可预测性，它应该体现为
**同样的选股、更少的亏损**，而不是换一批标的。

三条自我约束（都是为了不重演 7.5 的 PBO 43%）
--------------------------------------------
1. **只用 3 个特征**，且全部是慢变量：regime 分数、基准 20 日波动、信号近 6 期胜率。
   特征越多，376 期样本越撑不住。
2. **走前向验证（walk-forward）**：每个决策日只用**当日之前**已实现的标签重训，
   绝不使用未来。截断未来不改变历史预测——有单测钉住。
3. **仓位只减不增**（默认映射）：最低 0.5、最高 1.0。这样最坏情况是"少赚"，
   而不是把杠杆引入一个本来就没验证过的信号。

没有 sklearn 也能跑：3 个特征、几百个样本，numpy 的 IRLS 就够了，且**完全确定性**
（同一份数据两次运行结果逐位相同），符合本项目的可复现要求。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------- #
# 标签与特征
# --------------------------------------------------------------------------- #
def top_basket_return(
    close: pd.DataFrame, score: pd.DataFrame, dates: pd.Index, top: int = 3, horizon: int = 5
) -> pd.Series:
    """
    每期"选出来的 top N 等权组合"在随后 `horizon` 个交易日的收益（close→close）。

    用 close→close 而不是 open→open：标签只回答"这段时间多头方向对不对"，
    执行延迟的成本由回测引擎单独计，两件事不混在一个数里（与 7.10 的 IC 口径一致）。
    """
    fwd = close.shift(-horizon) / close - 1.0
    out = {}
    for d in dates:
        row = score.loc[d].dropna() if d in score.index else pd.Series(dtype=float)
        if len(row) < 20:
            continue
        picked = row.nlargest(top).index
        r = fwd.loc[d].reindex(picked).dropna() if d in fwd.index else pd.Series(dtype=float)
        if len(r):
            out[d] = float(r.mean())
    return pd.Series(out, dtype=float)


def rolling_hit(labels: pd.Series, window: int = 6, shift: int = 1) -> pd.Series:
    """
    信号近 `window` 期的胜率。

    `shift=1` 是硬性要求：第 t 期决策时，第 t 期自己的标签还不知道（要等未来 5 天），
    不位移就是把答案提前告诉模型——这类泄漏不会报错，只会让回测好看。
    """
    return labels.astype(float).rolling(window, min_periods=max(2, window // 2)).mean().shift(shift)


def bench_vol(close: pd.DataFrame, ticker: str = "sh510300", window: int = 20) -> pd.Series:
    """基准的已实现波动（年化）。慢变量，不用参数搜索。"""
    if ticker not in close.columns:
        return pd.Series(dtype=float)
    r = close[ticker].pct_change(fill_method=None)
    return r.rolling(window, min_periods=window // 2).std() * np.sqrt(252)


def build_features(regime: pd.Series, vol: pd.Series, hit: pd.Series) -> pd.DataFrame:
    """三列特征，索引对齐到决策日。缺值整行丢弃（不填 0——填 0 会伪造"中性"）。"""
    x = pd.DataFrame({"regime": regime, "vol": vol, "hit": hit})
    return x.replace([np.inf, -np.inf], np.nan).dropna()


# --------------------------------------------------------------------------- #
# 小型逻辑回归（IRLS，无第三方依赖）
# --------------------------------------------------------------------------- #
def fit_logit(x: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 50) -> np.ndarray:
    """
    带 L2 的逻辑回归，牛顿法（IRLS）。返回 `[截距, 系数...]`。

    `l2=1.0` 不是调出来的：3 个特征、样本几百，正则强度在这个量级上只起"别爆炸"的作用；
    真正防过拟合的是特征数和走前向验证。
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    xb = np.column_stack([np.ones(len(x)), x])
    w = np.zeros(xb.shape[1])
    reg = l2 * np.r_[0.0, np.ones(w.size - 1)]
    for _ in range(iters):
        z = np.clip(xb @ w, -30, 30)
        p = 1.0 / (1.0 + np.exp(-z))
        grad = xb.T @ (y - p) - reg * w
        hess = xb.T @ (xb * (p * (1 - p))[:, None]) + l2 * np.diag(np.r_[0.0, np.ones(w.size - 1)])
        try:
            step = np.linalg.solve(hess, grad)
        except np.linalg.LinAlgError:
            break
        w = w + step
        if np.max(np.abs(step)) < 1e-8:
            break
    return w


def _predict(w: np.ndarray, x: np.ndarray) -> float:
    z = float(np.clip(np.r_[1.0, x] @ w, -30, 30))
    return 1.0 / (1.0 + np.exp(-z))


def walk_forward(x: pd.DataFrame, y: pd.Series, min_train: int = 100) -> pd.Series:
    """
    每个决策日**只用当日之前已实现的样本**重训一次，输出下一期的上涨概率。

    为什么是逐期重训而不是一次训练：一次训练等于用全样本（含未来）拟合，
    得到的是"事后看哪期该减仓"，那种数字没有前向意义。
    逐期重训在 376 期、3 个特征上是毫秒级，没有性能理由偷懒。
    """
    idx = x.index
    out = pd.Series(np.nan, index=idx, dtype=float)
    if len(idx) < min_train + 1:
        return out
    # 标准化参数只用训练集估计，避免把未来分布泄漏进来
    for i in range(min_train, len(idx)):
        xt = x.iloc[:i]
        yt = y.reindex(xt.index).dropna()
        xt = xt.loc[yt.index]
        if len(yt) < min_train or yt.nunique() < 2:
            continue
        mu, sd = xt.mean(), xt.std(ddof=0).replace(0.0, 1.0)
        xs = ((xt - mu) / sd).to_numpy(dtype=float)
        w = fit_logit(xs, yt.to_numpy(dtype=float))
        out.iloc[i] = _predict(w, ((x.iloc[i] - mu) / sd).to_numpy(dtype=float))
    return out


# --------------------------------------------------------------------------- #
# 概率 → 仓位系数
# --------------------------------------------------------------------------- #
def to_exposure(prob: pd.Series, mode: str = "threshold", default: float = 1.0) -> pd.Series:
    """
    把概率映射成仓位系数（0.5~1.0），**只减不增**。

    - `threshold`：p≥0.60 满仓、0.45≤p<0.60 七五折、p<0.45 半仓（默认）
    - `linear`   ：0.5 + (p−0.5)/0.5，截断到 [0.5, 1.0]
    - `downside` ：只有 p<0.45 才减到 0.5，其余满仓（最保守，只防急跌）

    三种映射是同一条结论的三种画法：如果只有某一种能赚钱，说明结论不稳。
    """
    p = prob.astype(float)
    if mode == "threshold":
        k = pd.Series(0.75, index=p.index, dtype=float)
        k[p >= 0.60] = 1.0
        k[p < 0.45] = 0.5
    elif mode == "linear":
        k = (0.5 + (p - 0.5) / 0.5).clip(0.5, 1.0)
    elif mode == "downside":
        k = pd.Series(1.0, index=p.index, dtype=float)
        k[p < 0.45] = 0.5
    else:
        raise ValueError(f"未知映射方式：{mode}")
    # 没有预测时（走前向的训练期）保持满仓。注意不能靠 fillna：
    # threshold 模式下 k 预填 0.75，NaN 的位置并不会留空，fillna 抓不到。
    k = k.mask(p.isna(), default)
    return k.clip(0.0, 1.0)
