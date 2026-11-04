"""今日盈亏接口的口径与降级。

为什么单开一层
--------------
"今天赚了多少"是唯一一个**每分钟都会变**的数字，也是最容易被写错的地方：
- 用目标权重 vs 用真实持仓，两者在手数取整、部分成交后会差几十块；
- 缓存时间、盘中/收盘文案、停牌导致的权重缺口，都会让用户以为程序算错了。

这里只测三件确定的事：加权算法、有/无真实持仓的两条路径、缺行情时的不骗人。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server as S  # noqa: E402


def _fake_signal(rows):
    return lambda: {"rows": rows}


def test_signal_weights_are_used_when_no_holdings(monkeypatch):
    """没有真实持仓时，按清单目标权重加权；结果必须等于逐只加权之和。"""
    monkeypatch.setattr(S, "api_holdings", lambda: {"items": []})
    monkeypatch.setattr(S, "api_signal", _fake_signal([
        {"code": "a", "name": "A", "share": 0.5},
        {"code": "b", "name": "B", "share": 0.5},
    ]))
    monkeypatch.setattr(S, "_live_quotes", lambda codes: {
        "a": {"price": 1.10, "prev": 1.00, "date": "2026-11-04"},
        "b": {"price": 0.95, "prev": 1.00, "date": "2026-11-04"},
    })

    d = S.api_today()
    assert d["source"] == "signal"
    assert d["amount"] is None              # 估算口径不给金额，由前端按资金折算
    # (0.10 * 0.5) + (-0.05 * 0.5) = 0.025 → 2.5%
    assert d["pct"] == pytest.approx(2.5, abs=1e-6)
    assert d["covered"] == pytest.approx(1.0, abs=1e-6)
    assert d["partial"] is False
    assert [i["pct"] for i in d["items"]] == pytest.approx([10.0, -5.0], abs=1e-6)


def test_missing_quote_lowers_coverage_instead_of_faking_zero(monkeypatch):
    """停牌/无昨收的标的不能当 0% 混进去：覆盖率必须掉下来，让前端能提示。"""
    monkeypatch.setattr(S, "api_holdings", lambda: {"items": []})
    monkeypatch.setattr(S, "api_signal", _fake_signal([
        {"code": "a", "name": "A", "share": 0.5},
        {"code": "b", "name": "B", "share": 0.5},
    ]))
    monkeypatch.setattr(S, "_live_quotes", lambda codes: {
        "a": {"price": 1.02, "prev": 1.00, "date": "2026-11-04"},
    })

    d = S.api_today()
    assert d["covered"] == pytest.approx(0.5, abs=1e-6)
    assert d["partial"] is True
    assert d["pct"] == pytest.approx(1.0, abs=1e-6)   # 只有 A 的 2% × 50%
    missing = [i for i in d["items"] if i["missing"]]
    assert len(missing) == 1 and missing[0]["code"] == "b"


def test_real_holdings_win_over_the_list(monkeypatch):
    """有真实持仓时用账户金额，不能用清单权重估——那会把已实现盈亏算漏。"""
    monkeypatch.setattr(S, "api_holdings", lambda: {
        "items": [{"code": "a", "name": "A", "weight": 0.6, "day_pct": 1.2}],
        "day_pnl": 320.5,
        "day_pnl_pct": 0.0123,
        "updated_at": "2026-11-04 14:30",
        "missing_quotes": [],
    })
    d = S.api_today()
    assert d["source"] == "holdings"
    assert d["amount"] == pytest.approx(320.5, abs=1e-6)
    assert d["pct"] == pytest.approx(1.23, abs=1e-6)


def test_holdings_error_falls_back_to_the_list(monkeypatch):
    """/api/holdings 崩了也要能给出今日盈亏，不能整块消失。"""
    def boom():
        raise RuntimeError("持仓文件损坏")
    monkeypatch.setattr(S, "api_holdings", boom)
    monkeypatch.setattr(S, "api_signal", _fake_signal([
        {"code": "a", "name": "A", "share": 1.0},
    ]))
    monkeypatch.setattr(S, "_live_quotes", lambda codes: {
        "a": {"price": 1.01, "prev": 1.00, "date": "2026-11-04"},
    })
    d = S.api_today()
    assert d["source"] == "signal"
    assert d["pct"] == pytest.approx(1.0, abs=1e-6)
