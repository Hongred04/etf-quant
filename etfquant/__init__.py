"""
ETF 量化选股工具包（etf-quant）。

模块分三层，改动前先确认自己动的是哪一层（完整地图见 docs/architecture.md §1.5，
`python -m etfquant audit` 的 L 节会校验依赖方向与地图完整性）：

生产（server / 日常脚本调用，改错会让界面和刷新当天不可用）
    datafeed  universe  pipeline  factors  strategy  backtest  metrics  report
    holdings  track  expectation  secondary  rebalance  io_utils  cli

研究（只有 scripts/research/ 调用，结论已写进 README 第七章，可放心重写）
    placebo  chan  dual_mom  resid_mom  macross  meta  shares  alpha_track

工具（按需运行的验证与报告）
    diagnostics  overfit  monthly
"""

from __future__ import annotations

__version__ = "0.1.0"

from .datafeed import CacheConfig, download_panel, fetch_kline, fetch_snapshot
from .universe import build_universe, classify_etf, discover_etfs, universe_summary
from .strategy import (
    STRATEGY_REGISTRY,
    DualMomentum,
    MomentumBaseline,
    MultiFactorRotation,
    Panel,
    TrendGatedAdaptive,
)
from .backtest import Backtester, BacktestResult
from . import metrics

__all__ = [
    "CacheConfig",
    "download_panel",
    "fetch_kline",
    "fetch_snapshot",
    "build_universe",
    "classify_etf",
    "discover_etfs",
    "universe_summary",
    "STRATEGY_REGISTRY",
    "MomentumBaseline",
    "DualMomentum",
    "MultiFactorRotation",
    "TrendGatedAdaptive",
    "Panel",
    "Backtester",
    "BacktestResult",
    "metrics",
]
