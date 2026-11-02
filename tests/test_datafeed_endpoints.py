"""K 线多入口轮换的单测。全部**不打网络**：把 `_http_get` 换掉。

这一层的错误是"静默降级"型的——备用入口不可用时会表现为"某天没有数据"，
而不是报错。所以每个行为都要钉住：失败要落到备用、死的要冷却、结果要一致。
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from etfquant import datafeed as DF

PRIMARY = "web.ifzq.gtimg.cn"
BACKUP = "proxy.finance.qq.com"


def _payload(rows: list[list[str]]) -> str:
    return json.dumps({"code": 0, "data": {"sh510300": {"qfqday": rows}}})


def _row(date: str, close: str = "1.10") -> list[str]:
    return [date, "1.00", close, "1.20", "0.90", "100"]


@pytest.fixture(autouse=True)
def _clean_state():
    DF.reset_endpoint_health()
    yield
    DF.reset_endpoint_health()


def test_falls_back_to_backup_when_primary_500(monkeypatch):
    calls: list[str] = []

    def fake(url, **kw):
        host = url.split("/")[2]
        calls.append(host)
        if host == PRIMARY:
            raise RuntimeError("HTTP 请求失败: ... (HTTP Error 501)")
        return _payload([_row("2020-01-02")])

    monkeypatch.setattr(DF, "_http_get", fake)
    df = DF.fetch_kline_window("sh510300", "2020-01-01", "2020-01-03")
    assert len(df) == 1
    assert BACKUP in calls
    # 失败的那个入口必须进冷却，否则后续 260 只标的会各白等两轮
    assert DF.endpoint_health()[PRIMARY].startswith("cooldown")


def test_dead_host_is_not_retried(monkeypatch):
    calls: list[str] = []

    def fake(url, **kw):
        host = url.split("/")[2]
        calls.append(host)
        if host == PRIMARY:
            raise RuntimeError("501")
        return _payload([_row("2020-01-02")])

    monkeypatch.setattr(DF, "_http_get", fake)
    DF.fetch_kline_window("sh510300", "2020-01-01", "2020-01-03")
    calls.clear()
    DF.fetch_kline_window("sh510300", "2020-01-01", "2020-01-03")
    assert PRIMARY not in calls          # 冷却期内一次都不该再打


def test_round_robin_splits_load(monkeypatch):
    """两个入口都活着时，连续两次请求应从不同入口开始——这是分摊 600 次上限的关键。"""
    seen: list[str] = []

    def fake(url, **kw):
        seen.append(url.split("/")[2])
        return _payload([_row("2020-01-02")])

    monkeypatch.setattr(DF, "_http_get", fake)
    DF.fetch_kline_window("sh510300", "2020-01-01", "2020-01-03")
    first, seen[:] = seen[0], []
    DF.fetch_kline_window("sh510300", "2020-01-01", "2020-01-03")
    assert first != seen[0]


def test_window_is_clipped_even_if_source_returns_extra(monkeypatch):
    """备用入口只认 count，会多给 start 之前的 bar——必须裁掉，否则两入口行为不一致。"""
    monkeypatch.setattr(DF, "_http_get", lambda url, **kw: _payload(
        [_row("2019-12-30"), _row("2019-12-31"), _row("2020-01-02"), _row("2020-01-06")]
    ))
    df = DF.fetch_kline_window("sh510300", "2020-01-01", "2020-01-03")
    assert df["date"].dt.strftime("%Y-%m-%d").tolist() == ["2020-01-02"]


def test_all_hosts_down_raises_with_names(monkeypatch):
    def fake(url, **kw):
        raise RuntimeError("HTTP 请求失败: 501")

    monkeypatch.setattr(DF, "_http_get", fake)
    with pytest.raises(RuntimeError) as ei:
        DF.fetch_kline_window("sh510300", "2020-01-01", "2020-01-03")
    msg = str(ei.value)
    assert PRIMARY in msg and BACKUP in msg


def test_empty_payload_is_not_an_error(monkeypatch):
    """未上市（空数据）是正常结果，不能触发入口降级。"""
    monkeypatch.setattr(DF, "_http_get", lambda url, **kw: _payload([]))
    df = DF.fetch_kline_window("sh510300", "2020-01-01", "2020-01-03")
    assert df.empty
    assert all(v == "ok" for v in DF.endpoint_health().values())


# --------------------------------------------------------------------------- #
# 收盘后前复权滞后：用不复权日线补当日 bar
# --------------------------------------------------------------------------- #
# 2026-11-02 15:40 实测：qfq 序列停在上一交易日，不复权 day 已有当日，
# 而策略等不起。两者只差一个复权系数，可精确换算。
def _payload_key(rows: list[list[str]], key: str) -> str:
    return json.dumps({"code": 0, "data": {"sh510300": {key: rows}}})


def _two_series(monkeypatch, *, raw_has_today: bool, closed: bool = True):
    today = pd.Timestamp.today().normalize()
    d1 = (today - pd.Timedelta(days=3)).strftime("%Y-%m-%d")
    d0 = (today - pd.Timedelta(days=4)).strftime("%Y-%m-%d")
    t = today.strftime("%Y-%m-%d")
    # 不复权价格是 qfq 的 2 倍 → 换算系数 0.5
    qfq = [
        [d0, "1.00", "1.10", "1.20", "0.90", "100"],
        [d1, "1.10", "1.10", "1.20", "1.00", "110"],
    ]
    raw = [
        [d0, "2.00", "2.20", "2.40", "1.80", "100"],
        [d1, "2.20", "2.20", "2.40", "2.00", "110"],
    ]
    if raw_has_today:
        raw.append([t, "2.22", "2.20", "2.30", "2.10", "90"])

    def fake(url, **kw):
        return _payload_key(raw if url.endswith(",day") else qfq,
                            "day" if url.endswith(",day") else "qfqday")

    monkeypatch.setattr(DF, "_http_get", fake)
    monkeypatch.setattr(DF, "is_market_closed", lambda now=None: closed)
    return d0, d1, t


def test_fills_today_bar_from_unadjusted(monkeypatch):
    """qfq 缺当日、不复权有当日 → 按系数换算补上（成交量不换算）。"""
    d0, d1, t = _two_series(monkeypatch, raw_has_today=True)
    df = DF.fetch_kline_window("sh510300", d0, t)
    assert df["date"].dt.strftime("%Y-%m-%d").tolist() == [d0, d1, t]
    last = df.iloc[-1]
    assert abs(last["close"] - 1.10) < 1e-9       # 2.20 × 0.5
    assert abs(last["open"] - 1.11) < 1e-9        # 2.22 × 0.5
    assert last["volume"] == 90                   # 成交量不做复权换算


def test_no_fill_intraday(monkeypatch):
    """盘中不复权那根是未完成的，绝不能当收盘价写进来。"""
    d0, _, t = _two_series(monkeypatch, raw_has_today=True, closed=False)
    df = DF.fetch_kline_window("sh510300", d0, t)
    assert df["date"].max().strftime("%Y-%m-%d") != t


def test_no_fill_without_today_in_raw(monkeypatch):
    """不复权也没有当日 → 保持原样，交给上层重试，不能凭空造 bar。"""
    d0, d1, t = _two_series(monkeypatch, raw_has_today=False)
    df = DF.fetch_kline_window("sh510300", d0, t)
    assert df["date"].dt.strftime("%Y-%m-%d").tolist() == [d0, d1]
