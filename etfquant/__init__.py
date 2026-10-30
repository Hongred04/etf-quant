"""ETF 量化选股工具包（etf-quant）。"""

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
