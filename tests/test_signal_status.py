"""
清单状态与调仓日提醒的单元测试。

这段判定直接决定「今天该不该动手」：写错了用户要么白跑一趟，要么错过换仓。
手机端 `toSignal()` 里有一份等价实现（JS），两边必须同规则，所以这里把边界钉死。
"""
from __future__ import annotations

import pandas as pd

import server as S

D = pd.Timestamp("2026-10-26")      # 调仓决策日
E = pd.Timestamp("2026-10-27")      # 开盘买入日
N = pd.Timestamp("2026-11-02")      # 下次调仓日


# --------------------------------------------------------------------------- #
# 五态判定
# --------------------------------------------------------------------------- #
def test_pending_before_exec_day():
    assert S._signal_status(pd.Timestamp("2026-10-25"), E, N) == "pending"
    assert S._signal_status(D, E, N) == "pending"


def test_exec_today_on_exec_day():
    assert S._signal_status(E, E, N) == "exec_today"


def test_holding_between_exec_and_next():
    assert S._signal_status(pd.Timestamp("2026-10-28"), E, N) == "holding"
    assert S._signal_status(pd.Timestamp("2026-10-30"), E, N) == "holding"


def test_today_on_rebalance_day():
    assert S._signal_status(N, E, N) == "today"


def test_due_after_rebalance_day():
    assert S._signal_status(pd.Timestamp("2026-11-03"), E, N) == "due"


def test_no_next_rebalance_means_holding():
    """数据只到当前期时拿不到下一个调仓日，不能因此报"过期"。"""
    assert S._signal_status(pd.Timestamp("2026-10-28"), E, None) == "holding"


def test_statuses_are_mutually_exclusive_along_a_timeline():
    """沿时间轴走一遍：只允许按 pending → exec_today → holding → today → due 推进。"""
    seq = [pd.Timestamp(d) for d in
           ("2026-10-25", "2026-10-26", "2026-10-27", "2026-10-28", "2026-11-01",
            "2026-11-02", "2026-11-03")]
    got = [S._signal_status(d, E, N) for d in seq]
    assert got == ["pending", "pending", "exec_today", "holding", "holding", "today", "due"]


# --------------------------------------------------------------------------- #
# 提醒内容
# --------------------------------------------------------------------------- #
def test_remind_only_on_action_days():
    assert S._remind_of("exec_today", D, E, N)["should"] is True
    assert S._remind_of("today", D, E, N)["should"] is True
    for quiet in ("pending", "holding", "due"):
        r = S._remind_of(quiet, D, E, N)
        assert r["should"] is False and r["title"] == "" and r["body"] == ""


def test_remind_text_is_actionable():
    r = S._remind_of("exec_today", D, E, N)
    assert "开盘" in r["title"] and str(D.date()) in r["body"]
    r2 = S._remind_of("today", D, E, N)
    assert "调仓日" in r2["title"] and "收盘" in r2["body"]


def test_remind_kind_matches_status():
    assert S._remind_of("exec_today", D, E, N)["kind"] == "exec"
    assert S._remind_of("today", D, E, N)["kind"] == "decision"
