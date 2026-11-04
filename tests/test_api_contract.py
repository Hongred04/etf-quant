"""后端接口的契约测试：每个 /api/* 至少要能返回，不能抛异常。

为什么需要这一层
----------------
2026-11-03 的一次改动把 `H.advise()` 的返回值当成 list 遍历，而它实际是
dict（含 plan / needs_action / buy_value / sell_value）。结果 `/api/holdings`
每次都抛 AttributeError，页面直接空白。328 个单测全绿，因为它们都在测
etfquant 里的纯函数，没有一个真正调用过 api_holdings。

这类错误不需要业务断言就能抓住——只要把每个接口调一遍、确认不抛异常即可。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server as S  # noqa: E402

# 轻量接口（不含 api_track：它要跑完整回测，约 7 秒）
ENDPOINTS = [
    "api_status",
    "api_signal",
    "api_holdings",
    "api_portfolio",
    "api_universe",
    "api_debug_paths",
    "api_research",
    "api_overview",
]


@pytest.mark.parametrize("name", ENDPOINTS)
def test_endpoint_returns_without_raising(name):
    """接口不能抛异常；允许在数据缺失时返回 {"error": ...}，但不能 500。"""
    fn = getattr(S, name, None)
    assert fn is not None, f"{name} 不存在，路由表与实现不同步"
    out = fn()                      # 抛异常即失败
    assert isinstance(out, dict), f"{name} 应返回 dict，实际 {type(out)}"


@pytest.mark.parametrize("name", ENDPOINTS)
def test_endpoint_is_json_serialisable(name):
    """返回体必须能 JSON 序列化，否则前端拿到的是 500。"""
    import json

    out = getattr(S, name)()
    json.dumps(out, ensure_ascii=False)   # 抛 TypeError 即失败


def test_holdings_injects_leg_on_every_plan_row():
    """核心-卫星：持仓与调仓建议的每一行都要带 leg，否则前端徽章会漏。

    leg 由 signal 注入，缺字段时默认 sat——但绝不能因为结构不对而少标。
    """
    out = S.api_holdings()
    if out.get("error"):
        pytest.skip(f"持仓数据不可用：{out['error']}")
    for item in out.get("items") or []:
        assert item.get("leg") in ("core", "sat"), f"{item.get('code')} 缺 leg"
    plan = (out.get("advice") or {}).get("plan") or []
    for row in plan:
        assert isinstance(row, dict), "advise().plan 必须是 dict 列表"
        assert row.get("leg") in ("core", "sat"), f"{row.get('code')} 缺 leg"


def test_portfolio_degrades_gracefully_without_kline_cache():
    """缺行情缓存时 /api/portfolio 必须返回 error dict，不能抛异常。

    2026-11-03 第一次在 GitHub Actions 上跑 CI 就红在这里：
    output/*.csv 随仓库走、data/cache（260 只 × 8.8 年日线）不入库，
    runner 上 load_panel 直接 RuntimeError → 500。
    这不只影响 CI——新克隆的仓库、丢了缓存的便携版都会踩同一个坑。
    """
    from unittest import mock

    def boom(*args, **kwargs):
        raise RuntimeError("缓存为空，请先运行 scripts/download_history.py")

    with mock.patch("etfquant.pipeline.load_panel", boom):
        out = S.api_portfolio()          # 抛异常即失败
    assert isinstance(out, dict) and out.get("error"), (
        "缺缓存时应返回 {'error': ...} 而不是抛异常或返回半截数据"
    )


def test_core_weight_override_is_validated():
    """模式开关：core_weight 只接受 0~1，非法值必须明确拒绝而不是静默夹边。

    界面上的「纯卫星 / 核心+卫星」切换靠这个字段落到 run_signal --core-weight。
    静默改成 0.5 会让用户以为自己切了模式、实际没切——比报错更糟。
    """
    assert S._parse_core_weight({}) == (None, None)          # 不传 = 沿用默认
    assert S._parse_core_weight({"core_weight": 0}) == (0.0, None)
    assert S._parse_core_weight({"core_weight": "0.5"}) == (0.5, None)

    for bad, why in [("abc", "数字"), (-0.1, "0~1"), (1.5, "0~1")]:
        val, err = S._parse_core_weight({"core_weight": bad})
        assert val is None and err and why in err, f"{bad!r} 应被拒绝：{val} {err}"


def test_signal_payload_exposes_current_mode():
    """前端靠 signal.core_weight 高亮开关，缺字段会让按钮永远显示默认态。"""
    out = S.api_signal()
    if out.get("error"):
        pytest.skip(f"信号不可用：{out['error']}")
    assert "core_weight" in out
    assert 0.0 <= float(out["core_weight"]) <= 1.0
