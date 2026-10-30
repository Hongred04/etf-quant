"""
过拟合检验的纯函数：CSCV/PBO、Deflated Sharpe、滚动前向选择。

为什么单独成模块
----------------
这三件事都是"对**策略选择过程**的检验"，与具体策略无关：输入只是一张
`T×N` 的日收益矩阵（N 个候选变体），输出是几个统计量。放在 `etfquant/` 里
而不是脚本里，是为了能被单元测试覆盖——这类检验一旦算错，结论会悄悄地
从"没通过"变成"通过"，比策略本身出错更难发现。

参考
----
- Bailey, Borwein, López de Prado, Zhu (2014). *The Probability of Backtest Overfitting*.
  （CSCV：把收益序列切成 S 块，枚举一半做样本内、另一半做样本外）
- Bailey & López de Prado (2014). *The Deflated Sharpe Ratio*.
  （对"试了多少次"做多重检验校正后的 Sharpe 显著性）
"""
from __future__ import annotations

import math
from itertools import combinations
from statistics import NormalDist

import numpy as np
import pandas as pd

_EULER = 0.5772156649015329      # Euler–Mascheroni 常数，DSR 公式要用
_N = NormalDist()                # 标准正态：cdf / inv_cdf 都用它


def annualized_sharpe_from_sums(n, s1, s2, periods: int = 252) -> np.ndarray:
    """
    由（样本数, 和, 平方和）直接算年化 Sharpe。

    CSCV 要把不同区块拼起来算 Sharpe，若每次都重新拼数组会慢一个量级；
    均值和方差本来就能从这三个量精确恢复，所以按块预聚合。
    """
    n = np.asarray(n, dtype=float)
    mean = s1 / n
    var = np.maximum(s2 / n - mean**2, 0.0)
    sd = np.sqrt(var)
    with np.errstate(divide="ignore", invalid="ignore"):
        sr = np.where(sd > 0, mean / sd, 0.0)
    return sr * math.sqrt(periods)


