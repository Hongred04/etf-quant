"""
alpha 跟踪：把月报从"这个月赚了多少"改成"alpha 还在不在"。

做法是把每月收益拆成两部分：**因子能解释的**与**解释不了的**。
后者就是残差（residual），它才是"策略本身"的贡献：

    r_月 = α/12 + β_mkt·mkt_月 + β_mom·mom_月 + β_smb·smb_月 + ε_月

β 从 `output/diagnostics.json` 里取，**一经确定就冻结**，不再每月重估。
理由是：每月重估会让累计残差不可加（每个月换了一把尺子），
而且重估本身就引入了新的选择自由度——这正是 7.5 已经用尽的东西。

这个模块最重要的产出不是那个残差数字，而是**它旁边必须跟着的误差棒**：
本策略年化残差波动约 18%，折算每月约 5.3%，
于是"累计 n 个月的残差年化估计"的标准误是 12σ/√n ——
n=12 时是 18pp，n=36 时是 11pp。这个尺度决定了下面三条纪律：

1. **单月 |z| < 2（约 ±10pp）一律不解读。** 月度调仓策略单月 ±10pp 是常态。
2. **"证明 alpha 存在"是不可能的。** 要让 9.6pp 的 alpha 与 0 区分开（α/SE>2），
   需要约 15 年。前向跟踪的作用是**证伪与告警**，不是确认。
3. **真正能及时发现的只有"断裂"**：某月残差 |z|>3（约 −16pp 以下），
   几乎不可能是运气，那说明记账、执行或数据出了问题，必须当天查。

所以这个模块给月报提供的是一个**带说明书的数字**，而不是一个好看的百分比。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

DEFAULT_BETAS = ("mkt", "mom", "smb")


def monthly_residual(
    nav: pd.Series,
    factors: pd.DataFrame,
    alpha_ann: float,
    betas: dict[str, float],
    keys: tuple[str, ...] = DEFAULT_BETAS,
) -> pd.Series:
    """逐日残差聚合到月：`月残差 = Π(1+ε_d) − 1`，索引 "YYYY-MM"。

    日残差 = 策略日收益 − (α/252 + Σβ·因子日收益)。用复合而不是求和，
    是为了让"累计残差"能和净值直接对应（12 个月残差连乘 ≈ 年化偏离）。
    """
    ret = nav.pct_change(fill_method=None)
    f = factors.reindex(ret.index)
    ok = ret.notna() & f.notna().all(axis=1)
    if not ok.any():
        return pd.Series(dtype=float)
    r, ff = ret[ok], f[ok]
    pred = float(alpha_ann) / 252.0 + sum(float(betas[k]) * ff[k] for k in keys)
    resid = r - pred
    grp = pd.Series([f"{d.year}-{d.month:02d}" for d in resid.index], index=resid.index)
    return (1.0 + resid).groupby(grp).prod() - 1.0


def percentile_rank(value: float, history: pd.Series) -> float:
    """value 在历史分布中的分位（0~1）。用来回答"这个月算正常吗"。"""
    h = pd.Series(history).dropna()
    if h.empty or value is None or not np.isfinite(value):
        return float("nan")
    return float((h < value).mean())


def se_annualized(sigma_month: float, n_months: int) -> float:
    """累计 n 个月残差、折算成年化后的标准误。

    `12σ/√n`：年化把月均值放大 12 倍，而均值的标准误按 √n 收敛——
    两个效应一起，就是"要多长时间才能把一个年化偏离测准"的全部答案。
    """
    if n_months <= 0 or not np.isfinite(sigma_month):
        return float("nan")
    return float(12.0 * sigma_month / np.sqrt(n_months))


def months_to_se(sigma_month: float, target_se_ann: float) -> float:
    """标准误降到 target_se_ann 需要多少个月。"""
    if not np.isfinite(sigma_month) or sigma_month <= 0 or target_se_ann <= 0:
        return float("nan")
    return float((12.0 * sigma_month / target_se_ann) ** 2)


def months_to_detect(sigma_month: float, effect_ann: float, z: float = 2.0) -> float:
    """要以 z 倍标准误分辨 effect_ann 这么大的年化偏离，需要多少个月。"""
    if not np.isfinite(sigma_month) or sigma_month <= 0 or effect_ann <= 0:
        return float("nan")
    return float((z * 12.0 * sigma_month / effect_ann) ** 2)


def build_block(
    nav: pd.Series,
    factors: pd.DataFrame,
    alpha_ann: float,
    betas: dict[str, float],
    month: str,
    live_start: str | None = None,
    min_hist: int = 6,
) -> dict | None:
    """组装月报要用的那一节。历史不足 `min_hist` 个月就返回 None（整段不显示）。

    `live_start` 是"真实信号开始归档"的月份；累计残差从那个月开始算，
    因为回填期的净值本质上还是回测，不应该被当成前向证据。
    """
    hist = monthly_residual(nav, factors, alpha_ann, betas)
    if len(hist) < min_hist:
        return None
    sigma = float(hist.std(ddof=1))
    cur = float(hist[month]) if month in hist.index else None

    start = live_start if live_start and live_start in hist.index else None
    win = hist.loc[start:month] if start else None
    n = int(len(win)) if win is not None else 0
    cum = float((1.0 + win).prod() - 1.0) if n else None
    ann = float((1.0 + cum) ** (12.0 / n) - 1.0) if n and cum is not None and cum > -1 else None

    return {
        "alpha_ann": float(alpha_ann),
        "betas": {k: float(v) for k, v in betas.items()},
        "sigma_month": sigma,
        "sigma_ann": float(sigma * np.sqrt(12)),
        "month": month,
        "resid": cur,
        "z": float(cur / sigma) if cur is not None and sigma > 0 else None,
        "pct": percentile_rank(cur, hist) if cur is not None else None,
        "hist_n": int(len(hist)),
        "hist_p2_5": float(hist.quantile(0.025)),
        "hist_p97_5": float(hist.quantile(0.975)),
        "window_start": start,
        "window_n": n,
        "window_cum": cum,
        "window_ann": ann,
        "window_se": se_annualized(sigma, n) if n else None,
        "months_to_5pp": months_to_se(sigma, 0.05),
        "months_to_detect": months_to_detect(sigma, abs(float(alpha_ann))) if alpha_ann else None,
    }
