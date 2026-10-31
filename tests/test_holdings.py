"""
实盘持仓模块单元测试。

重点验证四件事：
1. 录入校验拦住明显的输入错误（份额 0、成本负数、日期越界、代码格式）；
2. 估值口径与券商 App 一致（浮动盈亏 = 市值 − 成本额，今日盈亏用昨收）；
3. 缺行情时不静默按成本价顶替，而是单独列出、不计入合计；
4. 调仓建议的边界（差额小于阈值不动、不在信号里的一律清仓）。

运行：python -m pytest tests -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etfquant import holdings as H


def _item(**kw):
    base = {"code": "sh511090", "name": "30年国债ETF", "shares": 1000, "cost": 120.0, "buy_date": "2026-10-20"}
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# 录入校验
# --------------------------------------------------------------------------- #
def test_validate_ok():
    it = H.validate_item(_item())
    assert it["code"] == "sh511090"
    assert it["shares"] == 1000
    assert it["cost"] == 120.0


@pytest.mark.parametrize("bad,msg", [
    ({"code": "510300"}, "代码格式"),
    ({"code": "sh51030"}, "代码格式"),
    ({"shares": 0}, "份额"),
    ({"shares": -100}, "份额"),
    ({"shares": 2e9}, "10 亿"),
    ({"cost": 0}, "成本价"),
    ({"cost": -3}, "成本价"),
    ({"cost": 2e5}, "10 万元"),
    ({"buy_date": "2026/10/20"}, "YYYY-MM-DD"),
    ({"buy_date": "2099-01-01"}, "不能晚于今天"),
])
def test_validate_rejects(bad, msg):
    with pytest.raises(H.HoldingError) as e:
        H.validate_item(_item(**bad))
    assert msg in str(e.value)


def test_validate_fills_name_from_pool():
    it = H.validate_item({"code": "sz159915", "shares": 100, "cost": 2.7}, known={"sz159915": "创业板ETF"})
    assert it["name"] == "创业板ETF"


def test_upsert_replaces_same_code():
    d = {"items": [H.validate_item(_item())], "cash": 0.0}
    d = H.upsert(d, H.validate_item(_item(shares=200, cost=121.0)))
    assert len(d["items"]) == 1
    assert d["items"][0]["shares"] == 200


def test_remove_only_matching():
    d = {"items": [H.validate_item(_item()), H.validate_item(_item(code="sz159915"))], "cash": 0.0}
    d = H.remove(d, "sh511090")
    assert [i["code"] for i in d["items"]] == ["sz159915"]


# --------------------------------------------------------------------------- #
# 估值
# --------------------------------------------------------------------------- #
def test_value_basic():
    data = {"items": [H.validate_item(_item(shares=1000, cost=120.0))], "cash": 10000.0}
    q = {"sh511090": {"price": 126.0, "prev": 125.0, "date": "2026-10-23", "live": False}}
    v = H.value(data, q)
    # 市值 126000，成本 120000，浮盈 6000
    assert v["total_market_value"] == 126000.0
    assert v["total_cost"] == 120000.0
    assert v["total_pnl"] == 6000.0
    assert abs(v["total_pnl_pct"] - 0.05) < 1e-9
    # 今日盈亏 = 1000 × (126 - 125)
    assert v["day_pnl"] == 1000.0
    assert v["total_assets"] == 136000.0
    assert abs(v["position_ratio"] - 126000 / 136000) < 1e-9


def test_value_missing_quote_excluded_from_total():
    data = {"items": [
        H.validate_item(_item(code="sh511090", shares=1000, cost=120.0)),
        H.validate_item(_item(code="sz159915", shares=100, cost=2.0)),
    ], "cash": 0.0}
    q = {"sh511090": {"price": 126.0, "prev": 125.0}}
    v = H.value(data, q)
    assert v["total_market_value"] == 126000.0        # 缺行情的不计入
    assert v["missing_quotes"] == ["30年国债ETF"]
    miss = [r for r in v["items"] if r["price"] is None][0]
    assert miss["market_value"] is None and miss["pnl"] is None
    assert miss["cost_value"] == 200.0                # 成本仍计入总成本，便于对账


def test_value_no_prev_close_skips_day_pnl():
    data = {"items": [H.validate_item(_item(shares=100, cost=120.0))], "cash": 0.0}
    v = H.value(data, {"sh511090": {"price": 126.0}})
    assert v["day_pnl"] == 0.0
    assert v["items"][0]["day_pnl"] is None


def test_value_bought_today_uses_cost_basis():
    """今天买的仓，当日盈亏必须用成本价，不能用昨收——否则会把买入前的行情算进去。"""
    from datetime import date
    data = {"items": [H.validate_item(_item(shares=1000, cost=120.0, buy_date=date.today().isoformat()))],
            "cash": 0.0}
    v = H.value(data, {"sh511090": {"price": 126.0, "prev": 125.0}})
    assert v["day_pnl"] == 6000.0            # (126 − 120) × 1000
    assert v["items"][0]["day_basis"] == "cost"


def test_value_old_holding_uses_prev_basis():
    data = {"items": [H.validate_item(_item(shares=1000, cost=120.0, buy_date="2026-01-05"))], "cash": 0.0}
    v = H.value(data, {"sh511090": {"price": 126.0, "prev": 125.0}})
    assert v["day_pnl"] == 1000.0            # (126 − 125) × 1000
    assert v["items"][0]["day_basis"] == "prev"


def test_value_empty_holdings():
    v = H.value({"items": [], "cash": 5000.0}, {})
    assert v["total_market_value"] == 0.0
    assert v["position_ratio"] == 0.0
    assert v["total_assets"] == 5000.0
    assert v["total_pnl_pct"] is None


def test_value_weights_sum_to_one():
    data = {"items": [
        H.validate_item(_item(code="sh511090", shares=1000, cost=120.0)),
        H.validate_item(_item(code="sz159915", shares=10000, cost=2.0)),
    ], "cash": 0.0}
    q = {"sh511090": {"price": 120.0}, "sz159915": {"price": 2.0}}
    v = H.value(data, q)
    w = sum(r["weight"] for r in v["items"])
    assert abs(w - 1.0) < 1e-9


# --------------------------------------------------------------------------- #
# 调仓建议
# --------------------------------------------------------------------------- #
def _valued(cash=0.0, items=None):
    data = {"items": items or [], "cash": cash}
    return data


def test_advise_add_when_underweight():
    data = {"items": [], "cash": 100000.0}
    valuation = H.value(data, {})
    res = H.advise(valuation, [{"code": "sh511090", "name": "国债", "weight": 0.5, "price": 100.0}])
    plan = [p for p in res["plan"] if p["code"] == "sh511090"][0]
    assert plan["action"] == "add"
    assert plan["lots"] == 5          # 50000 / (100×100)


def test_advise_exit_for_dropped_name():
    data = {"items": [H.validate_item(_item(code="sz159915", shares=1000, cost=2.0))], "cash": 0.0}
    q = {"sz159915": {"price": 2.0}}
    valuation = H.value(data, q)
    res = H.advise(valuation, [{"code": "sh511090", "weight": 1.0, "price": 100.0}])
    p = [x for x in res["plan"] if x["code"] == "sz159915"][0]
    assert p["action"] == "exit"
    assert p["target_weight"] == 0.0


def test_advise_ignores_small_diff():
    # 已成 49%，目标 50%，差 1 个百分点 < 2% 阈值
    data = {"items": [H.validate_item(_item(code="sh511090", shares=49, cost=100.0))], "cash": 5100.0}
    valuation = H.value(data, {"sh511090": {"price": 100.0}})
    res = H.advise(valuation, [{"code": "sh511090", "weight": 0.50, "price": 100.0}])
    p = [x for x in res["plan"] if x["code"] == "sh511090"][0]
    assert p["action"] == "hold"
    assert res["needs_action"] == 0


def test_advise_hold_when_aligned():
    data = {"items": [H.validate_item(_item(code="sh511090", shares=100, cost=100.0))], "cash": 0.0}
    valuation = H.value(data, {"sh511090": {"price": 100.0}})
    res = H.advise(valuation, [{"code": "sh511090", "weight": 1.0, "price": 100.0}])
    assert res["plan"][0]["action"] == "hold"


# --------------------------------------------------------------------------- #
# 一键建仓
# --------------------------------------------------------------------------- #
def test_build_from_weights_rounds_down():
    rows = [
        {"code": "sh511090", "name": "国债", "weight": 0.5, "price": 120.0},
        {"code": "sz159915", "name": "创业板", "weight": 0.5, "price": 2.7},
    ]
    items, cash, notes = H.build_from_weights(rows, 100000.0)
    # 50000 / 12000 = 4 手 → 400 份；50000 / 270 = 185 手 → 18500 份
    by = {i["code"]: i for i in items}
    assert by["sh511090"]["shares"] == 400
    assert by["sz159915"]["shares"] == 18500
    spent = sum(i["shares"] * i["cost"] for i in items)
    assert abs(spent + cash - 100000.0) < 0.01
    assert cash >= 0
    # 高价标的手数少，取整后权重从 50% 掉到 48%，应被标记为欠配
    assert [n["code"] for n in notes] == ["sh511090"]
    assert notes[0]["kind"] == "underweight"
    assert notes[0]["actual_weight"] == pytest.approx(0.48, abs=1e-6)


def test_build_from_weights_flags_unaffordable_lot():
    """高价 ETF 一手超预算：不能静默跳过，必须进 notes 让界面提示。"""
    rows = [
        {"code": "sh511090", "name": "国债", "weight": 0.5, "price": 120.0},
        {"code": "sh510300", "name": "沪深300", "weight": 0.5, "price": 4.0},
    ]
    # 总资金 2 万 → 每只预算 1 万；12000 一手 > 10000，整只买不上
    items, cash, notes = H.build_from_weights(rows, 20000.0)
    assert [i["code"] for i in items] == ["sh510300"]
    assert len(notes) == 1
    assert notes[0]["code"] == "sh511090"
    assert notes[0]["kind"] == "lot_too_big"
    assert notes[0]["lot_cost"] == pytest.approx(12000.0)
    assert notes[0]["budget"] == pytest.approx(10000.0)
    assert "未买入" in notes[0]["message"]


def test_build_from_weights_no_notes_when_clean():
    """低价标的、资金充足时不应产生任何提示。"""
    rows = [{"code": "sz159915", "name": "创业板", "weight": 1.0, "price": 2.0}]
    items, cash, notes = H.build_from_weights(rows, 100000.0)
    assert len(items) == 1
    assert notes == []


def test_build_from_weights_rejects_zero_amount():
    with pytest.raises(H.HoldingError):
        H.build_from_weights([{"code": "sh511090", "weight": 1.0, "price": 1.0}], 0)


# --------------------------------------------------------------------------- #
# 存储
# --------------------------------------------------------------------------- #
def test_save_load_roundtrip(tmp_path):
    fp = tmp_path / "my_holdings.json"
    data = {"items": [H.validate_item(_item())], "cash": 1234.56}
    H.save(fp, data)
    back = H.load(fp)
    assert back["cash"] == 1234.56
    assert back["items"][0]["shares"] == 1000
    assert back["updated_at"]


def test_load_missing_returns_empty(tmp_path):
    d = H.load(tmp_path / "nope.json")
    assert d["items"] == [] and d["cash"] == 0.0


def test_load_corrupt_backs_up_and_returns_empty(tmp_path):
    fp = tmp_path / "my_holdings.json"
    fp.write_text("{ this is not json", encoding="utf-8")
    d = H.load(fp)
    assert d["items"] == []
    assert (tmp_path / "my_holdings.json.broken").exists()


def test_load_skips_bad_item_keeps_good(tmp_path):
    fp = tmp_path / "my_holdings.json"
    fp.write_text(json.dumps({
        "cash": 0,
        "items": [
            {"code": "sh511090", "shares": 100, "cost": 1.0},
            {"code": "BADCODE", "shares": 100, "cost": 1.0},
        ],
    }), encoding="utf-8")
    d = H.load(fp)
    assert [i["code"] for i in d["items"]] == ["sh511090"]
