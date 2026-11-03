"""核心-卫星组合：红利低波核心 + 动量卫星。

为什么需要它
------------
单靠 S4（动量轮动）的 Calmar 只有 0.36：CAGR 12.4%、最大回撤 −35.2%。
2026-11-03 的归因与降回撤实验证明，在动量框架内**没有任何机制能改善这个
比值**——降敞口（Calmar 不变）、回撤熔断、个股止损、波动目标、熊市空仓、
权重漂移、分散持仓，全部无效或更差。

唯一有效的是加入一条**低相关**的收益源。池内 211 只宽基/主题/行业 ETF
彼此相关性 0.96~0.98，加进去等于没加；红利低波与 S4 的相关性是 **0.23~0.27**，
这是真正不同步的一条腿。

实测（2021-06-02 起，日频，含成本前）
--------------------------------------
| 配置 | CAGR | MaxDD | Sharpe | Calmar |
|---|---|---|---|---|
| S4 100%          | 13.1% | −35.1% | 0.54 | 0.37 |
| 红利低波 100%     | 12.6% | −16.9% | 0.74 | 0.75 |
| S4 40% + 红利 60% | 13.9% | −20.3% | 0.79 | 0.68 |

78 个滚动 12 个月窗口里，组合的 Calmar 有 88% 的窗口优于纯 S4。

为什么不是"用收益换波动"
------------------------
固定降敞口到 0.5 的代价是 CAGR 12.4% → 6.9%，Calmar 不变（0.37）。
而组合的收益**高于**纯 S4——因为两条低相关正收益流叠加时，波动按相关性
衰减、收益近似相加，复利效率提升。这是分散化的数学收益，不是择时。

风险与边界
----------
1. **这是风格暴露，不是 alpha**。红利低波在 2023-01~2024-09 贡献 +27.7%，
   2024-10~2026-11 只有 +6.6%。风格转冷时组合收益会低于纯 S4。
2. **样本**：A 股 4 只均自 2021-06 起有完整数据，港股 3 只更晚。
   `sh512890` 单只 2019 年起可用，但单只存在挑选偏差，本模块用等权篮子。
3. **核心腿不择时**：这是设计选择。给核心腿加趋势过滤会削弱它的分散作用
   （它在 S4 亏钱的时段赚钱，正是靠"始终在场"）。
"""
from __future__ import annotations

import pandas as pd

# A 股低波红利篮子：2021-06 起均有完整数据，无跨境溢价问题。
CORE_A = ["sh512890", "sh515450", "sh515300", "sz159307"]

# 扩展篮子：加入港股红利低波（收益更高，但有 QDII 溢价与额度风险）。
CORE_AH = CORE_A + ["sh513630", "sz159545", "sz159569"]

DEFAULT_CORE_WEIGHT = 0.5


def core_basket(kind: str = "a") -> list[str]:
    """返回核心腿标的列表。kind: 'a'（纯 A 股）或 'ah'（含港股）。"""
    if kind == "a":
        return list(CORE_A)
    if kind == "ah":
        return list(CORE_AH)
    raise ValueError(f"未知的核心篮子: {kind}")


def blend_targets(
    satellite: dict,
    core_weight: float = DEFAULT_CORE_WEIGHT,
    codes: list[str] | None = None,
) -> dict:
    """把卫星目标与核心等权篮子混合成一张目标权重表。

    参数
    ----
    satellite : {调仓日: {代码: 权重}}，来自任意策略（默认 S4）
    core_weight : 核心腿占比 ∈ [0,1]。0 等于不使用本模块。
    codes : 核心腿标的，默认 CORE_A。

    返回
    ----
    同结构的目标权重表，权重之和 <= 1，差额为现金。

    实现细节：核心腿在**每个调仓日都等权重置**，不做择时。这既是设计
    选择（保留它的分散作用），也让换手保持在很低的水位——核心腿之间的
    相对波动很小，再平衡的交易额远小于卫星腿。
    """
    if not 0.0 <= core_weight <= 1.0:
        raise ValueError("core_weight 必须在 [0, 1] 内")
    codes = list(codes or CORE_A)
    if core_weight == 0.0:
        return satellite

    per = core_weight / len(codes)
    out: dict = {}
    for dt, weights in satellite.items():
        blended = {}
        for c, w in weights.items():
            if w > 0:
                blended[c] = blended.get(c, 0.0) + w * (1.0 - core_weight)
        for c in codes:
            blended[c] = blended.get(c, 0.0) + per
        out[dt] = {k: v for k, v in blended.items() if v > 0}
    return out


def core_nav(close: pd.DataFrame, codes: list[str] | None = None) -> pd.Series:
    """核心腿的等权日频净值（用于报告与对照，不参与回测记账）。"""
    codes = [c for c in (codes or CORE_A) if c in close.columns]
    if not codes:
        return pd.Series(dtype=float)
    px = close[codes].ffill()
    r = px.pct_change().mean(axis=1, skipna=True).fillna(0.0)
    return (1.0 + r).cumprod()
