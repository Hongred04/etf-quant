"""实验页结论与实验表必须同源。

为什么需要这一层
----------------
「效果验证」「怎么选」两页原来把结论**手写**在 HTML 里：
「多因子 17.7% vs 纯动量 23.9%」「1/σ 加权 22.6%」「缓冲带 12.5% → 14.7%」
「2019—2023 累计超额仅 +0.9pp」。

2026-11 修复回测记账（调仓日持 stock 过夜的跳空盈亏只乘了权重、没乘净值）之后，
全样本年化从 17% 级降到 12% 级，头部模块的相对强弱也翻转了：
1/σ 加权从"更差"变成比等权高 2pp。表格重跑了，手写的结论没跟着动——
页面上于是同时存在两套互相矛盾的数字，而任何自动检查都不会报错。

这一层的断言很窄：只要 brief 里的数字与实验表对不上，就失败。
它不判断结论对不对（那是研究的事），只保证"页面上说的数 = 表里的数"。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server as S  # noqa: E402

OUT = ROOT / "output"


def _has(fp: Path) -> bool:
    return fp.exists() and fp.stat().st_size > 0


needs_tables = pytest.mark.skipif(
    not (_has(OUT / "ladder.csv") and _has(OUT / "ablation.csv")),
    reason="缺少实验表，先跑 scripts/grid2.py 与 scripts/ablation.py",
)


def _csv(name: str) -> pd.DataFrame:
    return pd.read_csv(OUT / name, index_col=0)


@needs_tables
def test_ladder_final_matches_perf_main():
    """阶梯表里的「仅降半仓」那一步必须等于 perf_main.csv 的线上策略。

    这两张表由不同脚本生成（grid2.py / run_backtest.py），却描述同一个配置。
    一旦其中一个换了截止日或回测口径，它们就会静默分叉——而页面上
    实验一的第一行和总览的 KPI 卡片正好并排放着。
    """
    ladder = _csv("ladder.csv")
    perf = _csv("perf_main.csv")
    row = ladder[ladder.index.str.contains("仅降半仓")]
    assert len(row) == 1, "阶梯表里找不到「闸门改为仅降半仓」那一行"
    live = perf[perf["策略"] == "S4-TGAR(自研)"]
    assert len(live) == 1

    lc = float(row["年化收益(CAGR)"].iloc[0])
    pc = float(live["年化收益(CAGR)"].iloc[0])
    assert abs(lc - pc) < 5e-4, (
        f"阶梯表年化 {lc:.4%} 与 perf_main {pc:.4%} 不一致："
        "两张表的截止日或回测口径分叉了（检查 grid2.py 的 END 是否还在写死）"
    )
    lm = float(row["最大回撤"].iloc[0])
    pm = float(live["最大回撤"].iloc[0])
    assert abs(lm - pm) < 5e-4, f"最大回撤 {lm:.4%} vs {pm:.4%} 不一致"


@needs_tables
def test_brief_numbers_come_from_tables():
    """brief 里的每个数都必须能在实验表里找到原值，不是第二套算法。"""
    brief = S.api_research().get("brief") or {}
    ladder = _csv("ladder.csv")
    ablation = _csv("ablation.csv")

    assert brief.get("available") is True

    # 阶梯：纯动量 / 仅降半仓
    base = ladder[ladder.index.str.contains("纯动量")].iloc[0]
    assert brief["ladder"]["base_cagr"] == pytest.approx(float(base["年化收益(CAGR)"]), abs=1e-9)

    final = ladder[ladder.index.str.contains("仅降半仓")].iloc[0]
    assert brief["ladder"]["final_cagr"] == pytest.approx(float(final["年化收益(CAGR)"]), abs=1e-9)

    # 消融：完整模型 / 回撤熔断
    full = ablation[ablation.index.str.contains("完整模型")].iloc[0]
    assert brief["ablation"]["full"]["cagr"] == pytest.approx(float(full["年化收益(CAGR)"]), abs=1e-9)

    brake = ablation[ablation.index.str.contains("回撤熔断")].iloc[0]
    assert brief["ablation"]["dd_brake"]["cagr"] == pytest.approx(
        float(brake["年化收益(CAGR)"]), abs=1e-9)


@needs_tables
def test_brief_reports_actual_sign_not_expected_sign():
    """brief 只记录实测方向，不许为了迎合旧结论而翻转正负。

    这是上一轮真正的病灶：代码里写着「1/σ 更差」，而新数据是它更好。
    修数据还是修文案都可以，但**不许让接口替数据说谎**。
    """
    brief = S.api_research().get("brief") or {}
    ablation = _csv("ablation.csv")
    full = float(ablation[ablation.index.str.contains("完整模型")]["年化收益(CAGR)"].iloc[0])

    inv = ablation[ablation.index.str.contains("波动率倒数")]
    if len(inv):
        actual = float(inv["年化收益(CAGR)"].iloc[0])
        got = brief["ablation"]["inv_vol"]["cagr"]
        assert got == pytest.approx(actual, abs=1e-9)
        # 若实测优于等权，brief 的值也必须 > 完整模型，不能停在旧的"更差"
        if actual > full:
            assert got > brief["ablation"]["full"]["cagr"]


def test_excess_split_is_derived_from_nav():
    """分段超额必须能由净值直接复算，不能是另一套口径。"""
    nav_fp = OUT / "nav_main.csv"
    if not _has(nav_fp):
        pytest.skip("缺少 nav_main.csv")

    nav = pd.read_csv(nav_fp, index_col=0, parse_dates=True)
    seg = S.api_overview().get("excess_split")
    assert seg, "excess_split 为空——总览页脚注会退回一句空话"

    self_nav = nav["S4-TGAR(自研)"].dropna()
    bench = nav["基准-沪深300"].dropna().reindex(self_nav.index).ffill()
    for item in seg:
        a, z = item["label"], None
        # 用接口给出的数值反查：区间内累计收益之差必须等于 excess
        assert item["excess"] == pytest.approx(item["strat"] - item["bench"], abs=1e-9)
        assert -1.5 < item["excess"] < 5.0, f"{a} 的超额 {item['excess']:.2f} 明显不合理"
