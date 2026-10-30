"""
第二轮实验：以"纯动量"为起点做增量归因（Incremental Attribution）。

方法
----
不做全参数网格搜索（易过拟合），而是沿一条单一路径逐项叠加模块，
每加一项观察 CAGR / Sharpe / MaxDD 的变化，判断该模块是否值得保留。
起点刻意设置为"无闸门、无过滤、等权、满仓"的最朴素动量策略。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from etfquant import metrics
from etfquant.backtest import Backtester
from etfquant.pipeline import benchmark_series, load_panel
from etfquant.strategy import TrendGatedAdaptive

PANEL_START, START, END = "2018-01-02", "2019-01-02", "2026-10-23"

# 朴素起点：短期动量打分、无任何过滤、等权、始终满仓、无熔断
BASE = dict(
    score_mode="mom20",
    expo_high=1.0, expo_mid=1.0, expo_low=1.0, regime_mid=0.7, regime_low=-1.0,
    buffer_mult=1, max_vr=99.0, require_trend=False,
    weight_mode="equal", dd_trigger=10.0,
)
GATE = dict(expo_mid=0.5, expo_low=0.0, regime_low=0.35, regime_mid=0.7)

LADDER: list[tuple[str, dict]] = [
    ("1. 纯动量(起点)", {}),
    ("2. + 趋势过滤 MA60", dict(require_trend=True)),
    ("3. + 拥挤度过滤", dict(require_trend=True, max_vr=3.0)),
    ("4. + 排名缓冲带", dict(require_trend=True, max_vr=3.0, buffer_mult=2)),
    ("5. + 市场状态闸门", dict(require_trend=True, max_vr=3.0, buffer_mult=2, **GATE)),
    ("6. + 回撤熔断", dict(require_trend=True, max_vr=3.0, buffer_mult=2, dd_trigger=0.10, **GATE)),
    ("7. 闸门改为仅降半仓(不空仓)", dict(require_trend=True, max_vr=3.0, buffer_mult=2,
                                        expo_mid=0.5, expo_low=0.5, regime_low=0.35, regime_mid=0.7)),
    ("8. 多因子打分替代纯动量", dict(require_trend=True, max_vr=3.0, buffer_mult=2,
                                    score_mode="multi", **GATE)),
    ("9. 波动率倒数加权", dict(require_trend=True, max_vr=3.0, buffer_mult=2,
                              weight_mode="inv_vol", **GATE)),
    ("10. Top5 分散", dict(require_trend=True, max_vr=3.0, buffer_mult=2, top_n=5, **GATE)),
]


# 线上默认配置 = 阶梯第 7 步（闸门只降半仓、不空仓）
# 之前这里取 LADDER[4]（第 5 步：允许空仓），年化 16.1%，
# 却在子样本表里被标成"完整模型"，把前段收益低估了约 2pct。
FINAL = LADDER[6][1]


def run(panel, bench, cfg, start=START, end=END):
    p = {**BASE, **cfg}
    s = TrendGatedAdaptive(panel, **p)
    t = s.generate()
    r = Backtester(panel.close, panel.open_).run(t, start=start, end=end)
    b = bench[(bench.index >= pd.Timestamp(start)) & (bench.index <= pd.Timestamp(end))]
    st = metrics.summarize(r.nav, b)
    st["年均换手"] = r.stats["年均换手(单边)"]
    st["调仓次数"] = r.stats["调仓次数"]
    return st, r


def main() -> None:
    panel = load_panel(ROOT / "data" / "etf_pool.csv", ROOT / "data" / "cache", PANEL_START, END)
    bench = benchmark_series(panel.close, "sh510300")

    print("=" * 104)
    print("增量归因  2019-01-02 ~ 2026-10-23")
    print("=" * 104)
    print(f"{'配置':<28}{'CAGR':>9}{'Sharpe':>8}{'MaxDD':>9}{'Calmar':>8}{'换手':>7}{'累计':>9}")
    rows = {}
    for name, cfg in LADDER:
        st, _ = run(panel, bench, cfg)
        rows[name] = st
        print(f"{name:<28}{st['年化收益(CAGR)']:>8.2%}{st['夏普比率']:>8.2f}"
              f"{st['最大回撤']:>9.2%}{st['卡玛比率']:>8.2f}{st['年均换手']:>7.2f}"
              f"{st['累计收益']:>9.1%}")

    pd.DataFrame(rows).T.to_csv(ROOT / "output" / "ladder.csv", encoding="utf-8-sig")

    print("\n" + "=" * 104)
    print("子样本稳定性（纯动量 vs 线上最终模型）")
    print("=" * 104)
    subs = [("前段 2019-2022", "2019-01-02", "2022-12-30"),
            ("后段 2023-2026", "2023-01-03", END)]
    sub_rows = {}
    for label, s0, s1 in subs:
        b = bench[(bench.index >= pd.Timestamp(s0)) & (bench.index <= pd.Timestamp(s1))]
        rec = {"基准CAGR": metrics.cagr(b), "基准MaxDD": metrics.max_drawdown(b),
               "基准Sharpe": metrics.sharpe(b)}
        for cname, cfg in [("纯动量", {}), ("最终模型", FINAL)]:
            st, _ = run(panel, bench, cfg, s0, s1)
            rec[f"{cname}_CAGR"] = st["年化收益(CAGR)"]
            rec[f"{cname}_Sharpe"] = st["夏普比率"]
            rec[f"{cname}_MaxDD"] = st["最大回撤"]
        sub_rows[label] = rec
    sdf = pd.DataFrame(sub_rows).T
    print(sdf.round(4).to_string())
    sdf.to_csv(ROOT / "output" / "subsample2.csv", encoding="utf-8-sig")


if __name__ == "__main__":
    main()
