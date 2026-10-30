"""
月报渲染测试。

月报每月只跑一次，跑挂了往往到月初才发现——而且它输出的是"给用户看的结论"，
数字错一位不会报错、只会被当真。所以这里把边界都钉住：跨年月份、
本月没有调仓、口径数据缺失、净值点不足两个。
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etfquant import monthly as M


def _data(**kw) -> dict:
    base = {
        "month": "2026-10",
        "generated_at": "2026-11-01 09:00",
        "data_date": "2026-10-30",
        "count": 20, "live": 3, "backfill": 17,
        "first": "2026-06-04", "last": "2026-10-26",
        "paper": {"total": 0.11, "cagr": 0.35, "mdd": -0.05},
        "backtest": {"total": 0.108, "cagr": 0.34, "mdd": -0.05},
        "month_ret": 0.0121, "month_ret_bt": 0.0121,
        "periods": [{
            "signal_date": "2026-10-26", "exec_date": "2026-10-27",
            "ret": 0.0121, "cum": 0.11, "source": "live",
            "holdings": [{"code": "sh511010", "name": "国债ETF", "weight": 0.5},
                         {"code": "sh513100", "name": "纳指ETF", "weight": 0.5}],
        }],
        "next_reb": "2026-11-02", "days_to_next": 2,
        "expect": {"label": "11%~13%", "band": [0.11, 0.13], "anchor": 0.1262,
                   "sample": 0.1745, "discount": 0.2767, "pbo": 0.432},
        "warnings": [],
    }
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# 月份计算
# --------------------------------------------------------------------------- #
def test_prev_month_crosses_year():
    assert M.prev_month(date(2026, 1, 15)) == "2025-12"
    assert M.prev_month(date(2026, 3, 1)) == "2026-02"
    assert M.prev_month(date(2026, 10, 29)) == "2026-09"


def test_month_key_accepts_str_and_timestamp():
    assert M.month_key("2026-10-26 15:00:00") == "2026-10"
    assert M.month_key(pd.Timestamp("2026-10-26")) == "2026-10"
    assert M.month_key(date(2026, 10, 26)) == "2026-10"


def test_slice_series_filters_by_month():
    series = [{"date": "2026-09-30", "paper": 1.0}, {"date": "2026-10-08", "paper": 1.1},
              {"date": "2026-10-31", "paper": 1.2}]
    assert len(M.slice_series(series, "2026-10")) == 2


def test_month_return_needs_two_points():
    assert M.month_return([{"paper": 1.0}]) is None
    assert M.month_return([]) is None
    got = M.month_return([{"paper": 1.0}, {"paper": 1.1}])
    assert round(got, 6) == 0.1


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def test_render_contains_core_numbers():
    md = M.render_markdown(_data())
    assert "# ETF Quant 月报 · 2026-10" in md
    assert "+11.0%" in md                       # 纸上累计，带符号
    assert "+0.20pp" in md                      # 偏差 = 11.0 − 10.8
    assert "11%~13%" in md                      # 口径必须出现
    assert "国债ETF 50%" in md                  # 持仓按权重渲染
    assert "2026-11-02" in md


def test_render_handles_no_rebalance_this_month():
    md = M.render_markdown(_data(periods=[]))
    assert "本月没有调仓" in md
    assert "偏差" in md                         # 表格不能因为没调仓就塌


def test_render_handles_missing_expectation():
    md = M.render_markdown(_data(expect=None))
    assert "口径" not in md                     # 没有锚点就不编区间
    assert "+11.0%" in md


def test_render_shows_warnings():
    md = M.render_markdown(_data(warnings=["数据已超过 7 天未更新"]))
    assert "## 六、提示" in md and "7 天未更新" in md


def test_render_without_days_to_next():
    md = M.render_markdown(_data(days_to_next=None))
    assert "下次调仓 **2026-11-02**。" in md


def test_alpha_section_renders_rules_not_just_number():
    """alpha 一节必须带上读数规则——只给一个残差数字等于鼓励误读。"""
    md = M.render_markdown({
        "month": "2026-10",
        "alpha": {
            "resid": 0.0959, "z": 1.7, "pct": 0.91, "hist_n": 94, "sigma_month": 0.057,
            "expo_month": 0.50, "expo_avg": 0.74, "mkt_month": -0.072,
            "window_start": "2026-10", "window_n": 1, "window_cum": 0.0959,
            "window_ann": None, "window_se": 0.682,
            "alpha_ann": 0.096, "months_to_detect": 203, "months_to_5pp": 186,
        },
    })
    assert "## 五、alpha 还在不在" in md
    assert "读数规则" in md and "不解读" in md
    assert "做不到" in md                      # 必须写明"证明 alpha 存在"做不到
    assert "2026-10 起 1 个月" in md
    assert "样本不足" in md                     # 1 个月不给年化


def test_alpha_section_is_optional():
    """没有诊断为证时不渲染该节，月报其余部分照常。"""
    md = M.render_markdown({"month": "2026-10", "count": 3})
    assert "alpha 还在不在" not in md
    assert "## 一、跟踪概览" in md
