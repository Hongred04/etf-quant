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
