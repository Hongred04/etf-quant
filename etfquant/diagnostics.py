"""
对**已有主线**的深度诊断。这里不产生任何新策略，只回答四个问题：

1. 信号本身有没有预测力？（rank IC + Newey-West t + 衰减曲线）
2. 17.3% 是 alpha 还是 beta？（用**自己的池子**造多空动量因子再回归）
3. 执行延迟吃掉多少？能装多少钱？（T+1 隔夜缺口 + ADV 容量）
4. 这个数字的误差棒有多宽？（块 bootstrap）

为什么单独放一层
----------------
`overfit.py` 回答的是"这个数字有多少来自挑选"（PBO/DSR/滚动前向），
`metrics.py` 回答的是"这条净值曲线长什么样"。
两者都不回答**"为什么"**——所以 README 里写了两年"核心 alpha 来自动量"，
却从来没有一个统计量支撑这句话。这一层补的就是那个缺口。

设计约束
--------
* 全是纯函数：输入 DataFrame / Series，输出 DataFrame / dict，不读文件、不写盘；
* 所有滚动量只向前看（`shift` 之后才用），多空因子的权重 `shift(1)` 生效；
* 回归与 t 统计量手写（numpy + HAC），不引入 statsmodels——依赖越少，
  这些数字越容易被别人独立复算；
* 需要 `min_names` 保护：横截面太小的时候算出来的 IC/排名全是噪声。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "fwd_return",
    "rowwise_spearman",
    "newey_west_t",
    "ic_stats",
    "ic_decay",
    "quantile_spread",
    "tail_spread",
    "tail_spread_series",
    "quantile_gap_series",
    "month_ends",
    "momentum_factor",
    "ols_hac",
    "factor_regression",
    "block_bootstrap",
    "snapshot_rows",
    "trend_test",
    "period_summary",
    "execution_gap",
    "capacity",
    "equity_exposure",
    "exposure_split",
]


# --------------------------------------------------------------------------- #
# 一、信号层：IC 与衰减
# --------------------------------------------------------------------------- #
def fwd_return(close: pd.DataFrame, horizon: int, entry: pd.DataFrame | None = None) -> pd.DataFrame:
    """未来 h 个交易日的收益（在 t 日算，末 h 行为 NaN）。

    默认 close→close：衡量"信号对价格有没有预测力"。
    传 `entry=open_.shift(-1)` 则得到 **open(t+1)→close(t+h)** 的"可收割"版本——
    两者之差就是 T+1 延迟的代价，也就是把 IC 从"收盘价能成交"折成"次日开盘能成交"
    要打的那个折。两件事必须分开量化，不能混成一个数字。
    """
    base = close if entry is None else entry
    return close.shift(-horizon) / base - 1.0


def rowwise_spearman(a: pd.DataFrame, b: pd.DataFrame, min_names: int = 20) -> pd.Series:
    """逐行（逐日）的 Spearman 秩相关。两表按列名取交集，任一侧缺失则跳过该标的。

    用秩相关而不是 Pearson：信号是排名，收益有极端值，秩相关对异常值免疫，
    也是业界 IC 的标准定义。
    """
    cols = a.columns.intersection(b.columns)
    A = a[cols].to_numpy(dtype=float)
    B = b[cols].to_numpy(dtype=float)
    out = np.full(len(a), np.nan, dtype=float)
    for i in range(len(a)):
        m = np.isfinite(A[i]) & np.isfinite(B[i])
        n = int(m.sum())
        if n < min_names:
            continue
        ra = pd.Series(A[i][m]).rank().to_numpy()
        rb = pd.Series(B[i][m]).rank().to_numpy()
        if ra.std() == 0 or rb.std() == 0:
            continue
        out[i] = float(np.corrcoef(ra, rb)[0, 1])
    return pd.Series(out, index=a.index)


def newey_west_t(x: pd.Series | np.ndarray, lags: int = 5) -> float:
    """均值的 Newey-West t 统计量。

    为什么不能直接用 t = mean/(std/√n)：IC 序列有自相关（相邻调仓日的信号高度重叠），
    朴素 t 会系统性高估显著性，在 5 日调仓上能虚高 30%~50%。
    """
    arr = np.asarray(x, dtype=float)
    arr = arr[np.isfinite(arr)]
    n = arr.size
    if n < 8:
        return float("nan")
    # 零离散序列的 t 值没有定义。不挡这一条，浮点残差（~1e-18）会被当成
    # 真实方差，算出 1e16 量级的 t——曾经在基准行上输出过这种"天文数字"。
    if float(np.ptp(arr)) < 1e-12:
        return float("nan")
    e = arr - arr.mean()
    var = float(e @ e) / n
    for lag in range(1, min(lags, n - 1) + 1):
        w = 1.0 - lag / (lags + 1.0)
        var += w * 2.0 * float(e[lag:] @ e[:-lag]) / n
    if var <= 0:
        return float("nan")
    return float(arr.mean() / np.sqrt(var / n))


def ic_stats(ic: pd.Series) -> dict:
    """IC 序列的汇总：均值、ICIR、NW t、胜率、样本数。

    经验刻度（日频/周频横截面）：|IC| 0.02 勉强、0.05 可用、0.10 以上很罕见。
    ICIR（= mean/std）比 IC 更重要：均值高但方差大，说明是几天的运气。
    """
    s = pd.Series(ic).dropna()
    if s.empty:
        return {"n": 0, "ic": float("nan"), "icir": float("nan"),
                "t": float("nan"), "hit": float("nan")}
    return {
        "n": int(s.size),
        "ic": float(s.mean()),
        "icir": float(s.mean() / s.std(ddof=1)) if s.std(ddof=1) else float("nan"),
        "t": newey_west_t(s),
        "hit": float((s > 0).mean()),
    }


def ic_decay(
    close: pd.DataFrame,
    signal: pd.DataFrame,
    dates: pd.Index,
    horizons: tuple[int, ...] = (1, 5, 10, 20, 60),
    min_names: int = 20,
) -> pd.DataFrame:
    """信号在不同持有期上的 IC。衰减越快，调仓就该越勤（或越不该做长周期）。

    这条曲线直接回答 7.6 的"12 个月 + 月度"为什么输：如果 IC 在 20 日后就衰减到
    接近 0，那么月度调仓拿的是残值。
    """
    rows = []
    for h in horizons:
        fwd = fwd_return(close, h)
        ic = rowwise_spearman(signal.loc[dates], fwd.reindex(dates), min_names=min_names)
        st = ic_stats(ic)
        rows.append({"horizon": h, **st})
    return pd.DataFrame(rows).set_index("horizon")


# --------------------------------------------------------------------------- #
# 二、因子层：用自己的池子造动量因子，再做归因
# --------------------------------------------------------------------------- #
def month_ends(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """每个自然月的最后一个交易日（取实际存在的交易日）。"""
    s = pd.Series(index, index=index)
    return pd.DatetimeIndex(s.groupby([index.year, index.month]).max().to_numpy())


def momentum_factor(
    close: pd.DataFrame,
    lookback: int = 20,
    skip: int = 5,
    q: float = 0.10,
    min_names: int = 20,
) -> pd.Series:
    """用**本池**构造多空动量因子：每月末按 R20 分位，做多最强 q、做空最弱 q，等权。

    为什么要自己造而不用现成的 UMD：
    * 外部 UMD 是美股/大盘股口径，与本池（A 股主题 ETF）不是同一个市场；
    * 自己造等于问一个更尖锐的问题——**"如果我就照着同一张排名表机械地做多空，
      能拿到多少？"** 剩下的部分才是"挑 3 只 + 闸门"带来的增量。

    权重在月末设定、**次日**生效（`shift(1)`），不存在前视。
    """
    ret = close.pct_change(fill_method=None)
    sig = close.shift(skip) / close.shift(skip + lookback) - 1.0
    w = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    for d in month_ends(close.index):
        row = sig.loc[d].dropna()
        if len(row) < min_names:
            continue
        k = max(1, int(len(row) * q))
        vec = pd.Series(0.0, index=close.columns)
        vec[row.nlargest(k).index] = 1.0 / k
        vec[row.nsmallest(k).index] = -1.0 / k
        w.loc[d] = vec
    w = w.ffill().shift(1).fillna(0.0)
    return (w * ret.fillna(0.0)).sum(axis=1)


def ols_hac(y: np.ndarray, X: np.ndarray, lags: int = 5) -> dict:
    """带 Newey-West 标准误的 OLS（含截距，需由调用方放进 X 第一列）。"""
    y = np.asarray(y, dtype=float)
    X = np.asarray(X, dtype=float)
    n, k = X.shape
    XtX_inv = np.linalg.pinv(X.T @ X)
    beta = XtX_inv @ X.T @ y
    e = y - X @ beta
    Xe = X * e[:, None]
    meat = Xe.T @ Xe
    for lag in range(1, min(lags, n - 1) + 1):
        w = 1.0 - lag / (lags + 1.0)
        G = Xe[lag:].T @ Xe[:-lag]
        meat += w * (G + G.T)
    V = XtX_inv @ meat @ XtX_inv
    se = np.sqrt(np.clip(np.diag(V), 0, None))
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - float((e ** 2).sum()) / ss_tot if ss_tot > 0 else float("nan")
    with np.errstate(divide="ignore", invalid="ignore"):
        t = beta / se
    return {"beta": beta, "se": se, "t": t, "r2": r2, "n": n}


def factor_regression(
    nav: pd.Series,
    factors: pd.DataFrame,
    names: tuple[str, ...] = ("mkt", "mom", "smb"),
) -> dict:
    """策略日收益对因子的回归，alpha 折算成年化。

    解读约定：
    * alpha 的 t 值 < 2 → 没有统计上站得住的超额，收益可以完全由 beta 解释；
    * R² 从"只放 mkt"到"加上 mom"的跳升幅度，说明策略本质上是**动量 beta 的某种包装**。
    """
    ret = nav.pct_change(fill_method=None).dropna()
    X = factors.reindex(ret.index).dropna()
    common = ret.index.intersection(X.index)
    y = ret.loc[common].to_numpy()
    Xm = X.loc[common, list(names)].to_numpy()
    design = np.column_stack([np.ones(len(common)), Xm])
    got = ols_hac(y, design)
    alpha_d = float(got["beta"][0])
    alpha_t = float(got["t"][0])
    out = {
        "n": got["n"],
        "alpha_daily": alpha_d,
        "alpha_ann": float((1 + alpha_d) ** 252 - 1),
        "alpha_t": alpha_t,
        "r2": got["r2"],
    }
    for i, nm in enumerate(names, start=1):
        out[f"beta_{nm}"] = float(got["beta"][i])
        out[f"t_{nm}"] = float(got["t"][i])
    # 只放市场的对照规格：R² 的差额 = 动量 beta 解释了多少
    solo = ols_hac(y, np.column_stack([np.ones(len(common)), Xm[:, 0]]))
    out["r2_mkt_only"] = solo["r2"]
    out["beta_mkt_only"] = float(solo["beta"][1])
    return out


# --------------------------------------------------------------------------- #
# 三、误差棒：块 bootstrap
# --------------------------------------------------------------------------- #
def block_bootstrap(
    returns: pd.Series,
    block: int = 20,
    n_boot: int = 2000,
    seed: int = 7,
    cal_per_day: float = 1.5,
    annualize_days: int = 252,
) -> dict:
    """循环块 bootstrap：给出 CAGR / 最大回撤 / 夏普的置信区间。

    为什么必须分块：日收益有波动率聚集与自相关，独立重采样会把回撤和夏普的分布
    人为压窄（路径变"白噪声"，回撤被系统性低估）。

    `cal_per_day` = 样本跨度日历天数 / 交易日数，用来把年化口径对齐主表
    （本项目 CAGR 一律用日历天数 365.25），否则这里的区间和 headline 对不上。
    """
    r = pd.Series(returns).dropna().to_numpy(dtype=float)
    n = r.size
    if n < block * 5:
        return {"n": 0}
    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(n / block))
    ann, mdd, shp = [], [], []
    for _ in range(n_boot):
        starts = rng.integers(0, n, n_blocks)
        idx = np.concatenate([(s + np.arange(block)) % n for s in starts])[:n]
        path = r[idx]
        nav = np.cumprod(1.0 + path)
        ann.append(nav[-1] ** (365.25 / (n * cal_per_day)) - 1.0)
        peak = np.maximum.accumulate(np.r_[1.0, nav])[1:]
        mdd.append(float((nav / peak).min()) - 1.0)
        sd = path.std(ddof=1)
        shp.append(float(path.mean() / sd * np.sqrt(annualize_days)) if sd > 0 else np.nan)

    def band(a: list[float]) -> dict:
        v = np.asarray(a, dtype=float)
        v = v[np.isfinite(v)]
        return {"p2.5": float(np.percentile(v, 2.5)),
                "p50": float(np.percentile(v, 50)),
                "p97.5": float(np.percentile(v, 97.5))}

    return {
        "n": n,
        "block": block,
        "cagr": band(ann),
        "mdd": band(mdd),
        "sharpe": band(shp),
        "p_cagr_le_0": float(np.mean(np.asarray(ann) <= 0)),
    }


# --------------------------------------------------------------------------- #
# 四、执行与容量
# --------------------------------------------------------------------------- #
def quantile_spread(
    signal: pd.DataFrame,
    fwd: pd.DataFrame,
    dates: pd.Index,
    n_groups: int = 10,
    min_names: int = 50,
) -> pd.Series:
    """按信号分 N 组的未来收益（bp），组 0 最弱。

    这是 IC 的必要补充：**秩相关会把"只有头部有效"的信号判成噪声**。
    R20 就是这种形状——中间几组几乎是平的，全部信息在 Q9/Q0 两端，
    而策略只买前 3 只（横截面的前 1%），所以 IC 不是它的正确摘要。
    """
    groups = np.full((len(dates), n_groups), np.nan)
    for i, d in enumerate(dates):
        row, r = signal.loc[d], fwd.loc[d]
        ok = row.notna() & r.notna()
        if int(ok.sum()) < min_names:
            continue
        q = pd.qcut(row[ok].rank(method="first"), n_groups, labels=False)
        groups[i] = r[ok].groupby(q).mean().reindex(range(n_groups)).to_numpy()
    return pd.Series(np.nanmean(groups, axis=0) * 1e4,
                     index=[f"Q{i}" for i in range(n_groups)], name="fwd_bp")


def quantile_gap_series(
    signal: pd.DataFrame,
    fwd: pd.DataFrame,
    dates: pd.Index,
    n_groups: int = 10,
    min_names: int = 50,
) -> pd.Series:
    """逐调仓日的 Q_top − Q_bottom（bp）。趋势检验需要逐日值，不能只看均值。"""
    out = {}
    for d in dates:
        row, r = signal.loc[d], fwd.loc[d]
        ok = row.notna() & r.notna()
        if int(ok.sum()) < min_names:
            continue
        q = pd.qcut(row[ok].rank(method="first"), n_groups, labels=False)
        means = r[ok].groupby(q).mean()
        if len(means) < n_groups:
            continue
        out[d] = float(means.iloc[-1] - means.iloc[0])
    return pd.Series(out, dtype=float)


def tail_spread_series(
    signal: pd.DataFrame,
    fwd: pd.DataFrame,
    dates: pd.Index,
    top: int = 3,
    min_names: int = 20,
) -> pd.Series:
    """逐调仓日的 "top N 平均 − 全池平均"。给它单独一个函数，是为了能做趋势检验。"""
    out = {}
    for d in dates:
        row, r = signal.loc[d].dropna(), fwd.loc[d]
        if len(row) < min_names:
            continue
        picked = r.reindex(row.nlargest(top).index).dropna()
        pool = r.reindex(row.index).dropna()
        if len(picked) < top or len(pool) < min_names:
            continue
        out[d] = float(picked.mean() - pool.mean())
    return pd.Series(out, dtype=float)


def tail_spread(
    signal: pd.DataFrame,
    fwd: pd.DataFrame,
    dates: pd.Index,
    top: int = 3,
    min_names: int = 20,
) -> dict:
    """被选中的 top 只 vs 全池平均，逐调仓日。

    这是最贴近策略的检验：不看整条横截面，只问"选出来的那 3 只，
    未来 X 日跑赢池子平均多少"。均值与中位数要分别看——
    均值显著为正而中位数≈0，说明收益来自少数几次大赢（动量策略的典型形状）。
    """
    s = tail_spread_series(signal, fwd, dates, top=top, min_names=min_names).dropna()
    if s.empty:
        return {"n": 0}
    naive = s.mean() / (s.std(ddof=1) / np.sqrt(len(s))) if s.std(ddof=1) else float("nan")
    return {
        "n": int(s.size),
        "mean_bp": float(s.mean() * 1e4),
        "median_bp": float(s.median() * 1e4),
        "hit": float((s > 0).mean()),
        "t": float(newey_west_t(s)),
        "t_naive": float(naive),
    }


def trend_test(x: pd.Series, lags: int = 5) -> dict:
    """序列对"经过的年数"做带截距的 OLS（HAC 标准误），看有没有单调漂移。

    为什么需要它
    ------------
    7.10 只说"IC≈0.02、top3 价差 t=1.57"，那是**全样本平均**。
    但"alpha 是否在衰减"问的是**趋势**，不是水平：一个 2019 年很准、
    2026 年完全失效的信号，全样本平均仍然会是个正数。

    **判据（写在这里，跑数据之前定，不跑完再改）：**
    * 斜率 t < −2 → "有衰减证据"；
    * |t| < 1    → "无衰减证据"；
    * 其余       → "弱证据，不下结论"。
    IC、Q9−Q0、top3−全池 三个指标用同一把尺子，避免"挑一个好看的报"。
    """
    s = pd.Series(x).dropna()
    if len(s) < 24 or not isinstance(s.index, pd.DatetimeIndex):
        return {"n": int(len(s)), "slope_per_year": float("nan"),
                "t": float("nan"), "verdict": "样本不足"}
    yrs = (s.index - s.index[0]).days / 365.25
    yrs = np.asarray(yrs, dtype=float)
    if yrs[-1] <= 0:
        return {"n": int(len(s)), "slope_per_year": float("nan"),
                "t": float("nan"), "verdict": "样本不足"}
    got = ols_hac(s.to_numpy(dtype=float),
                  np.column_stack([np.ones(len(s)), yrs]), lags=lags)
    t = float(got["t"][1])
    if not np.isfinite(t):
        verdict = "无法判断"
    elif t < -2:
        verdict = "有衰减证据"
    elif abs(t) < 1:
        verdict = "无衰减证据"
    else:
        verdict = "弱证据"
    return {"n": int(len(s)), "slope_per_year": float(got["beta"][1]), "t": t,
            "verdict": verdict}


def period_summary(
    signal: pd.DataFrame,
    close: pd.DataFrame,
    dates: pd.Index,
    periods: dict[str, tuple[str, str]],
    horizon: int = 5,
    top: int = 3,
    min_names: int = 20,
) -> pd.DataFrame:
    """把 1.57 那个 t 值拆到各子区间：同一把尺子量每一段。

    子区间是**事先按行情性质定的**（2019-2022 震荡下行 / 2023-2026 主题轮动），
    不是先看结果再切——后者总能切出一个"信号很好"的窗口。
    """
    fwd = fwd_return(close, horizon)
    rows = []
    for name, (a, b) in periods.items():
        d = pd.DatetimeIndex(dates)
        d = d[(d >= pd.Timestamp(a)) & (d <= pd.Timestamp(b))]
        if len(d) < 12:
            rows.append({"period": name, "n": int(len(d))})
            continue
        ic = rowwise_spearman(signal.loc[d], fwd.reindex(d), min_names=min_names)
        st = ic_stats(ic)
        ts = tail_spread_series(signal.loc[d], fwd.reindex(d), d, top=top,
                                min_names=min_names)
        qs = quantile_spread(signal.loc[d], fwd.reindex(d), d, n_groups=10,
                             min_names=max(50, min_names))
        rows.append({
            "period": name,
            "n": int(len(d)),
            "ic": float(st["ic"]),
            "ic_t": float(st["t"]),
            "ic_hit": float(st["hit"]),
            "q_spread_bp": float(qs["Q9"] - qs["Q0"]) if len(qs) else float("nan"),
            "tail_mean_bp": float(ts.mean() * 1e4) if len(ts) else float("nan"),
            "tail_t": newey_west_t(ts) if len(ts) else float("nan"),
            "tail_hit": float((ts > 0).mean()) if len(ts) else float("nan"),
        })
    return pd.DataFrame(rows)


def execution_gap(
    targets: pd.DataFrame,
    open_: pd.DataFrame,
    close: pd.DataFrame,
    amount: pd.DataFrame | None = None,
    min_amount: float | None = None,
) -> dict:
    """实现缺口：信号日收盘 → **次日开盘**成交。

    必须用**目标权重**（信号日生成的权重，行只在调仓日）而不是每日持仓快照：
    快照里的每日变化大部分是价格漂移，不是成交，把它们当交易会把换手和成本算重十倍。
    回测器的约定写死在 `Backtest.run` 的 docstring 里："信号日收盘生成，次一交易日开盘执行"。

    符号：买入付 gap（开盘高于前收 = 亏），卖出付 -gap（开盘低于前收 = 亏）。

    `amount` / `min_amount`：成交日成交额过滤。**这一步不能省**——薄量 ETF 的开盘价
    可能只是几十手的零星成交（实测有 open 0.76 / 收盘 0.85、当日仅成交 3 手的情形），
    这种价上的"缺口"是数据噪声而不是市场事实，会把均值拖出一个不存在的收益。
    因此返回 `all` 与 `liquid` 两套统计，并给出落在薄量日的成交占比。
    """
    tw = targets.ffill().fillna(0.0)   # NaN = 当天没有新信号，沿用上次目标
    dw = tw.diff()
    if len(dw):
        dw.iloc[0] = tw.iloc[0]
    idx = tw.index
    gap = (open_.shift(-1).reindex(idx) / close.reindex(idx) - 1.0).reindex(columns=tw.columns)
    buy, sell = dw > 1e-12, dw < -1e-12
    cost = np.where(buy, gap.to_numpy(), np.where(sell, -gap.to_numpy(), np.nan))
    traded = dw.abs().to_numpy(dtype=float)
    base = np.isfinite(cost) & np.isfinite(traded) & (traded > 1e-12)

    if amount is not None and min_amount is not None:
        amt = amount.shift(-1).reindex(index=idx, columns=tw.columns).to_numpy(dtype=float)
        liquid = base & np.isfinite(amt) & (amt >= min_amount)
    else:
        liquid = base
    years = max((idx[-1] - idx[0]).days / 365.25, 1e-9)
    g = gap.to_numpy()
    buy_m, sell_m = buy.to_numpy(), sell.to_numpy()

    def _stat(mask: np.ndarray) -> dict:
        if not mask.any():
            return {"n": 0, "per_unit_bp": float("nan"), "annual_drag": float("nan")}
        wts = traded[mask]
        total = float((cost[mask] * wts).sum())
        return {
            "n": int(mask.sum()),
            "per_unit_cost": total / wts.sum() if wts.sum() > 0 else float("nan"),
            "per_unit_bp": (total / wts.sum() * 1e4) if wts.sum() > 0 else float("nan"),
            # 中位数是稳健版本：成本分布两侧都有极端跳空（QDII/薄量/停牌复牌），
            # 单看加权均值会被少数几笔支配，必须同时给出中位。
            "median_bp": float(np.nanmedian(cost[mask]) * 1e4),
            "buy_gap_mean_bp": float(np.nanmean(g[buy_m & mask]) * 1e4) if (buy_m & mask).any() else float("nan"),
            "sell_gap_mean_bp": float(np.nanmean(g[sell_m & mask]) * 1e4) if (sell_m & mask).any() else float("nan"),
            "total_drag": total,
            "annual_drag": total / years,
            "turnover_per_year": float(traded[mask].sum()) / years,
        }

    return {
        "n_trades": int(base.sum()),
        "share_illiquid": float(1.0 - liquid.sum() / base.sum()) if base.sum() else float("nan"),
        "all": _stat(base),
        "liquid": _stat(liquid),
    }


def capacity(
    targets: pd.DataFrame,
    adv: pd.DataFrame,
    aum_grid: tuple[float, ...] = (1e7, 5e7, 1e8, 5e8),
    participation: float = 0.10,
) -> pd.DataFrame:
    """不同 AUM 下"建完一次仓需要几天"（按目标权重的变化量，不是每日漂移）。

    口径：每次调仓的成交名义额 / (ADV20 × 参与率)。中位数决定日常，
    p90 决定"某几次调仓要拖很久"，>3 天的比例决定这个规模是否现实。
    """
    tw = targets.ffill().fillna(0.0)
    dw = tw.diff()
    if len(dw):
        dw.iloc[0] = tw.iloc[0]
    dw = dw.abs()
    adv_al = adv.reindex(index=tw.index, columns=tw.columns)
    rows = []
    for aum in aum_grid:
        days = (dw * aum) / (adv_al * participation)
        v = days.to_numpy(dtype=float)
        v = v[np.isfinite(v) & (v > 1e-4)]
        if v.size == 0:
            continue
        rows.append({
            "AUM": aum,
            "median_days": float(np.median(v)),
            "p90_days": float(np.percentile(v, 90)),
            "p99_days": float(np.percentile(v, 99)),
            "share_over_3d": float((v > 3).mean()),
        })
    return pd.DataFrame(rows)


def equity_exposure(weights: pd.DataFrame, category: pd.Series) -> pd.Series:
    """权益敞口 = 非债券权重之和。

    闸门不是"空仓/满仓"：它把权益从 100% 降到 50%，另外 50% 换成债券 ETF，
    所以毛敞口恒为 1.0——只看行和会得出"闸门从不生效"的错误结论。
    """
    bond = [c for c in weights.columns if str(category.get(c, "")) == "债券"]
    return 1.0 - weights[bond].sum(axis=1) if bond else weights.sum(axis=1)


def exposure_split(factor_ret: pd.Series, exposure: pd.Series, split: float = 0.75) -> dict:
    """把因子收益按"闸门开/关"分组，检验闸门是否有机制层面的作用。

    闸门要成立，必须满足：**关闸期间动量因子本身是亏的**。
    否则降半仓只是在稀释收益，不是在避险。
    """
    ex = exposure.reindex(factor_ret.index).ffill()
    on = factor_ret[ex >= split].dropna()
    off = factor_ret[ex < split].dropna()

    def stat(s: pd.Series) -> dict:
        if s.empty:
            return {"n": 0, "mean_ann": float("nan"), "hit": float("nan")}
        return {
            "n": int(s.size),
            "mean_ann": float((1 + s.mean()) ** 252 - 1),
            "hit": float((s > 0).mean()),
        }

    return {"on": stat(on), "off": stat(off)}


def snapshot_rows(doc: dict) -> list[tuple[str, str]]:
    """README 7.10 顶部那张"关键数字"表的行。

    `scripts/sync_readme.py` 用它生成、`scripts/audit.py` 用它核对——
    格式定义只此一份，避免"生成的和核对的不是同一套格式"这种假报警。
    缺字段就跳过该行，不抛异常（月报式的降级）。
    """
    meta = doc.get("meta") or {}
    reg = doc.get("regression") or {}
    boot = doc.get("bootstrap") or {}
    per = {r["period"]: r for r in doc.get("periods", []) if "ic" in r}
    tr = (doc.get("trend") or {}).get("IC") or {}
    rows: list[tuple[str, str]] = []

    def add(label: str, value: str | None) -> None:
        if value:
            rows.append((label, value))

    if meta.get("frozen_at"):
        add("诊断快照", f"{meta['frozen_at']}（数据截至 {meta.get('end', '—')}）")
    if reg.get("alpha_ann") is not None:
        add("alpha（年化，HAC t）",
            f"{reg['alpha_ann'] * 100:.1f}%（t={reg.get('alpha_t', float('nan')):.2f}，不显著）")
    if boot.get("cagr"):
        add("CAGR 95% 置信区间",
            f"{boot['cagr']['p2.5'] * 100:.1f}% ~ {boot['cagr']['p97.5'] * 100:.1f}%")
    if boot.get("mdd"):
        add("最大回撤（同分布中位数）", f"{boot['mdd']['p50'] * 100:.1f}%")
    tail = " / ".join(f"{per[k]['tail_mean_bp']:+.1f}bp"
                      for k in ("2019-2022", "2023-2026") if k in per)
    add("top3−全池 2019-2022 / 2023-2026", tail)
    if tr.get("verdict"):
        add("IC 趋势（年）",
            f"{tr['slope_per_year'] * 1e4:+.1f}bp/年（t={tr.get('t', float('nan')):.2f}，{tr['verdict']}）")
    return rows
