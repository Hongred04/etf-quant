"""ETF 份额采集：字段解析、单位解析、追加去重（全部离线，不触网）。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from etfquant import shares as S


def _szse_payload():
    return [{
        "metadata": {"subname": "2026-10-30", "recordcount": 768},
        "data": [
            {"sys_key": "<a href='x'><u>158082</u></a>",
             "kzjcurl": "<a title='创业板电池ETF银华'><u>创业板电池ETF银华</u></a>",
             "dqgm": "15,173.52"},
            {"sys_key": "<a><u>159915</u></a>", "kzjcurl": "创业板ETF", "dqgm": "-"},
        ],
    }]


def _sse_payload():
    return {"result": [
        {"STAT_DATE": "2026-10-29", "SEC_CODE": "510300", "SEC_NAME": "沪深300ETF",
         "TOT_VOL": "4,545,678.90"},
        {"STAT_DATE": "2026-10-29", "SEC_CODE": "511010", "SEC_NAME": "国债ETF",
         "TOT_VOL": "12752.44"},
    ], "pageHelp": {"total": 923}}


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def test_parse_szse_extracts_code_name_shares():
    df, date = S.parse_szse(_szse_payload())
    assert date == "2026-10-30"
    assert list(df["ticker"]) == ["sz158082", "sz159915"]
    assert df.loc[0, "name"] == "创业板电池ETF银华"
    assert df.loc[0, "shares_wan"] == pytest.approx(15173.52)
    assert np.isnan(df.loc[1, "shares_wan"]), "'-' 必须当缺失，不能当 0"
    assert set(df["source"]) == {"szse"}


def test_parse_sse_uses_stat_date_not_today():
    df, date = S.parse_sse(_sse_payload())
    assert date == "2026-10-29"
    assert list(df["ticker"]) == ["sh510300", "sh511010"]
    assert df.loc[0, "shares_wan"] == pytest.approx(4545678.90)
    assert set(df["source"]) == {"sse"}


def test_number_parser_rejects_junk():
    assert S._num("15,173.52") == pytest.approx(15173.52)
    assert np.isnan(S._num("-"))
    assert np.isnan(S._num(""))
    assert np.isnan(S._num("暂无"))
    assert np.isnan(S._num("-3.5")), "负份额是不可能的，按缺失处理"


# --------------------------------------------------------------------------- #
# 追加与去重
# --------------------------------------------------------------------------- #
def test_append_snapshot_dedupes_same_day(tmp_path):
    fp = tmp_path / "shares.csv"
    first = pd.DataFrame({"date": pd.to_datetime(["2026-10-29"]), "ticker": ["sh510300"],
                          "name": ["沪深300ETF"], "shares_wan": [100.0], "source": ["sse"]})
    got = S.append_snapshot(first, fp)
    assert got == {"new_rows": 1, "total_rows": 1, "dates": 1}

    # 同一天重跑，值被修正 → 行数不变，取最新一次
    again = first.copy()
    again["shares_wan"] = 120.0
    got = S.append_snapshot(again, fp)
    assert got == {"new_rows": 0, "total_rows": 1, "dates": 1}
    saved = pd.read_csv(fp)
    assert saved.loc[0, "shares_wan"] == pytest.approx(120.0)


def test_append_snapshot_adds_new_dates(tmp_path):
    fp = tmp_path / "shares.csv"
    d1 = pd.DataFrame({"date": pd.to_datetime(["2026-10-29"]), "ticker": ["sh510300"],
                       "name": ["a"], "shares_wan": [1.0], "source": ["sse"]})
    d2 = d1.assign(date=pd.to_datetime("2026-10-30"), shares_wan=2.0)
    S.append_snapshot(d1, fp)
    got = S.append_snapshot(d2, fp)
    assert got["new_rows"] == 1 and got["total_rows"] == 2 and got["dates"] == 2


def test_append_snapshot_keeps_file_sorted(tmp_path):
    fp = tmp_path / "shares.csv"
    df = pd.DataFrame({
        "date": pd.to_datetime(["2026-10-30", "2026-10-29"]),
        "ticker": ["sh510300", "sz159915"],
        "name": ["a", "b"], "shares_wan": [1.0, 2.0], "source": ["sse", "szse"],
    })
    S.append_snapshot(df, fp)
    saved = pd.read_csv(fp, parse_dates=["date"])
    assert saved["date"].is_monotonic_increasing
