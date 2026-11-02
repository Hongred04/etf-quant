"""K 线接口的单测。

这一层容易错在"口径"而不是"算术"：
1. 买卖点只能来自调仓记录的 0→>0 / >0→0，日常微调（权重 16.7%→17.2%）
   不是交易；若当成交易，图上会出现一串毫无意义的旗标；
2. 日期必须落在行情序列里，否则前端按日期找索引会找不到、静默丢标记；
3. 代码格式要拦住，避免拿 `../../` 之类的东西去拼路径。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server as S


@pytest.fixture()
def fake_data(tmp_path, monkeypatch):
    cache = tmp_path / "cache" / "kline_qfq"
    cache.mkdir(parents=True)
    dates = pd.bdate_range("2026-10-01", periods=8)
    k = pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "open": [1.0 + i * 0.01 for i in range(8)],
        "high": [1.02 + i * 0.01 for i in range(8)],
        "low": [0.99 + i * 0.01 for i in range(8)],
        "close": [1.01 + i * 0.01 for i in range(8)],
        "volume": [1000 + i * 10 for i in range(8)],
    })
    k.to_csv(cache / "sh516620.csv", index=False)
    pool = pd.DataFrame([{
        "ticker": "sh516620", "name": "影视ETF国泰", "category": "行业",
    }])
    pool.to_csv(tmp_path / "etf_pool.csv", index=False)

    trades = pd.DataFrame([
        {"date": "2026-10-08", "code": "sh516620", "w_from": 0.0, "w_to": 0.1667, "delta": 0.1667},
        {"date": "2026-10-09", "code": "sh516620", "w_from": 0.1667, "w_to": 0.1720, "delta": 0.0053},
        {"date": "2026-10-12", "code": "sh516620", "w_from": 0.1720, "w_to": 0.0, "delta": -0.1720},
        {"date": "2026-10-13", "code": "sz159915", "w_from": 0.0, "w_to": 0.5, "delta": 0.5},
    ])
    trades.to_csv(tmp_path / "trades_s4_tgar_main.csv", index=False)

    monkeypatch.setattr(S, "DATA", tmp_path)
    monkeypatch.setattr(S, "OUT", tmp_path)
    return tmp_path


def test_kline_rows_and_meta(fake_data):
    # bars 有 30 的下限（少于 30 根画不出均线），这里只有 8 根，所以全给
    d = S.api_kline("sh516620", 5)
    assert d["name"] == "影视ETF国泰" and d["category"] == "行业"
    assert len(d["rows"]) == 8
    assert d["data_date"] == d["rows"][-1][0]
    # 行结构：[date, open, high, low, close, volume]
    assert len(d["rows"][0]) == 6


def test_trade_marks_only_entries_and_exits(fake_data):
    """权重 16.67%→17.20% 只是微调，不能算一次交易。"""
    d = S.api_kline("sh516620", 5)
    assert d["trades"] == [["2026-10-08", "B"], ["2026-10-12", "S"]]


def test_trade_marks_ignore_other_codes(fake_data):
    d = S.api_kline("sh516620", 50)
    assert all(code != "sz159915" for code, _ in d["trades"])


@pytest.mark.parametrize("bad", ["", "../../etc/passwd", "516620", "sh51662"])
def test_rejects_bad_code(fake_data, bad):
    assert "error" in S.api_kline(bad, 100)


def test_missing_cache_is_an_error(fake_data):
    assert "error" in S.api_kline("sz159999", 100)
