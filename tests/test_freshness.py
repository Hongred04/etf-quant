"""
数据新鲜度判断的单元测试。

这段逻辑很容易写错，而且错了不会报错——只会让页面显示一个看起来"正确"的旧日期，
或者反过来在春节后疯狂提示"数据落后 7 个交易日"。

三个关键口径：
1. 「应到哪天」必须优先用行情快照的时间戳判断，而不是日历推算，
   否则法定节假日一律误判；
2. 盘中时今天的数据本来就不完整，应到日是上一个工作日；
3. 落后天数只数工作日，且是 (data_date, expected] 的半开区间。
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server as S


def _prev_weekday_str(d: date) -> str:
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


# --------------------------------------------------------------------------- #
# 工作日推算
# --------------------------------------------------------------------------- #
def test_prev_weekday_skips_weekend():
    # 2026-10-26 是周一，前一个工作日应为周五 10-23
    assert S._prev_weekday(date(2026, 10, 26)) == "2026-10-23"
    assert S._prev_weekday(date(2026, 10, 25)) == "2026-10-23"   # 周日
    assert S._prev_weekday(date(2026, 10, 27)) == "2026-10-26"


@pytest.mark.parametrize("start,end,want", [
    ("2026-10-23", "2026-10-27", 2),    # 周五 → 周二：周一、周二
    ("2026-10-23", "2026-10-23", 0),    # 同一天
    ("2026-10-27", "2026-10-23", 0),    # 反向：不能返回负数
    ("2026-10-23", "2026-10-26", 1),    # 只跨周末，算 1 个工作日
    ("2026-10-26", "2026-10-30", 4),    # 周一到周五
    ("", "2026-10-27", 0),              # 空日期不炸
])
def test_weekdays_between(start, end, want):
    assert S._weekdays_between(start, end) == want


# --------------------------------------------------------------------------- #
# 应到交易日
# --------------------------------------------------------------------------- #
def test_expected_uses_snapshot_when_today_trading_intraday(monkeypatch):
    today = date.today().isoformat()
    monkeypatch.setattr(S, "_market_session", lambda: {"date": today, "time": "10:30"})
    want, intraday = S._expected_session()
    assert want == S._prev_weekday(date.today())
    assert intraday is True


def test_expected_uses_today_after_close(monkeypatch):
    today = date.today().isoformat()
    monkeypatch.setattr(S, "_market_session", lambda: {"date": today, "time": "15:05"})
    want, intraday = S._expected_session()
    assert want == today
    assert intraday is False


def test_expected_stays_on_holiday(monkeypatch):
    """快照停在上一个交易日 = 今天没成交（周末/节假日），不能按日历往前推。"""
    last = _prev_weekday_str(date.today())
    monkeypatch.setattr(S, "_market_session", lambda: {"date": last, "time": "15:00"})
    want, intraday = S._expected_session()
    assert want == last
    assert intraday is False


def test_expected_falls_back_when_offline(monkeypatch):
    monkeypatch.setattr(S, "_market_session", lambda: {})
    want, _ = S._expected_session()
    assert want == _prev_weekday_str(date.today())


# --------------------------------------------------------------------------- #
# 状态接口
# --------------------------------------------------------------------------- #
def test_status_shape(monkeypatch):
    monkeypatch.setattr(S, "_data_date", lambda: "2026-10-23")
    monkeypatch.setattr(S, "_market_session", lambda: {"date": "2026-10-27", "time": "15:00"})
    st = S.api_status()
    assert st["data_date"] == "2026-10-23"
    assert st["expected_date"] == "2026-10-27"
    assert st["behind_sessions"] == 2
    assert st["stale"] is True
    assert set(["running", "intraday", "auto_attempted", "market_open_hint"]) <= set(st)


def test_status_not_stale_when_same_day(monkeypatch):
    monkeypatch.setattr(S, "_data_date", lambda: "2026-10-27")
    monkeypatch.setattr(S, "_market_session", lambda: {"date": "2026-10-27", "time": "15:00"})
    st = S.api_status()
    assert st["stale"] is False and st["behind_sessions"] == 0


# --------------------------------------------------------------------------- #
# 回测产物日期
# --------------------------------------------------------------------------- #
def test_bt_date_reads_last_row(tmp_path, monkeypatch):
    fp = tmp_path / "weights_s4_tgar_main.csv"
    fp.write_text(",sh511090,sz159915\n2026-10-22,0.5,0.5\n2026-10-27,0.4,0.6\n", encoding="utf-8")
    monkeypatch.setattr(S, "OUT", tmp_path)
    assert S._bt_date() == "2026-10-27"


def test_bt_date_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "OUT", tmp_path)
    assert S._bt_date() == ""


def test_status_flags_bt_lag(tmp_path, monkeypatch):
    fp = tmp_path / "weights_s4_tgar_main.csv"
    fp.write_text(",a\n2026-10-23,1.0\n", encoding="utf-8")
    monkeypatch.setattr(S, "OUT", tmp_path)
    monkeypatch.setattr(S, "_data_date", lambda: "2026-10-27")
    monkeypatch.setattr(S, "_market_session", lambda: {"date": "2026-10-27", "time": "15:00"})
    st = S.api_status()
    assert st["bt_date"] == "2026-10-23"
    assert st["bt_stale"] is True
    assert st["stale"] is False       # 行情是新的，只是回测没重跑


# --------------------------------------------------------------------------- #
# 自动更新的冷却重试
# --------------------------------------------------------------------------- #
# 场景：收盘后自动更新扑空（数据源当日 K 线还没入库），如果永久记"已尝试"，
# 当天就再也不自动重试——用户看到"都收盘了还不更新"。冷却期过后必须放行。
def test_auto_attempt_cooldown(monkeypatch):
    monkeypatch.setattr(S, "_market_session",
                        lambda: {"date": "2026-11-02", "time": "15:05"})
    expected = S._expected_session()[0]

    with S._AUTO_LOCK:
        S._AUTO_ATTEMPT.clear()

    # 从未尝试 → 不算已尝试
    assert S.api_status()["auto_attempted"] is False

    # 刚尝试过 → 冷却期内，视为已尝试
    S.mark_auto_attempt()
    assert S.api_status()["auto_attempted"] is True

    # 冷却期过后（把时间戳往回拨）→ 放行下一次自动重试
    with S._AUTO_LOCK:
        S._AUTO_ATTEMPT["at"] = 0.0
    assert S.api_status()["auto_attempted"] is False

    with S._AUTO_LOCK:
        S._AUTO_ATTEMPT.clear()


def test_auto_attempt_key_change_retries(monkeypatch):
    """盘中尝试过（目标日=昨日），收盘后目标日推进到今天 → 立即允许重试。"""
    monkeypatch.setattr(S, "_market_session",
                        lambda: {"date": "2026-11-02", "time": "15:05"})
    expected_now = S._expected_session()[0]
    with S._AUTO_LOCK:
        S._AUTO_ATTEMPT["key"] = f"2026-11-02|NOT-{expected_now}"
        S._AUTO_ATTEMPT["at"] = S.time.time()      # 刚刚尝试过，但 key 不同
    assert S.api_status()["auto_attempted"] is False
    with S._AUTO_LOCK:
        S._AUTO_ATTEMPT.clear()


# --------------------------------------------------------------------------- #
# 仓位结构拆分（"半仓"到底指什么）
# --------------------------------------------------------------------------- #
def test_position_split_half_equity_half_bond():
    rows = [
        {"code": "sh511090", "category": "债券", "weight": 0.5},
        {"code": "sz159509", "category": "跨境", "weight": 0.1667},
        {"code": "sh516620", "category": "行业", "weight": 0.1667},
        {"code": "sh515450", "category": "主题", "weight": 0.1667},
    ]
    pos = S._position_split(rows)
    assert pos["bond"] == 0.5
    assert abs(pos["equity"] - 0.5001) < 1e-6
    assert pos["cash"] == 0.0                       # 半仓 ≠ 留一半现金
    # 源权重 1/6 存成 0.1667，三块合计允许 0.1% 的舍入差
    assert abs(pos["equity"] + pos["bond"] + pos["cash"] - 1.0) < 1e-3


def test_position_split_full_equity_no_bond():
    rows = [{"category": "宽基", "weight": 0.45}, {"category": "行业", "weight": 0.45}]
    pos = S._position_split(rows)
    assert pos["equity"] == 0.9 and pos["bond"] == 0.0
    assert abs(pos["cash"] - 0.1) < 1e-9             # 有剩余才算现金


def test_position_split_empty():
    pos = S._position_split([])
    assert pos["equity"] == 0 and pos["bond"] == 0 and pos["cash"] == 1.0
