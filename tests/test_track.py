"""
实盘跟踪的归档与纸上净值测试。

三条必须守住的行为：
1. 同日重复生成要**整体替换**，不能叠加出两份；
2. backfill 不得覆盖同日期的 live —— 否则"当时的建议"被事后重算悄悄改掉；
3. 目标权重没变时只算一次调仓，否则每天点一次生成信号 = 每天再平衡，
   凭空多出一笔策略里并不存在的交易成本。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etfquant import track as T
from etfquant.strategy import Panel


def _rows(sig_date: str, weights: dict[str, float], exec_date: str = "2026-01-02") -> list[dict]:
    return [{
        "signal_date": sig_date, "exec_date": exec_date, "code": c, "name": c.upper(),
        "category": "行业", "weight": w, "ref_price": 1.0,
    } for c, w in weights.items()]


# --------------------------------------------------------------------------- #
# 写入与覆盖
# --------------------------------------------------------------------------- #
def test_upsert_replaces_same_date(tmp_path):
    fp = tmp_path / "a.csv"
    T.upsert_signal(fp, _rows("2026-01-05", {"sh1": 0.5, "sh2": 0.5}), "live")
    T.upsert_signal(fp, _rows("2026-01-05", {"sh1": 1.0}), "live")
    df = T.load_archive(fp)
    assert len(df) == 1 and df.iloc[0]["code"] == "sh1"


def test_backfill_does_not_overwrite_live(tmp_path):
    fp = tmp_path / "a.csv"
    T.upsert_signal(fp, _rows("2026-01-05", {"sh1": 1.0}), "live")
    n = T.upsert_signal(fp, _rows("2026-01-05", {"sh9": 1.0}), "backfill")
    df = T.load_archive(fp)
    assert n == 0
    assert df.iloc[0]["code"] == "sh1" and df.iloc[0]["source"] == "live"


def test_backfill_overwrites_backfill(tmp_path):
    fp = tmp_path / "a.csv"
    T.upsert_signal(fp, _rows("2026-01-05", {"sh1": 1.0}), "backfill")
    T.upsert_signal(fp, _rows("2026-01-05", {"sh2": 1.0}), "backfill")
    df = T.load_archive(fp)
    assert len(df) == 1 and df.iloc[0]["code"] == "sh2"


def test_load_missing_returns_empty(tmp_path):
    df = T.load_archive(tmp_path / "nope.csv")
    assert df.empty and list(df.columns) == T.COLUMNS


# --------------------------------------------------------------------------- #
# 目标权重折叠
# --------------------------------------------------------------------------- #
def test_archive_targets_collapses_unchanged(tmp_path):
    fp = tmp_path / "a.csv"
    T.upsert_signal(fp, _rows("2026-01-05", {"sh1": 0.5, "sh2": 0.5}), "live")
    # 次日权重完全一样（用户又点了一次生成信号）
    T.upsert_signal(fp, _rows("2026-01-06", {"sh1": 0.5, "sh2": 0.5}), "live")
    T.upsert_signal(fp, _rows("2026-01-07", {"sh1": 1.0}), "live")
    tg = T.archive_targets(T.load_archive(fp))
    assert [str(d.date()) for d in tg] == ["2026-01-05", "2026-01-07"]


def test_archive_targets_can_disable_collapse(tmp_path):
    fp = tmp_path / "a.csv"
    T.upsert_signal(fp, _rows("2026-01-05", {"sh1": 1.0}), "live")
    T.upsert_signal(fp, _rows("2026-01-06", {"sh1": 1.0}), "live")
    tg = T.archive_targets(T.load_archive(fp), collapse_unchanged=False)
    assert len(tg) == 2


# --------------------------------------------------------------------------- #
# 纸上净值
# --------------------------------------------------------------------------- #
def _panel(n: int = 40) -> Panel:
    idx = pd.bdate_range("2026-01-01", periods=n)
    rng = np.random.default_rng(7)
    px = pd.DataFrame({
        "sh1": 10 * np.exp(np.cumsum(rng.normal(0.002, 0.01, n))),
        "sh2": 10 * np.exp(np.cumsum(rng.normal(0.001, 0.01, n))),
    }, index=idx)
    vol = pd.DataFrame({"sh1": np.full(n, 5e5), "sh2": np.full(n, 5e5)}, index=idx)
    meta = pd.DataFrame({"name": ["一", "二"], "category": ["行业", "行业"]}, index=["sh1", "sh2"])
    return Panel(close=px, open_=px.copy(), high=px.copy(), low=px.copy(), volume=vol, meta=meta)


def test_build_track_empty_archive():
    res = T.build_track(_panel(), {}, T.load_archive(Path("/nonexistent/x.csv")))
    assert res["count"] == 0 and res["periods"] == []


def test_build_track_shapes(tmp_path):
    fp = tmp_path / "a.csv"
    T.upsert_signal(fp, _rows("2026-01-05", {"sh1": 1.0}), "live")
    T.upsert_signal(fp, _rows("2026-01-09", {"sh2": 1.0}), "live")
    arch = T.load_archive(fp)
    panel = _panel()
    tg = T.archive_targets(arch)
    res = T.build_track(panel, tg, arch)

    assert res["count"] == 2
    assert len(res["periods"]) == 2
    assert res["series"] and all({"date", "paper", "backtest"} <= set(x) for x in res["series"])
    # 归档与"回测"用同一份目标时，两条曲线必须完全一致
    assert res["paper"]["total"] == pytest.approx(res["backtest"]["total"], abs=1e-9)
    assert res["periods"][0]["exec_date"] > res["periods"][0]["signal_date"]


def test_build_track_marks_sources(tmp_path):
    fp = tmp_path / "a.csv"
    T.upsert_signal(fp, _rows("2026-01-05", {"sh1": 1.0}), "backfill")
    T.upsert_signal(fp, _rows("2026-01-09", {"sh2": 1.0}), "live")
    arch = T.load_archive(fp)
    res = T.build_track(_panel(), T.archive_targets(arch), arch)
    sources = [p["source"] for p in res["periods"]]
    assert sources == ["backfill", "live"]
    assert res["live_count"] == 1 and res["backfill_count"] == 1


# --------------------------------------------------------------------------- #
# 期间区间：最后一期不能拿数据起点当期末
# --------------------------------------------------------------------------- #
def test_period_spans_last_period_ends_at_last_bar():
    """回归：原来写 `b = idx`，b[0] 取到 2018-01-02，最后一期收益变成 −8.8% 的假数字。"""
    from etfquant.track import period_spans

    idx = pd.bdate_range("2026-01-05", periods=12)
    dates = [idx[0], idx[5]]

    spans = period_spans(idx, dates)
    assert spans[0] == (idx[1], idx[6]), "第一期期末 = 下一期买入日"
    assert spans[1] == (idx[6], idx[-1]), "最后一期期末必须是数据最后一天"


def test_period_spans_chains_without_gap():
    from etfquant.track import period_spans

    idx = pd.bdate_range("2026-01-05", periods=20)
    dates = [idx[i] for i in (0, 5, 10, 15)]
    spans = period_spans(idx, dates)
    for (_, prev_end), (next_start, _) in zip(spans, spans[1:]):
        assert prev_end == next_start, "期末必须等于下一期买入日，否则收益会漏算一天"


def test_period_spans_pending_when_no_bar_after_signal():
    from etfquant.track import period_spans

    idx = pd.bdate_range("2026-01-05", periods=5)
    spans = period_spans(idx, [idx[-1]])
    assert spans == [(None, None)], "信号日就是最后一根时，还没有买入日，应给占位"
