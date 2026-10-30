"""
"当日只落库一部分"的防护。

背景（2026-10-30 实测）：收盘后各 ETF 的当日 K 线分批落库，15:1x 时 260 只里只有
165 只有当日 bar。写进缓存后 `build_price_panel` 会对余下标的 ffill 昨天收盘价，
当天收益只由一部分标的贡献——横截面排名、波动率、量比同时失真，而且**不报错**。

这条测试钉住三件事：判定覆盖率、整天丢弃、以及**磁盘上那一天也要裁掉**
（只从内存删掉的话，下次运行 `last >= expected` 会判定"已是最新"，永远不再补）。
"""
from __future__ import annotations

import pandas as pd
import pytest

from etfquant import datafeed as DF

LAST_DAY = pd.Timestamp("2024-03-29")   # 周五


def _frame(end: str, n: int = 30) -> pd.DataFrame:
    idx = pd.bdate_range(end=end, periods=n)
    return pd.DataFrame({
        "date": idx,
        "open": 1.0,
        "close": 1.0,
        "high": 1.0,
        "low": 1.0,
        "volume": 1e5,
    })


def _pair(prefix: str, n_total: int, n_with_last: int) -> dict[str, pd.DataFrame]:
    """前 n_with_last 只有最后一天，其余只到前一天（模拟分批落库）。"""
    out = {}
    for i in range(n_total):
        tk = f"{prefix}{i:02d}"
        end = "2024-03-29" if i < n_with_last else "2024-03-28"
        out[tk] = _frame(end)
    return out


# --------------------------------------------------------------------------- #
# 覆盖率判定
# --------------------------------------------------------------------------- #
def test_latest_bar_coverage_counts_fraction():
    got = DF.latest_bar_coverage(_pair("a", 10, 7))
    assert got["date"] == LAST_DAY
    assert (got["have"], got["total"]) == (7, 10)
    assert got["coverage"] == pytest.approx(0.7)


def test_latest_bar_coverage_ignores_empty_frames():
    frames = _pair("a", 10, 10)
    frames["empty"] = pd.DataFrame(columns=["date", "close"])
    frames["none"] = pd.DataFrame()
    got = DF.latest_bar_coverage(frames)
    assert got["total"] == 10                      # 空表不进分母
    assert got["coverage"] == pytest.approx(1.0)


def test_latest_bar_coverage_on_empty_input():
    got = DF.latest_bar_coverage({})
    assert got["date"] is None and got["total"] == 0


# --------------------------------------------------------------------------- #
# 丢弃逻辑
# --------------------------------------------------------------------------- #
def test_strip_partial_day_drops_below_threshold():
    frames = _pair("a", 10, 6)
    out, info = DF.strip_partial_day(frames, min_coverage=0.8)
    assert info["applied"] is True
    assert info["coverage"] == pytest.approx(0.6)      # 报的是**丢弃前**的覆盖率
    assert len(info["dropped"]) == 6
    assert all(df["date"].max() < LAST_DAY for df in out.values())
    # 原本就没有这一天的标的保持原样（不该被当成"被丢弃"）
    assert "a09" not in info["dropped"]


def test_strip_partial_day_keeps_when_coverage_is_high():
    frames = _pair("a", 10, 9)
    out, info = DF.strip_partial_day(frames, min_coverage=0.8)
    assert info["applied"] is False
    assert info["dropped"] == []
    assert out is frames                               # 没有触发就绝不复制


def test_strip_partial_day_ignores_tiny_sample():
    """研究脚本只下三五只时，覆盖率没有统计意义，不能因此丢数据。"""
    frames = _pair("a", 3, 1)
    _, info = DF.strip_partial_day(frames, min_coverage=0.8, min_sample=10)
    assert info["applied"] is False
    assert info["total"] == 3


# --------------------------------------------------------------------------- #
# 与 download_panel 的集成
# --------------------------------------------------------------------------- #
def test_download_panel_does_not_persist_partial_day(monkeypatch, tmp_path):
    """半天数据不能留在缓存里——留着就再也不会被纠正。"""
    cache = DF.CacheConfig(root=tmp_path)
    frames = _pair("s", 12, 5)                         # 5/12 = 42%
    monkeypatch.setattr(DF, "fetch_kline", lambda tk, s, e, adj="qfq": frames[tk].copy())
    monkeypatch.setattr(DF, "market_last_trade_date", lambda: LAST_DAY)

    report: dict = {}
    got = DF.download_panel(list(frames), "2024-01-01", "2024-03-29", cache,
                            workers=2, min_bars=1, report=report)

    assert report["partial_day"]["applied"] is True
    assert report["partial_day"]["date"] == "2024-03-29"
    assert report["partial_day"]["dropped"] == 5
    # 内存里没有了
    assert all(df["date"].max() < LAST_DAY for df in got.values())
    # **磁盘上也没有了**：否则下次 `last >= expected` 会判定已是最新
    files = list((tmp_path / "kline_qfq").glob("*.csv"))
    assert len(files) == 12
    for fp in files:
        d = pd.read_csv(fp, parse_dates=["date"])
        assert not (d["date"] == LAST_DAY).any(), f"{fp.name} 仍留着半天的那根"


def test_download_panel_keeps_full_day(monkeypatch, tmp_path):
    """覆盖率达到门槛时正常写入，不能被误伤。"""
    cache = DF.CacheConfig(root=tmp_path)
    frames = _pair("s", 12, 12)
    monkeypatch.setattr(DF, "fetch_kline", lambda tk, s, e, adj="qfq": frames[tk].copy())
    monkeypatch.setattr(DF, "market_last_trade_date", lambda: LAST_DAY)

    report: dict = {}
    got = DF.download_panel(list(frames), "2024-01-01", "2024-03-29", cache,
                            workers=2, min_bars=1, report=report)
    assert report["partial_day"]["applied"] is False
    assert all(df["date"].max() == LAST_DAY for df in got.values())
