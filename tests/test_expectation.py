"""
对外口径换算的测试。

这个模块只做一件事：把"样本内年化"换成可以对外说的区间。但它一旦算错，
错误会同时出现在 README、桌面、手机、月报四个地方，而且都是"看起来很正常"的数字，
所以边界（锚点缺失、负区间、老版本 summary）都要钉住。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from etfquant import expectation as E


def test_band_matches_current_numbers():
    """12.62% 的锚点 → 11%~13%（本项目对外口径，不要悄悄变成 12%~14%）。"""
    assert E.band_of(0.1262) == (0.11, 0.13)


def test_band_rounds_down_then_pads():
    assert E.band_of(0.20) == (0.19, 0.21)
    assert E.band_of(0.0999) == (0.08, 0.10)


def test_band_never_goes_negative():
    assert E.band_of(0.004) == (0.0, 0.02)


def test_band_rejects_missing_anchor():
    with pytest.raises(ValueError):
        E.band_of(float("nan"))


def test_expectation_reports_discount():
    got = E.expectation(0.1745, 0.1262, pbo=0.432, n_variants=29)
    assert got["label"] == "11%~13%"
    assert got["band"] == [0.11, 0.13]
    assert got["discount"] == pytest.approx(0.277, abs=1e-3)
    assert got["pbo"] == 0.432 and got["n_variants"] == 29


def test_expectation_refuses_without_anchor():
    """没有锚点就不给区间——不要用"样本内乘个系数"临时编一个。"""
    with pytest.raises(ValueError):
        E.expectation(0.1745, None)


def test_from_summary_prefers_stored_block_but_sample_is_live():
    """区间/锚点用文件里的（研究结论，慢变量）；sample 必须跟当天回测走（快变量）。"""
    stored = {"band": [0.11, 0.13], "label": "11%~13%", "anchor": 0.1262, "sample": 0.1745}
    got = E.from_summary({"expectation": stored, "wf_cagr": 0.1262}, sample_cagr=0.1734)
    assert got["label"] == "11%~13%"
    assert got["sample"] == 0.1734                  # 数据更新一天，样本内上限跟着变
    assert got["discount"] == round(1 - 0.1262 / 0.1734, 4)
    # 没传 sample 时保持文件原值
    got2 = E.from_summary({"expectation": stored})
    assert got2["sample"] == 0.1745


def test_from_summary_falls_back_to_wf_cagr():
    """老版本 json 没有 expectation 字段：用 wf_cagr 现算，界面不要空一块。"""
    got = E.from_summary({"wf_cagr": 0.1262, "pbo": 0.432, "n_variants": 29}, sample_cagr=0.1745)
    assert got["label"] == "11%~13%"
    assert got["sample"] == 0.1745


def test_from_summary_handles_missing():
    assert E.from_summary(None) is None
    assert E.from_summary({}) is None
    assert E.from_summary({"pbo": 0.4}) is None     # 没有锚点，不给区间


def test_load_tolerates_broken_file(tmp_path):
    assert E.load(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{不是 json", encoding="utf-8")
    assert E.load(bad) is None
    ok = tmp_path / "ok.json"
    ok.write_text('{"wf_cagr": 0.1262, "pbo": 0.43}', encoding="utf-8")
    assert E.load(ok, sample_cagr=0.1745)["label"] == "11%~13%"
