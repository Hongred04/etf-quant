"""
事件驱动回测引擎（Event-driven Backtester）。

成交假设（贴近 A 股 ETF 实盘）
------------------------------
* 信号时点：调仓日 T 收盘后，使用 <= T 的全部历史数据计算因子；
* 成交时点：T+1 开盘价成交（避免"收盘价成交"带来的隐性前视）；
* 交易成本：单边 `fee`（佣金+过户，默认 0.03%）+ `slippage`（冲击成本，默认 0.07%），
  合计 0.10%/边，与主流 ETF 回测假设一致（参考项目采用 0.15%/边）；
* 权重漂移：非调仓日权重随价格自然漂移，不主动再平衡（降低无效换手）；
* 停牌处理：价格向前填充，避免 NaN 传染。

不做空、不加杠杆，权重和 <= 1，剩余部分视为现金（收益 0）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class BacktestResult:
    nav: pd.Series
    weights: pd.DataFrame
    turnover: pd.Series
    cost: pd.Series
    holdings: pd.DataFrame
    stats: dict = field(default_factory=dict)
    trades: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(
        columns=["date", "code", "w_from", "w_to", "delta"]))
    # 逐日收益分解记录（只记录，不参与记账）：
    # 每个交易日一行，含当日毛利 pr、跳空段 gap、开盘→收盘段 oc，
    # 以及两段各自使用的权重（稀疏 dict）。
    # 归因脚本据此做精确拆解；没这一步就只能用"昨日权重 × 收盘收益率"近似，
    # 而执行日的开盘成交会让近似产生系统性偏差（实测累计 62 个百分点）。
    daily: list = field(default_factory=list)
    # 逐日收益分解记录（只记录，不参与记账）：
    # 每个交易日一行，含当日毛利 pr、跳空段 gap、开盘→收盘段 oc，
    # 以及两段各自使用的权重（稀疏 dict）。
    # 归因脚本据此做精确拆解；没这一步就只能用"昨日权重 × 收盘收益率"近似，
    # 而执行日的开盘成交会让近似产生系统性偏差（实测累计 62 个百分点）。
    daily: list = field(default_factory=list)

    @property
    def returns(self) -> pd.Series:
        return self.nav.pct_change(fill_method=None).dropna()


class Backtester:
    def __init__(
        self,
        close: pd.DataFrame,
        open_: pd.DataFrame | None = None,
        fee: float = 0.0003,
        slippage: float = 0.0007,
        cb_tickers: set[str] | None = None,
        cb_extra: float = 0.0,
    ):
        self.close = close.sort_index().ffill()
        self.open = (open_.sort_index().ffill() if open_ is not None else self.close)
        self.cost_per_side = fee + slippage
        # 跨境 ETF 的额外单边成本，用于量化"场内溢价"这种拿不到 IOPV 的损耗。
        # 默认 0：不影响任何既有回测。只在做敏感性测试时传入。
        self.cb_tickers = set(cb_tickers or ())
        self.cb_extra = float(cb_extra)

    def _cb_turn(self, a: pd.Series, b: pd.Series, cols) -> float:
        """换手中来自跨境标的的单边部分。溢价损耗只发生在它们身上。"""
        if self.cb_extra <= 0 or not self.cb_tickers:
            return 0.0
        idx = [c for c in cols if c in self.cb_tickers]
        if not idx:
            return 0.0
        return 0.5 * float((a.reindex(idx).fillna(0.0) - b.reindex(idx).fillna(0.0)).abs().sum())

    def run(
        self,
        targets: dict[pd.Timestamp, dict[str, float]],
        start: str | None = None,
        end: str | None = None,
        initial_cash: float = 1.0,
    ) -> BacktestResult:
        """
        参数
        ----
        targets : {信号日: {ticker: weight}}，信号日收盘生成，次一交易日开盘执行。
        """
        px = self.close
        dates = px.index
        if start:
            dates = dates[dates >= pd.Timestamp(start)]
        if end:
            dates = dates[dates <= pd.Timestamp(end)]
        if len(dates) < 3:
            raise ValueError("回测区间过短")

        cols = list(px.columns)
        # 信号日 -> 执行日（下一个交易日）
        idx_map = {d: i for i, d in enumerate(px.index)}
        exec_targets: dict[pd.Timestamp, dict[str, float]] = {}
        for sig_date, w in targets.items():
            sig_date = pd.Timestamp(sig_date)
            if sig_date not in idx_map:
                continue
            j = idx_map[sig_date] + 1
            if j >= len(px.index):
                continue
            exec_targets[px.index[j]] = w

        w = pd.Series(0.0, index=cols)
        nav = initial_cash
        navs, w_rows, turns, costs, hold_rows = [], [], [], [], []
        trade_rows: list[dict] = []
        daily_rows: list[dict] = []

        def _sparse(sr: pd.Series) -> dict[str, float]:
            return {k: round(float(v), 8) for k, v in sr.items() if abs(float(v)) > 1e-8}
        daily_rows: list[dict] = []

        def _sparse(sr: pd.Series) -> dict[str, float]:
            return {k: round(float(v), 8) for k, v in sr.items() if abs(float(v)) > 1e-8}

        def _record(dt, before: pd.Series, after: pd.Series) -> None:
            """记录本次调仓中每只标的的权重变化（仅记有变动的）。"""
            b = before.reindex(cols).fillna(0.0)
            a = after.reindex(cols).fillna(0.0)
            d = a - b
            for c in cols:
                if abs(float(d[c])) > 1e-6:
                    trade_rows.append({
                        "date": dt, "code": c,
                        "w_from": round(float(b[c]), 6),
                        "w_to": round(float(a[c]), 6),
                        "delta": round(float(d[c]), 6),
                    })

        for i, dt in enumerate(dates):
            if i == 0:
                # 首个交易日：若为执行日则按开盘价建仓，并计入当日 open→close 涨跌。
                # 原实现只扣成本、不算收益，等于吞掉首日行情（满仓 +5% 时净值仍是 1.0）；
                # 而且"扣成本不记收益"是单向偏低估。
                if dt in exec_targets:
                    tgt = self._norm(exec_targets[dt])
                    # 必须先把目标权重对齐到全部标的再相减：
                    # tgt 只含非零权重的标的，直接 tgt - w 会让"已清仓标的"变成 NaN 被跳过，
                    # 从而漏算买入新标的与清仓的成交额，换手会被严重低估。
                    turn = 0.5 * float((tgt.reindex(cols).fillna(0.0) - w).abs().sum())
                    # 实际成交金额 = Σ|Δw| = 2 × 单边换手，成本按成交金额计
                    # 跨境部分另加 cb_extra：场内溢价拿不到 IOPV，只能以等价成本近似
                    cb_turn = self._cb_turn(tgt.reindex(cols).fillna(0.0), w, cols)
                    trade_cost = 2.0 * turn * self.cost_per_side + 2.0 * cb_turn * self.cb_extra
                    nav *= 1 - trade_cost
                    _record(dt, w, tgt)
                    # 必须补齐到全标的索引，否则 w 会退化成稀疏索引，
                    # 下一轮计算换手时缺失标的按 NaN 被跳过，买入/清仓成交额将漏算。
                    w = tgt.reindex(cols).fillna(0.0)

                    r0 = (px.loc[dt] / self.open.loc[dt] - 1.0).replace(
                        [np.inf, -np.inf], np.nan).fillna(0.0)
                    pr0 = float((w * r0).sum())
                    nav *= 1.0 + pr0
                    if pr0 != -1.0:
                        w = (w * (1.0 + r0) / (1.0 + pr0)).reindex(cols).fillna(0.0)
                    turns.append((dt, turn))
                    costs.append((dt, trade_cost))
                    daily_rows.append({"date": dt, "pr": pr0, "gap": 0.0,
                                       "oc": pr0, "kind": "exec", "w_oc": _sparse(w)})
                else:
                    turns.append((dt, 0.0))
                    costs.append((dt, 0.0))
                    daily_rows.append({"date": dt, "pr": 0.0, "gap": 0.0,
                                       "oc": 0.0, "kind": "exec", "w_oc": {}})
                navs.append((dt, nav))
                w_rows.append(w.copy())
                hold_rows.append(self._snapshot(dt, w, px, nav))
                continue

            prev = dates[i - 1]
            c_prev = px.loc[prev]
            c_now = px.loc[dt]
            o_now = self.open.loc[dt]

            if dt in exec_targets:
                # 1) 开盘前按跳空缺口漂移旧权重
                gap = (o_now / c_prev - 1.0).replace([np.inf, -np.inf], np.nan).fillna(0.0)
                w_gap_snapshot = w.copy()
                v_gap = 1.0 + float((w * gap).sum())
                # 关键：先让净值承担隔夜跳空，再把旧权重归一化。
                # 原实现只归一化权重、不给 nav 乘 v_gap，等于把调仓日开盘前的
                # 盈亏整段丢掉（隔夜下跌时高估收益，上涨时低估）。实测这段
                # 年均 −4.0pp，修复后 CAGR 从 17.4% 降到 ~12.6%。
                if v_gap > 0:
                    nav *= v_gap
                    w = w * (1.0 + gap) / v_gap
                else:
                    nav = 0.0
                    w = w * 0.0

                # 2) 调仓到目标权重
                tgt = self._norm(exec_targets[dt])
                turn = 0.5 * float((tgt.reindex(cols).fillna(0.0) - w).abs().sum())
                # 实际成交金额 = Σ|Δw| = 2 × 单边换手，成本按成交金额计
                cb_turn = self._cb_turn(tgt.reindex(cols).fillna(0.0), w, cols)
                trade_cost = 2.0 * turn * self.cost_per_side + 2.0 * cb_turn * self.cb_extra
                nav *= 1.0 - trade_cost
                _record(dt, w, tgt)
                w = tgt.reindex(cols).fillna(0.0)

                # 3) 当日收益：开盘 -> 收盘
                r = (c_now / o_now - 1.0).replace([np.inf, -np.inf], np.nan).fillna(0.0)
                pr = float((w * r).sum())
                turns.append((dt, turn))
                costs.append((dt, trade_cost))
                daily_rows.append({"date": dt, "pr": pr, "gap": float(v_gap - 1.0),
                                   "oc": pr, "kind": "exec",
                                   "w_gap": _sparse(w_gap_snapshot), "w_oc": _sparse(w)})
            else:
                r = (c_now / c_prev - 1.0).replace([np.inf, -np.inf], np.nan).fillna(0.0)
                pr = float((w * r).sum())
                turns.append((dt, 0.0))
                costs.append((dt, 0.0))
                daily_rows.append({"date": dt, "pr": pr, "gap": 0.0,
                                   "oc": pr, "kind": "cc", "w_oc": _sparse(w)})

            nav *= 1.0 + pr
            if pr != -1.0:
                w = (w * (1.0 + r) / (1.0 + pr)).reindex(cols).fillna(0.0)
            navs.append((dt, nav))
            w_rows.append(w.copy())
            hold_rows.append(self._snapshot(dt, w, px, nav))

        nav_s = pd.Series(dict(navs)).sort_index()
        w_df = pd.DataFrame(w_rows, index=nav_s.index).fillna(0.0)
        turn_s = pd.Series(dict(turns)).sort_index().fillna(0.0)
        cost_s = pd.Series(dict(costs)).sort_index().fillna(0.0)

        stats = {
            "调仓次数": int((turn_s > 0).sum()),
            "年均换手(单边)": float(turn_s.sum() / max(len(nav_s) / 252, 1e-9)),
            "累计交易成本": float(cost_s.sum()),
            "期末净值": float(nav_s.iloc[-1]),
        }
        trades_df = pd.DataFrame(trade_rows)
        if not trades_df.empty:
            trades_df["date"] = pd.to_datetime(trades_df["date"])
        return BacktestResult(nav_s, w_df, turn_s, cost_s, pd.DataFrame(hold_rows),
                              stats, trades_df, daily_rows)

    @staticmethod
    def _norm(w: dict[str, float]) -> pd.Series:
        if not w:
            return pd.Series(dtype=float)
        s = pd.Series({k: max(float(v), 0.0) for k, v in w.items()})
        s = s[s > 0]
        tot = s.sum()
        if tot > 1.0:
            s = s / tot
        return s

    def _snapshot(self, dt, w: pd.Series, px: pd.DataFrame, nav: float) -> dict:
        held = w[w > 1e-6]
        return {
            "date": dt,
            "nav": nav,
            "n_hold": int(len(held)),
            "cash_weight": float(max(0.0, 1.0 - w.sum())),
            "top_holding": held.idxmax() if len(held) else "",
            "top_weight": float(held.max()) if len(held) else 0.0,
        }
