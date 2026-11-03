"""
消融实验（Ablation Study）与子样本稳健性检验。

目的：验证 TGAR 各模块是否真实贡献超额收益，而非叙事性堆砌。
每个配置只改变一个模块，其余保持默认，观察 CAGR / Sharpe / MaxDD 的边际变化。
同时把区间切分为前后两段，检验策略是否依赖单一市场环境。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from etfquant import metrics
from etfquant.backtest import Backtester
from etfquant.pipeline import benchmark_series, latest_data_date, load_panel
from etfquant.strategy import TrendGatedAdaptive

PANEL_START, START = "2018-01-02", "2019-01-02"
# 同 grid2：截止日从缓存反推。两轮实验必须与 perf_main.csv 用同一个终点，
# 否则「完整模型」在消融表里是一套数字、在回测表里是另一套。
END = latest_data_date(ROOT / "data" / "cache")

CONFIGS: dict[str, dict] = {
    "A. 完整模型(默认)": {},
    # 三项都设 1.0 即"永远满仓"；regime_low 传 -1 是为了让第三档也命中（历史写法，保留可复现）
    "B. 关闭市场状态闸门": {"expo_high": 1.0, "expo_mid": 1.0, "expo_low": 1.0, "regime_low": -1.0},
    "C. 开启回撤熔断": {"dd_trigger": 0.10},
    "D. 关闭排名缓冲带": {"buffer_mult": 1},
    "E. 关闭趋势过滤(MA60)": {"require_trend": False},
    "F. 关闭拥挤度过滤": {"max_vr": 99.0},
    "G. 改用波动率倒数加权": {"weight_mode": "inv_vol"},
    "H. 调仓周期 10 日": {"rebalance_freq": 10},
    "I. 调仓周期 20 日": {"rebalance_freq": 20},
    # 第三档默认已关闭，这里显式传 regime_low 才能复现"三档"实验
    "J. 防守档位 0/0.5/1 → 0.3/0.6/1": {"expo_high": 1.0, "expo_mid": 0.6,
                                        "expo_low": 0.3, "regime_low": 0.35},
}


def run(panel, bench, cfg, start, end):
    s = TrendGatedAdaptive(panel, **cfg)
    t = s.generate()
    r = Backtester(panel.close, panel.open_).run(t, start=start, end=end)
    b = bench[bench.index >= pd.Timestamp(start)]
    st = metrics.summarize(r.nav, b)
    st["年均换手"] = r.stats["年均换手(单边)"]
    return st.to_dict()


def main() -> None:
    panel = load_panel(ROOT / "data" / "etf_pool.csv", ROOT / "data" / "cache", PANEL_START, END)
    bench = benchmark_series(panel.close, "sh510300")

    print("=" * 96)
    print(f"全样本消融  {START} ~ {END}")
    print("=" * 96)
    rows = {}
    for name, cfg in CONFIGS.items():
        rows[name] = run(panel, bench, cfg, START, END)
        r = rows[name]
        print(f"{name:<32} CAGR {r['年化收益(CAGR)']:>7.2%}  Sharpe {r['夏普比率']:>5.2f}  "
              f"MaxDD {r['最大回撤']:>7.2%}  Calmar {r['卡玛比率']:>5.2f}  换手 {r['年均换手']:>4.2f}")

    df = pd.DataFrame(rows).T
    df.to_csv(ROOT / "output" / "ablation.csv", encoding="utf-8-sig")

    print("\n" + "=" * 96)
    print("子样本稳健性（完整模型）")
    print("=" * 96)
    # 只打印、不落盘：页面上「实验三」读的是 grid2.py 产出的 subsample2.csv
    # （含纯动量对照列，口径更全）。这里原来还写过一份 output/subsample.csv，
    # 列名不同、没有任何消费方，纯属孤儿产物——留着只会让下一个人读错表。
    subs = [("前段 2019-2022", "2019-01-02", "2022-12-30"), ("后段 2023-2026", "2023-01-03", END)]
    for label, s0, s1 in subs:
        r = run(panel, bench, {}, s0, s1)
        b = bench[(bench.index >= pd.Timestamp(s0)) & (bench.index <= pd.Timestamp(s1))]
        print(f"{label}: 策略 CAGR {r['年化收益(CAGR)']:>7.2%} Sharpe {r['夏普比率']:>5.2f} "
              f"MaxDD {r['最大回撤']:>7.2%} | 基准 CAGR {metrics.cagr(b):>7.2%} MaxDD {metrics.max_drawdown(b):>7.2%}")


if __name__ == "__main__":
    main()
