"""收益归因：把组合收益拆成 Beta / 择时 / 选股 / 债券 / 成本。

为什么不用"昨日权重 × 收盘收益率"近似
------------------------------------
调仓日是 T+1 开盘成交：旧持仓先吃一个隔夜跳空，再按新权重吃开盘→收盘。
用收盘价近似会把这两段混在一起，实测累计偏差可达 60 个百分点。
所以这里直接读 `BacktestResult.daily` —— 回测器逐日记录的
`gap`（跳空段收益）、`oc`（开盘→收盘段收益）与两段各自使用的权重。

分解恒等式（逐日，精确成立）
--------------------------
    pr   = eq_gap + (1+gap)*eq_oc + bond_gap + (1+gap)*bond_oc
    eq   = eq_gap + (1+gap)*eq_oc
    bench= bench_gap + (1+gap)*bench_oc
    eq   = 选股 + w_eq * bench
    w_eq * bench = Beta + 择时              （Beta 用平均权益仓位）
    net  = pr - cost

其中：
    Beta  = 平均权益仓位 × 等权基准收益      —— "什么都不选，按平均仓位拿市场"
    择时  = (当期权益仓位 − 平均仓位) × 基准 —— 闸门加减仓带来的超额
    选股  = 权益腿收益 − w_eq × 基准         —— 从 260 只里挑 3 只的贡献
    债券  = 债券腿收益                       —— 防御容器
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from etfquant.metrics import cagr as _cagr

TRADING_DAYS = 252


def _sparse_dot(weights: dict[str, float], returns: pd.Series) -> float:
    if not weights:
        return 0.0
    idx = [c for c in weights if c in returns.index]
    if not idx:
        return 0.0
    return float(sum(weights[c] * float(returns[c]) for c in idx if pd.notna(returns[c])))


def decompose(
    daily: list[dict],
    close: pd.DataFrame,
    open_: pd.DataFrame,
    eq_cols: list[str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """返回 (逐日分项, 逐日逐标的贡献)。

    分项列：beta/timing/selection/bond/gross；贡献表列：date/code/pnl。
    """
    px = close.ffill()
    op = open_.reindex_like(px).ffill().fillna(px)
    gap_ret = (op / px.shift(1) - 1.0).replace([np.inf, -np.inf], np.nan)
    oc_ret = (px / op - 1.0).replace([np.inf, -np.inf], np.nan)
    cc_ret = (px / px.shift(1) - 1.0).replace([np.inf, -np.inf], np.nan)
    eq_set = set(eq_cols)

    rows, contrib_rows = [], []
    for rec in daily:
        dt = rec["date"]
        if dt not in gap_ret.index:
            continue
        # 非调仓日回测器用的是"收盘→收盘"，调仓日才是"跳空 + 开盘→收盘"。
        # 口径必须跟着记录走，否则会漏掉（或凭空造出）隔夜段。
        exec_day = rec.get("kind") == "exec"
        g_r = gap_ret.loc[dt].dropna()
        o_r = (oc_ret if exec_day else cc_ret).loc[dt].dropna()
        w_gap = rec.get("w_gap") or {}
        w_oc = rec.get("w_oc") or {}
        gap_t = float(rec.get("gap") or 0.0)

        eq_gap = _sparse_dot({k: v for k, v in w_gap.items() if k in eq_set}, g_r)
        bd_gap = _sparse_dot({k: v for k, v in w_gap.items() if k not in eq_set}, g_r)
        eq_oc = _sparse_dot({k: v for k, v in w_oc.items() if k in eq_set}, o_r)
        bd_oc = _sparse_dot({k: v for k, v in w_oc.items() if k not in eq_set}, o_r)

        eq_dates = [c for c in g_r.index if c in eq_set]
        bench_gap = float(g_r.reindex(eq_dates).mean()) if eq_dates else 0.0
        bench_oc = float(o_r.reindex(eq_dates).mean()) if eq_dates else 0.0
        bench = bench_gap + (1.0 + gap_t) * bench_oc

        eq = eq_gap + (1.0 + gap_t) * eq_oc
        bond = bd_gap + (1.0 + gap_t) * bd_oc
        w_eq = sum(v for k, v in w_oc.items() if k in eq_set)

        rows.append({"date": dt, "w_eq": w_eq, "bench": bench,
                     "equity": eq, "bond": bond, "gap": gap_t})

        # 逐标的贡献：跳空段用旧权重，开盘→收盘段用新权重（并乘上跳空系数）
        for code in set(w_gap) | set(w_oc):
            pnl = 0.0
            if code in w_gap and code in g_r.index and pd.notna(g_r[code]):
                pnl += w_gap[code] * float(g_r[code])
            if code in w_oc and code in o_r.index and pd.notna(o_r[code]):
                pnl += (1.0 + gap_t) * w_oc[code] * float(o_r[code])
            if pnl:
                contrib_rows.append({"date": dt, "code": code, "pnl": pnl})

    df = pd.DataFrame(rows).set_index("date").sort_index()
    avg_w = float(df["w_eq"].mean())
    df["beta"] = avg_w * df["bench"]
    df["timing"] = (df["w_eq"] - avg_w) * df["bench"]
    df["selection"] = df["equity"] - df["w_eq"] * df["bench"]
    df["bond"] = df["bond"]
    df["gross"] = df["equity"] + df["bond"]
    df["avg_w"] = avg_w
    return df, pd.DataFrame(contrib_rows)


def with_cost(comp: pd.DataFrame, cost: pd.Series) -> pd.DataFrame:
    """成本按回测的乘法口径折算成可加项。

    回测里是 nav *= (1+gap) * (1-cost) * (1+oc)，所以
        net = (1+gross)(1-cost) - 1 = gross - cost*(1+gross)
    直接写 -cost 会漏掉 cost×gross 的交叉项，让净收益与净值差几个 bp。
    """
    comp = comp.copy()
    c = cost.reindex(comp.index).fillna(0.0)
    comp["cost"] = -c * (1.0 + comp["gross"])
    comp["net"] = comp["gross"] + comp["cost"]
    return comp


def annualize(series: pd.Series) -> float:
    return float(series.mean() * TRADING_DAYS)


def summarize(comp: pd.DataFrame) -> dict:
    """算术年化分项；`residual` 应恒为 0（恒等式），留作自检。"""
    parts = {k: annualize(comp[k]) for k in ("beta", "timing", "selection", "bond", "cost")}
    parts["sum"] = sum(parts.values())
    parts["gross_arith"] = annualize(comp["gross"])
    parts["net_arith"] = annualize(comp["net"])
    parts["residual"] = parts["sum"] - parts["net_arith"]
    parts["avg_equity_weight"] = float(comp["avg_w"].iloc[0])
    return parts


def cagr_from_returns(net: pd.Series) -> float:
    """与 metrics.cagr 同一口径（日历天数），避免报告里出现两个 CAGR。"""
    nav = (1.0 + net.fillna(0.0)).cumprod()
    return float(_cagr(nav))


def block_bootstrap(
    comp: pd.DataFrame, cols=("beta", "timing", "selection", "bond", "cost"),
    block: int = 20, n: int = 2000, seed: int = 7,
) -> dict[str, list[float]]:
    """循环块自助法：保留收益的自相关，给每个分项一个区间。"""
    rng = np.random.default_rng(seed)
    idx = np.arange(len(comp))
    out: dict[str, list[float]] = {c: [] for c in cols}
    nb = int(np.ceil(len(comp) / block))
    for _ in range(n):
        starts = rng.integers(0, len(comp) - block, size=nb)
        pick = np.concatenate([idx[s:s + block] for s in starts])
        sample = comp.iloc[pick]
        for c in cols:
            out[c].append(annualize(sample[c]))
    return out


def episodes_from_trades(trades: pd.DataFrame, last_date: pd.Timestamp) -> pd.DataFrame:
    """把调仓记录还原成持仓段：(code, start, end)，end 为清仓日或数据末日。"""
    rows = []
    if trades is None or trades.empty:
        return pd.DataFrame(columns=["code", "start", "end"])
    for code, g in trades.groupby("code"):
        g = g.sort_values("date")
        start = None
        for _, r in g.iterrows():
            if r["w_from"] <= 0 < r["w_to"]:
                start = r["date"]
            elif start is not None and r["w_from"] > 0 >= r["w_to"]:
                rows.append({"code": code, "start": start, "end": r["date"]})
                start = None
        if start is not None:
            rows.append({"code": code, "start": start, "end": last_date})
    return pd.DataFrame(rows)


def episode_pnl(contrib: pd.DataFrame, episodes: pd.DataFrame) -> pd.DataFrame:
    """把逐日贡献按持仓段汇总，得到每段的累计贡献。"""
    if episodes.empty or contrib.empty:
        return episodes.assign(pnl=[])
    parts = []
    for ep in episodes.itertuples():
        m = (contrib["code"] == ep.code) & (contrib["date"] >= ep.start) & (contrib["date"] < ep.end)
        parts.append(float(contrib.loc[m, "pnl"].sum()))
    out = episodes.copy()
    out["pnl"] = parts
    return out


def concentration(comp: pd.DataFrame, contrib: pd.DataFrame, episodes: pd.DataFrame) -> dict:
    """收益集中度：前 5% 持仓段的贡献占比，以及剔除最好 k 段后的 CAGR。"""
    ep = episode_pnl(contrib, episodes)
    if ep.empty or ep["pnl"].sum() <= 0:
        return {"n_episodes": int(len(ep)), "top5_share": None, "drop": []}
    total = float(ep["pnl"].sum())
    k = max(1, int(np.ceil(len(ep) * 0.05)))
    top5 = float(ep["pnl"].nlargest(k).sum()) / total

    base_cagr = cagr_from_returns(comp["net"])
    drop = []
    ranked = ep.sort_values("pnl", ascending=False)
    for kk in (1, 5, 10):
        if kk > len(ranked):
            break
        victims = ranked.head(kk)
        mask = pd.Series(False, index=contrib.index)
        for v in victims.itertuples():
            mask |= ((contrib["code"] == v.code) & (contrib["date"] >= v.start)
                     & (contrib["date"] < v.end))
        adj = comp["net"] - contrib.loc[mask].groupby("date")["pnl"].sum().reindex(comp.index).fillna(0.0)
        drop.append({"k": kk, "cagr": cagr_from_returns(adj)})
    return {"n_episodes": int(len(ep)), "top5_share": top5,
            "base_cagr": base_cagr, "drop": drop}


def break_even_cost(comp: pd.DataFrame, bench_cagr: float) -> dict:
    """成本翻多少倍时 CAGR 归零 / 跌到基准以下（成本按换手线性缩放）。"""
    cost = -comp["cost"].fillna(0.0)

    def cagr_at(k: float) -> float:
        return cagr_from_returns(comp["gross"] - k * cost)

    def solve(target: float) -> float | None:
        lo, hi = 0.0, 20.0
        if cagr_at(hi) > target:
            return None
        for _ in range(60):
            mid = (lo + hi) / 2
            if cagr_at(mid) > target:
                lo = mid
            else:
                hi = mid
        return float(hi)

    return {
        "zero": solve(0.0),
        "below_benchmark": solve(bench_cagr),
        "cagr_at_1x": cagr_at(1.0),
        "cagr_at_2x": cagr_at(2.0),
        "cagr_at_3x": cagr_at(3.0),
    }
