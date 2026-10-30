"""
A/B：缠论最机械化的部分——「按笔交易」（笔向上持有、笔向下离场）。

为什么不测完整的缠论
--------------------
线段、中枢、背驰、三类买卖点都依赖流派约定（是否处理包含、中枢如何升级、
背驰用面积还是峰值、MACD 参数…），不同实现给出不同信号。**在这种自由度下做回测，
测出来的不是缠论，而是某一版实现**。所以这里只测它的地基：

* K线包含 → 分型 → 笔（规则见 `etfquant/chan.py`，显式写死）；
* 状态 = 当前位于"向上笔"还是"向下笔"；向上持有、向下空仓；
* 笔端点确认滞后按实测的 1~2 个交易日处理（`CONFIRM_DELAY`），
  避免用未来数据——这是本实验的公平性底线。

两个用法：单只沪深300ETF（全仓进出）、全池扫描（当前处于向上笔者等权）。

用法：python scripts/research/ab_chan.py
产物：output/experiments/ab_chan.csv
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from etfquant import chan as C  # noqa: E402
from etfquant import metrics  # noqa: E402
from etfquant.backtest import Backtester  # noqa: E402
from etfquant.io_utils import atomic_to_csv  # noqa: E402
from etfquant.pipeline import benchmark_series, load_panel  # noqa: E402
from etfquant.strategy import TrendGatedAdaptive  # noqa: E402

PANEL_START, END = "2018-01-02", "2026-12-31"
MAIN_START = "2019-01-02"
SUB_A, SUB_B = ("2020-01-02", "2022-12-31"), ("2023-01-03", None)
CONFIRM_DELAY = 2          # 实测中位 1、p90 2 个交易日


def state_series(high: pd.Series, low: pd.Series, delay: int = CONFIRM_DELAY) -> pd.Series:
    """
    逐日的"是否处于向上笔"状态，带确认滞后。

    用全样本笔端点 + 固定滞后近似实时：前提是**笔端点不重绘**（本项目实测 0%），
    否则这种近似就是自欺。滞后期间沿用上一次确认的状态。
    """
    df = pd.DataFrame({"high": high, "low": low}).dropna()
    merged = C.merge_bars(df)
    pv = C.pivots(C.fractals(merged))
    if len(pv) < 2:
        return pd.Series(False, index=df.index)
    idx = df.index
    state = pd.Series(pd.NA, index=idx, dtype="object")
    for i in range(len(pv) - 1):
        a = idx.searchsorted(pv[i]["date"], side="right") - 1 + delay
        b = idx.searchsorted(pv[i + 1]["date"], side="right") - 1 + delay
        a = max(a, 0)
        b = min(max(b, a), len(idx))
        up = pv[i]["kind"] == "bottom"      # 底 → 顶 是向上笔
        state.iloc[a:b] = up
    return state.ffill().fillna(False).astype(bool)


def main() -> int:
    print("[1/3] 加载面板…")
    panel = load_panel(ROOT / "data" / "etf_pool.csv", ROOT / "data" / "cache", PANEL_START, END)
    close, high, low = panel.close, panel.high, panel.low
    bt = Backtester(close, panel.open_)
    bench = benchmark_series(close, "sh510300")

    print(f"[2/3] 计算每只标的的笔状态…")
    t0 = time.time()
    states_off, states_on = {}, {}
    for tk in close.columns:
        so = state_series(high[tk], low[tk])
        if so.notna().any():
            states_off[tk] = so.reindex(close.index).fillna(False)
        sn = C.online_states(high[tk].dropna(), low[tk].dropna())
        states_on[tk] = sn.reindex(close.index).fillna(False)
    states = states_on
    print(f"      {len(states_on)} 只有效（{time.time()-t0:.0f}s）")

    # 单只：沪深300ETF 全仓进出
    st300 = states["sh510300"]
    sig_single, prev = {}, None
    for d, flag in st300.items():
        if prev is None or prev != flag:
            sig_single[d] = {"sh510300": 1.0} if flag else {}
        prev = flag

    # 全池：当前处于向上笔者等权
    sig_pool, prev_set = {}, None
    for d in close.index:
        on = [tk for tk, s in states.items() if s.get(d, False)]
        w = {tk: 1.0 / len(on) for tk in on} if on else {}
        if prev_set is None or set(w) != set(prev_set):
            sig_pool[d] = w
        prev_set = w

    print(f"      单只版 {len(sig_single)} 次调仓；全池版 {len(sig_pool)} 次调仓")

    # 对照组：离线全样本端点版（**含未来函数**，仅用于量化"用了未来数据能虚高多少"）
    off_single, prev = {}, None
    for d, flag in states_off["sh510300"].items():
        if prev is None or prev != flag:
            off_single[d] = {"sh510300": 1.0} if flag else {}
        prev = flag
    off_pool, prev_set = {}, None
    for d in close.index:
        on = [tk for tk, st in states_off.items() if st.get(d, False)]
        w = {tk: 1.0 / len(on) for tk in on} if on else {}
        if prev_set is None or set(w) != set(prev_set):
            off_pool[d] = w
        prev_set = w

    tgar = TrendGatedAdaptive(panel).generate()

    print("[3/3] 回测…")
    rows = []
    for label, sig in [("缠论在线 · 单只沪深300ETF", sig_single),
                       ("缠论在线 · 全池扫描", sig_pool),
                       ("【未来函数】离线端点·单只", off_single),
                       ("【未来函数】离线端点·全池", off_pool),
                       ("本项目 TGAR（对照）", tgar)]:
        rec = {"策略": label}
        for wname, (s, e) in {"主窗口": (MAIN_START, None), "2020-22": SUB_A, "2023-26": SUB_B}.items():
            r = bt.run(sig, start=s, end=e)
            b = bench.loc[r.nav.index[0]:r.nav.index[-1]]
            st = metrics.summarize(r.nav, b)
            rec[wname] = {"CAGR": float(st["年化收益(CAGR)"]), "MDD": float(st["最大回撤"]),
                          "夏普": float(st["夏普比率"])}
        r = bt.run(sig, start=MAIN_START)
        rec["换手"] = float(r.stats.get("年均换手(单边)", float("nan")))
        rec["在场天数比"] = float((r.weights.abs().sum(axis=1) > 1e-9).mean())
        rows.append(rec)

    brow = {"策略": "沪深300 买入持有", "换手": 0.0, "在场天数比": 1.0}
    for wname, (s, e) in {"主窗口": (MAIN_START, None), "2020-22": SUB_A, "2023-26": SUB_B}.items():
        seg = bench.loc[s:e].dropna()
        brow[wname] = {"CAGR": float((seg.iloc[-1] / seg.iloc[0]) ** (252 / len(seg)) - 1),
                       "MDD": float((seg / seg.cummax() - 1).min()),
                       "夏普": float(metrics.sharpe(seg))}
    rows.append(brow)

    df = pd.DataFrame(rows)
    atomic_to_csv(df, ROOT / "output" / "experiments" / "ab_chan.csv", index=False, encoding="utf-8-sig")

    print(f"\n=== 主窗口 {MAIN_START} ~ 2026-10-29 ===")
    print(f"{'策略':<24}{'CAGR':>8}{'回撤':>9}{'夏普':>7}{'换手':>7}{'在场':>7}{'2020-22':>9}{'2023-26':>9}")
    for r in df.to_dict("records"):
        m = r["主窗口"]
        print(f"{r['策略']:<24}{m['CAGR']*100:>7.1f}%{m['MDD']*100:>8.1f}%{m['夏普']:>7.2f}"
              f"{r['换手']:>7.1f}{r['在场天数比']*100:>6.0f}%"
              f"{r['2020-22']['CAGR']*100:>8.1f}%{r['2023-26']['CAGR']*100:>8.1f}%")
    print(f"\n[OK] 已写 output/experiments/ab_chan.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
