"""风险预算：把目标权重整体缩放到指定比例，剩余留现金。

为什么单独成一个模块
--------------------
"把回撤压到 20% 以下"在本策略里只有一条路：降低风险资产暴露。
固定缩放 k 会让 CAGR 与 MDD 近似同比例下降（Calmar 基本不变），
所以它是一个**风险预算选择**，不是模型改进。把这件事显式化，
避免有人以为换个 overlay 就能免费降回撤。

默认 1.0（完全按信号执行）。设置环境变量 `ETF_RISK_SCALE=0.5`
或在脚本里传 `--risk-scale 0.5` 才生效。
"""
from __future__ import annotations

import os

DEFAULT_RISK_SCALE = 1.0


def resolve_scale(cli: float | None = None) -> float:
    """优先级：命令行 > 环境变量 > 默认。结果裁剪到 [0.1, 1.0]。"""
    raw = cli if cli is not None else os.environ.get("ETF_RISK_SCALE")
    try:
        k = float(raw) if raw is not None else DEFAULT_RISK_SCALE
    except (TypeError, ValueError):
        k = DEFAULT_RISK_SCALE
    return max(0.1, min(1.0, k))


def scale_targets(targets: dict, k: float) -> dict:
    """把每期目标权重乘以 k；k=1 时返回原对象（不产生副本）。"""
    if k >= 1.0:
        return targets
    return {d: {c: w * k for c, w in ws.items()} for d, ws in targets.items()}
