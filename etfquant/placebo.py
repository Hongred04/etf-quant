"""
随机对照（placebo）：把"选股"换成随机抽签，其余全部不变，看真实策略排在第几分位。

为什么这不是又一个"回测变体"
----------------------------
bootstrap 给的是"同一个策略换一段历史会怎样"；这里给的是**"如果根本没有选股能力"
会怎样**——两者是不同的零假设。前者回答"这个数字稳不稳"，后者回答"这个数字是不是
只要满仓乱买就能拿到"。后者才是外部可验证的部分：任何人都能用同一份数据、
同一个闸门、同样的成本，跑出这条零分布。

口径
----
* 同样的候选集（`liquid_at` 过滤后的可交易池）、同样的调仓日、同样的**权益敞口路径**
  （直接取实盘权重里的非债券部分），同样的成本率；
* 差别只有一个：3 只是因为 R20 排出来，还是随机抽的；
* 抽签**不允许重复**，且必须落在当日候选集内——否则会抽到停牌/未上市标的，
  那是把"不可交易"当成"随机"，等于人为抬高零分布。

成本口径必须分开报
------------------
随机组合每期全部换手（卖 3 买 3），年换手约 150 倍；真实策略有缓冲带，只有约 25 倍。
若只报净值，随机组会被自己的换手费打死，看起来"策略远胜随机"——那是成本差异，
不是选股能力。所以**同时给毛收益（不扣成本）与净收益（各扣各的）**。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def pick_random(candidates: list[str], top: int, rng: np.random.Generator) -> list[str]:
    """从当日候选集里无放回抽 `top` 只。候选不足时返回全部（不补别的日的标的）。"""
    if len(candidates) <= top:
        return list(candidates)
    idx = rng.choice(len(candidates), size=top, replace=False)
    return [candidates[i] for i in idx]


def random_leg_returns(
    ret: pd.DataFrame,
    candidates: dict[pd.Timestamp, list[str]],
    top: int = 3,
    n_draws: int = 300,
    seed: int = 0,
) -> pd.DataFrame:
    """
    抽 `n_draws` 组随机组合，返回"每日权益腿收益"矩阵（index=交易日, columns=draw）。

    实现要点：
    * 每个调仓日的选股一直持有到**下一个调仓日**（与策略的持仓期一致）；
    * 只在调仓日重新抽签；调仓日之间的收益按等权计算；
    * 全部用 numpy 数组，避免 376 × 300 次 pandas 切片。
    """
    cols = list(ret.columns)
    pos = {c: i for i, c in enumerate(cols)}
    arr = ret.to_numpy(dtype=float)          # (days, tickers)
    dates = ret.index
    reb = sorted(d for d in candidates if d in dates)
    if len(reb) < 2:
        return pd.DataFrame(index=dates)

    rng = np.random.default_rng(seed)
    out = np.full((len(dates), n_draws), np.nan)
    dpos = {d: i for i, d in enumerate(dates)}

    for k, d in enumerate(reb):
        start = dpos[d]
        end = dpos[reb[k + 1]] if k + 1 < len(reb) else len(dates)
        cand = candidates[d]
        if not cand:
            continue
        idx_pool = np.array([pos[c] for c in cand if c in pos], dtype=int)
        if idx_pool.size < top:
            continue
        # 抽签：shape (n_draws, top)
        picks = np.argsort(rng.random((n_draws, idx_pool.size)), axis=1)[:, :top]
        picked = idx_pool[picks]                      # (n_draws, top)
        seg = arr[start:end][:, picked]               # (days, n_draws, top)
        out[start:end] = np.nanmean(seg, axis=2)
    return pd.DataFrame(out, index=dates)


def cagr_of(daily: pd.Series, min_days: int = 120) -> float:
    """把日收益序列折成年化（复利）。不足 `min_days` 返回 NaN。"""
    r = pd.Series(daily).dropna()
    if len(r) < min_days:
        return float("nan")
    nav = float(np.prod(1.0 + r.to_numpy()))
    years = len(r) / 252.0
    return nav ** (1.0 / years) - 1.0 if nav > 0 else float("nan")


def percentile_of(value: float, sample: np.ndarray) -> float:
    """`value` 在 `sample` 中的分位（0~100）。样本为空返回 NaN。"""
    s = np.asarray(sample, dtype=float)
    s = s[np.isfinite(s)]
    if s.size == 0 or not np.isfinite(value):
        return float("nan")
    return float((s < value).mean() * 100.0)