def block_sums(returns: np.ndarray, n_blocks: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """把 `T×N` 收益按行切成 `n_blocks` 块，返回每块的 (个数, 和, 平方和)。"""
    t, n = returns.shape
    if t < n_blocks:
        raise ValueError(f"样本太短：{t} 行切不出 {n_blocks} 块")
    edges = np.linspace(0, t, n_blocks + 1).astype(int)
    n_list, s1_list, s2_list = [], [], []
    for i in range(n_blocks):
        blk = returns[edges[i] : edges[i + 1]]
        n_list.append(blk.shape[0])
        s1_list.append(blk.sum(axis=0))
        s2_list.append((blk**2).sum(axis=0))
    return np.array(n_list), np.array(s1_list), np.array(s2_list)


def cscv_pbo(returns: np.ndarray, n_blocks: int = 16, max_combos: int | None = None) -> dict:
    """
    组合对称交叉验证（CSCV）→ 回测过拟合概率 PBO。

    做法：把 `T` 天切成 `n_blocks` 块（必须偶数），枚举"哪一半当样本内"的全部组合；
    每个组合里挑**样本内 Sharpe 最高**的变体，看它在样本外的 Sharpe 排在第几。
    如果样本内挑出来的冠军在样本外经常掉到中位数以下，说明"挑冠军"这个动作本身
    是在拟合噪声，PBO 就高。

    返回 `pbo`（0~1，越低越好）+ 每个组合的 IS/OOS 排名分位（omega）与 logit。
    """
    returns = np.asarray(returns, dtype=float)
    if n_blocks % 2:
        raise ValueError("n_blocks 必须是偶数")
    t, n = returns.shape
    nblk, s1, s2 = block_sums(returns, n_blocks)

    half = n_blocks // 2
    combos = list(combinations(range(n_blocks), half))
    if max_combos and len(combos) > max_combos:
        rng = np.random.default_rng(42)
        pick = rng.choice(len(combos), size=max_combos, replace=False)
        combos = [combos[i] for i in sorted(pick)]
    sel = np.zeros((len(combos), n_blocks), dtype=bool)
    for row, idx in enumerate(combos):
        sel[row, list(idx)] = True

    # 样本内 = 选中的块，样本外 = 其余块；两者都能从块级统计量直接聚合
    is_n = sel @ nblk
    is_s1 = sel @ s1
    is_s2 = sel @ s2
    oos_n = nblk.sum() - is_n
    oos_s1 = s1.sum(axis=0) - is_s1
    oos_s2 = s2.sum(axis=0) - is_s2

    is_sr = annualized_sharpe_from_sums(is_n[:, None], is_s1, is_s2)
    oos_sr = annualized_sharpe_from_sums(oos_n[:, None], oos_s1, oos_s2)

    best = np.argmax(is_sr, axis=1)                      # 每个组合挑出的"样本内冠军"
    rows = np.arange(len(combos))
    oos_best = oos_sr[rows, best]
    # 冠军在样本外的分位：低于它的变体占比
    omega = (oos_sr < oos_best[:, None]).sum(axis=1) / (n - 1 if n > 1 else 1)
    omega = np.clip(omega, 1e-9, 1 - 1e-9)
    lam = np.log(omega / (1 - omega))

    return {
        "pbo": float((lam <= 0).mean()),
        "omega": omega,
        "lambda": lam,
        "is_sharpe": is_sr[rows, best],
        "oos_sharpe": oos_best,
        "chosen": best,
        "n_combos": len(combos),
    }


def deflated_sharpe(returns: np.ndarray, n_trials: int, sr_variance: float | None = None) -> dict:
    """
    Deflated Sharpe Ratio：把"试了 N 次挑最好"这件事算进显著性里。

    普通 t 检验问的是"这个 Sharpe 是否为 0"，而这里问的是"在试了 N 个变体、
    取表现最好的那个"之后，它还有多大概率是真本事而不是选出来的运气"。
    输出 `dsr`（0~1）与 `sr0`（多重检验下的临界 Sharpe，年化后便于读）。

    `sr_variance` 传各变体**日频** Sharpe 的方差；不传则用 0（退化为普通 SR 检验）。
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    t = r.size
    if t < 30 or n_trials < 1:
        return {"dsr": float("nan"), "sr0": float("nan"), "sr": float("nan")}
    sd = r.std(ddof=1)
    sr = float(r.mean() / sd) if sd > 0 else 0.0          # 日频 Sharpe

    sr_var = float(sr_variance or 0.0)
    if n_trials > 1 and sr_var > 0:
        z1 = _N.inv_cdf(1 - 1.0 / n_trials)
        z2 = _N.inv_cdf(1 - 1.0 / (n_trials * math.e))
        sr0 = math.sqrt(sr_var) * ((1 - _EULER) * z1 + _EULER * z2)
    else:
        sr0 = 0.0

    skew = float(((r - r.mean()) ** 3).mean() / sd**3) if sd > 0 else 0.0
    kurt = float(((r - r.mean()) ** 4).mean() / sd**4) if sd > 0 else 3.0
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr**2
    denom = max(denom, 1e-9)
    z = (sr - sr0) * math.sqrt(t - 1) / math.sqrt(denom)
    return {
        "dsr": float(_N.cdf(z)),
        "sr0": sr0 * math.sqrt(252),      # 年化，便于与报告的 Sharpe 对照
        "sr": sr * math.sqrt(252),
        "n_trials": n_trials,
        "t": t,
    }


def walk_forward_select(
    returns: pd.DataFrame,
    is_days: int = 504,
    oos_days: int = 126,
    periods: int = 252,
) -> pd.DataFrame:
    """
    滚动前向选择：只用**窗口内**数据挑变体，再拿下一段做样本外。

    这是"参数选择过程"的实盘模拟：每 6 个月回头看一眼过去 2 年谁最好，然后
    照着它跑 6 个月。若这套流程的样本外收益显著低于"事后最优"，说明选参数
    这件事本身没有持续性；若还不如固定默认值，那就更该少调参数。
    """
    if returns.empty:
        return pd.DataFrame()
    cols = list(returns.columns)
    arr = returns.to_numpy()
    idx = returns.index
    out = []
    start = 0
    while start + is_days + oos_days <= len(arr):
        is_seg = arr[start : start + is_days]
        oos_seg = arr[start + is_days : start + is_days + oos_days]
        is_sr = np.array([_sr_simple(is_seg[:, j], periods) for j in range(len(cols))])
        pick = int(np.argmax(is_sr))
        out.append({
            "is_start": idx[start].date().isoformat(),
            "is_end": idx[start + is_days - 1].date().isoformat(),
            "oos_start": idx[start + is_days].date().isoformat(),
            "oos_end": idx[start + is_days + oos_days - 1].date().isoformat(),
            "picked": cols[pick],
            "is_sharpe": float(is_sr[pick]),
            "oos_sharpe": float(_sr_simple(oos_seg[:, pick], periods)),
            "oos_ret": float(np.prod(1 + oos_seg[:, pick]) - 1),
            "hindsight": cols[int(np.argmax([_sr_simple(oos_seg[:, j], periods) for j in range(len(cols))]))],
        })
        start += oos_days
    return pd.DataFrame(out)


def _sr_simple(r: np.ndarray, periods: int = 252) -> float:
    sd = float(np.std(r, ddof=1)) if r.size > 1 else 0.0
    return float(np.mean(r) / sd * math.sqrt(periods)) if sd > 0 else 0.0
