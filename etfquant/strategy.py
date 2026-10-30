"""
策略层（Strategy Layer）。

包含四个可对比的策略
--------------------
1. `MomentumBaseline`   —— 复刻 AlphaRotationLab/A-Share-ETF-Rotation-Model
                          （20 日动量排名 + 200 日均线过滤 + Top3 等权 + 每 5 日调仓）
2. `DualMomentum`       —— 相对动量 + 绝对动量双闸门（Antonacci Dual Momentum 思想）
3. `MultiFactorRotation`—— 多因子 z-score 合成 + 波动率倒数加权
4. `TrendGatedAdaptive` —— 本项目自研主策略（详见类文档字符串）

设计约束
--------
* 所有因子仅依赖当日及以前数据（t 日收盘计算），t+1 开盘执行；
* 候选池每日动态判定（上市满 N 日 + 流动性门槛），不可用"今日成分"倒推历史；
* 权重非负且合计 <= 1，剩余为现金；
* 换手控制通过"排名缓冲带"实现，避免边际排名波动造成过度交易。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import factors as F
from .secondary import blend_secondary


# --------------------------------------------------------------------------- #
# 数据容器与因子库
# --------------------------------------------------------------------------- #
@dataclass
class Panel:
    close: pd.DataFrame
    open_: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    volume: pd.DataFrame
    meta: pd.DataFrame  # index=ticker, columns=[name, category]
    # 每只标的实际最后一根日线的日期（前向填充之前）。
    # 用于检查"信号日用的是哪天的价格"——缓存落后的标的会被 ffill 顶上来，
    # 价格看起来正常但其实是旧的，必须在信号里标出来。
    last_date: pd.Series | None = None

    @property
    def amount(self) -> pd.DataFrame:
        """成交额近似（元）：收盘价 × 成交量(手) × 100。"""
        return self.close * self.volume * 100.0

    @property
    def cost(self) -> pd.Series:
        return self.meta["category"] if "category" in self.meta else pd.Series(dtype=object)


class FactorBank:
    """一次性预计算全部因子矩阵，供各策略共享。"""

    def __init__(self, panel: Panel):
        c = panel.close
        self.close = c
        self.ret = c.pct_change(fill_method=None)
        self.count = c.notna().cumsum()

        # 动量（剔除最近 5 日，规避短期反转）
        self.r20 = F.momentum(c, 20, skip=5)
        self.r60 = F.momentum(c, 60, skip=5)
        self.r120 = F.momentum(c, 120, skip=5)
        self.r20_raw = F.momentum(c, 20)
        self.r60_raw = F.momentum(c, 60)

        # 趋势
        self.ma60 = F.ma(c, 60)
        self.ma200 = F.ma(c, 200)
        self.above60 = c > self.ma60
        self.above200 = c > self.ma200
        self.slope60 = F.trend_slope(c, 60)

        # 风险
        self.vol20 = F.realized_vol(c, 20)
        self.vol60 = F.realized_vol(c, 60)
        self.dd60 = F.max_drawdown(c, 60)
        self.sharpe60 = self.r60_raw / self.vol60.replace(0, np.nan)

        # 流动性与拥挤
        self.amt20 = panel.amount.rolling(20, min_periods=10).mean()
        self.vr = F.volume_ratio(panel.volume, 5, 60)

        # 预计算横截面打分矩阵（缩尾 + z-score），保证不同调仓日的分数可比
        self.z_r20 = F.zscore_cs(F.winsorize_cs(self.r20))
        self.z_r60 = F.zscore_cs(F.winsorize_cs(self.r60))
        self.z_sharpe60 = F.zscore_cs(F.winsorize_cs(self.sharpe60))
        self.z_negvol60 = F.zscore_cs(F.winsorize_cs(-self.vol60))

    def z(self, df: pd.DataFrame) -> pd.DataFrame:
        return F.zscore_cs(df)


# --------------------------------------------------------------------------- #
# 基类
# --------------------------------------------------------------------------- #
class RotationBase:
    name = "base"
    lookback_rank = 20

    def __init__(
        self,
        panel: Panel,
        benchmark: str = "sh510300",
        top_n: int = 3,
        rebalance_freq: int = 5,
        min_hist: int = 250,
        min_amount: float = 2e7,
        max_weight: float = 0.45,
        warmup: int = 260,
        sleeve_n: int = 1,
        **kwargs,
    ):
        self.p = panel
        self.fb = FactorBank(panel)
        self.benchmark = benchmark if benchmark in panel.close.columns else None
        self.top_n = top_n
        self.rebalance_freq = rebalance_freq
        self.min_hist = min_hist
        self.min_amount = min_amount
        self.max_weight = max_weight
        self.warmup = warmup
        # 错峰子组合数：1 = 每次调仓全量换（现状）；N>1 = 目标权重取最近 N 次
        # 信号的等权平均，等价于把一次大调仓摊成 N 次小调仓。
        # 参考 guoyaohua/etf-adaptive-rotation-qmt 的 staggered_weeks。
        self.sleeve_n = max(1, int(sleeve_n))
        self.params = dict(top_n=top_n, rebalance_freq=rebalance_freq,
                           sleeve_n=self.sleeve_n, **kwargs)

        cats = panel.meta["category"] if "category" in panel.meta.columns else pd.Series(dtype=object)
        self.cat = cats.reindex(panel.close.columns).fillna("主题")
        self.defensive = [t for t in panel.close.columns if self.cat.get(t) == "债券"]
        self.holdings: list[str] = []
        # 计算失败的调仓日：不再静默 continue，回测脚本会把它计入 stats 并在日志里提示
        self.skipped: list[tuple[pd.Timestamp, str, str]] = []

        self._rebalance_days: list[pd.Timestamp] | None = None

    # ---------------- 时点可用性 ---------------- #
    def eligible_at(self, dt: pd.Timestamp) -> pd.Series:
        """当日可交易标的：价格有效 + 上市满 min_hist 个交易日 + 未被池子口径排除。

        ``meta["tradable"]`` 由 etfquant.universe.apply_pool 写入：剔除纯大盘宽基时
        只把它标成不可交易，列仍保留在 close 里供闸门算沪深300 趋势。
        """
        px_ok = self.p.close.loc[dt].notna()
        hist_ok = self.fb.count.loc[dt] >= self.min_hist
        ok = px_ok & hist_ok
        if "tradable" in self.p.meta.columns:
            ok = ok & self.p.meta["tradable"].reindex(ok.index).fillna(True).astype(bool)
        return ok

    def liquid_at(self, dt: pd.Timestamp, eligible: pd.Series) -> pd.Series:
        amt = self.fb.amt20.loc[dt]
        return eligible & (amt.fillna(0) >= self.min_amount)

    # ---------------- 调仓日 ---------------- #
    def rebalance_days(self) -> list[pd.Timestamp]:
        if self._rebalance_days is not None:
            return self._rebalance_days
        idx = self.p.close.index
        if len(idx) <= self.warmup:
            self._rebalance_days = []
            return []
        anchor = self.benchmark or (
            self.p.close.notna().sum().idxmax() if len(self.p.close.columns) else None
        )
        dates = idx[self.warmup :]
        # 以固定步长取样，保证调仓间隔一致
        self._rebalance_days = list(dates[:: self.rebalance_freq])
        return self._rebalance_days

    # ---------------- 权重工具 ---------------- #
    def _cap_weights(self, w: pd.Series, cap: float | None = None) -> dict[str, float]:
        """
        迭代削峰，使单只权重不超过 cap，且不改变组合总仓位（保留现金比例）。

        先在"归一化空间"里削峰（u_cap = cap / total），再乘回 total，
        否则当总仓位 < 1 时会被错误地放回满仓。
        """
        cap = self.max_weight if cap is None else cap
        if w.empty:
            return {}
        total = float(w.sum())
        if total <= 0:
            return {}
        u = w / total
        u_cap = min(1.0, cap / total)
        for _ in range(50):
            over = u > u_cap + 1e-12
            if not over.any():
                break
            excess = float((u[over] - u_cap).sum())
            u[over] = u_cap
            rest = ~over
            if not rest.any():
                break
            u[rest] += excess * u[rest] / u[rest].sum()
        # 当 持仓数 × cap < 目标总仓位 时约束不可行，迭代会在标的间来回倒而无法收敛。
        # 此时按 cap 强制封顶，缺口留给现金/债券，保证单只上限严格成立。
        u = u.clip(upper=u_cap)
        out = u * total
        return {k: float(v) for k, v in out.items() if v > 1e-6}

    def _inv_vol_weights(self, dt, picks: list[str]) -> pd.Series:
        vol = self.fb.vol60.loc[dt, picks].astype(float)
        vol = vol.replace(0, np.nan).fillna(vol.median() if vol.notna().any() else 0.3)
        inv = 1.0 / vol
        return inv / inv.sum()

    # ---------------- 子类实现 ---------------- #
    def target_weights(self, dt: pd.Timestamp, eligible: pd.Series) -> dict[str, float]:
        raise NotImplementedError

    # ---------------- 主流程 ---------------- #
    def generate(self) -> dict[pd.Timestamp, dict[str, float]]:
        """
        生成全部调仓日的目标权重。

        计算失败的调仓日不再被静默丢弃（原实现 `except: continue`）——
        那会让策略在一段时间内停止调仓，但回测照常输出净值曲线，没有任何提示。
        现在记录到 ``self.skipped`` 并打印警告，供 run_backtest 写入 stats。
        """
        out: dict[pd.Timestamp, dict[str, float]] = {}
        self.holdings = []
        self.skipped = []
        sleeves: list[dict[str, float]] = []
        for dt in self.rebalance_days():
            elig = self.eligible_at(dt)
            if not elig.any():
                continue
            try:
                w = self.target_weights(dt, elig)
            except Exception as exc:  # noqa: BLE001
                self.skipped.append((dt, type(exc).__name__, str(exc)[:120]))
                continue
            if self.sleeve_n > 1:
                sleeves.append(w)
                if len(sleeves) > self.sleeve_n:
                    sleeves.pop(0)
                w = _average_sleeves(sleeves)
            out[dt] = w
            # 缓冲带要护的是"实际持有的组合"，错峰后就是平均后的权重
            self.holdings = [k for k, v in w.items() if v > 1e-6]
        if self.skipped:
            first = self.skipped[0]
            print(f"[WARN] {self.name}: {len(self.skipped)} 个调仓日计算失败，"
                  f"首个 {first[0].date()} {first[1]}: {first[2]}")
        return out


def _average_sleeves(sleeves: list[dict[str, float]]) -> dict[str, float]:
    """
    把最近若干个调仓信号等权平均成当前目标权重。

    除以**已有**子组合数而不是固定的 N：回测头几个调仓日只有 1~2 份信号，
    若按 N 做分母，组合会自动留出一半现金（源项目就是这种"未初始化即现金"的写法），
    在 2019 年初等于人为空仓 20 个交易日。这里改成"有多少平均多少"，
    保证从第一天起就是满仓运行，各版本之间可比。
    """
    if not sleeves:
        return {}
    acc: dict[str, float] = {}
    for s in sleeves:
        for k, v in s.items():
            acc[k] = acc.get(k, 0.0) + v
    n = float(len(sleeves))
    return {k: v / n for k, v in acc.items() if v / n > 1e-6}


def _equal(w: pd.Series) -> dict[str, float]:
    if w.empty:
        return {}
    return {k: float(1.0 / len(w)) for k in w.index}


# --------------------------------------------------------------------------- #
# 1) 基线：复刻开源项目
# --------------------------------------------------------------------------- #
class MomentumBaseline(RotationBase):
    """
    简化动量轮动基线（对应 AlphaRotationLab A-Share-ETF-Rotation-Model）。

    规则：R20 排名 → 仅保留价格 > MA200 → 取前 top_n 等权。
    该模型不做绝对动量择时，也不做防守资产切换，作为对照基准存在。
    """

    name = "S1-动量基线"

    def __init__(self, panel, ma_period: int = 200, **kw):
        super().__init__(panel, **kw)
        self.ma_period = ma_period

    def target_weights(self, dt, eligible):
        elig = self.liquid_at(dt, eligible)
        mom = self.fb.r20.loc[dt, elig].dropna()
        if mom.empty:
            return {}
        above = (
            self.fb.above200 if self.ma_period == 200 else self.p.close > F.ma(self.p.close, self.ma_period)
        ).loc[dt]
        cand = mom[above.reindex(mom.index).fillna(False)]
        if len(cand) < self.top_n:
            cand = mom
        picks = cand.sort_values(ascending=False).head(self.top_n)
        return _equal(picks)


# --------------------------------------------------------------------------- #
# 2) 双动量
# --------------------------------------------------------------------------- #
class DualMomentum(RotationBase):
    """
    相对动量 + 绝对动量（Antonacci Dual Momentum）。

    相对动量：在池内按 R60 排序取前 top_n；
    绝对动量：若标的 R60 <= 0（绝对动量为负）则剔除；
    若全部候选被剔除 → 切换至债券 ETF；债券亦弱则空仓。
    """

    name = "S2-双动量"

    def target_weights(self, dt, eligible):
        elig = self.liquid_at(dt, eligible)
        r60 = self.fb.r60.loc[dt, elig].dropna()
        above = self.fb.above200.loc[dt, elig].fillna(False)
        cand = r60[(r60 > 0) & above]
        if len(cand) >= 1:
            picks = cand.sort_values(ascending=False).head(self.top_n)
            return self._cap_weights(pd.Series(1.0, index=picks.index))
        # 防守切换
        return self._defensive_alloc(dt, 1.0)

    def _defensive_alloc(self, dt, weight: float) -> dict[str, float]:
        if not self.defensive:
            return {}
        r20 = self.fb.r20_raw.loc[dt, self.defensive].dropna()
        if r20.empty:
            return {}
        return {r20.idxmax(): weight}


# --------------------------------------------------------------------------- #
# 3) 多因子
# --------------------------------------------------------------------------- #
class MultiFactorRotation(RotationBase):
    """
    多因子 z-score 合成 + 波动率倒数加权。

    打分 = 0.40·z(R20) + 0.30·z(R60) + 0.30·z(Sharpe60)
    候选要求价格 > MA60 且流动性达标；Top N 按 1/σ60 加权。
    """

    name = "S3-多因子"

    def target_weights(self, dt, eligible):
        elig = self.liquid_at(dt, eligible)
        above = self.fb.above60.loc[dt, elig].fillna(False)
        cols = elig[elig].index
        cand = above[above].index
        if len(cand) < self.top_n:
            cand = cols
        if len(cand) == 0:
            return {}
        # 只有 1 只候选时横截面标准差为 0（pandas 默认 ddof=1，单样本 std 为 NaN），
        # z-score 全变 NaN → score 为空 → 当日空仓。这是"无提示的仓位归零"，
        # 必须退化为按波动率倒数直接配权。
        if len(cand) == 1:
            return self._cap_weights(self._inv_vol_weights(dt, list(cand)))
        z = lambda df: F.zscore_cs(df.loc[[dt], cand]).iloc[0]  # noqa: E731
        score = 0.40 * z(self.fb.z_r20) + 0.30 * z(self.fb.z_r60) + 0.30 * z(self.fb.z_sharpe60)
        score = score.dropna().sort_values(ascending=False)
        if score.empty:
            # 候选之间因子完全相同时同样会退化为空（如全部停牌、价格恒定）
            return self._cap_weights(self._inv_vol_weights(dt, list(cand)))
        picks = list(score.head(self.top_n).index)
        return self._cap_weights(self._inv_vol_weights(dt, picks))


# --------------------------------------------------------------------------- #
# 4) 自研主策略
# --------------------------------------------------------------------------- #
class TrendGatedAdaptive(RotationBase):
    """
    趋势闸门自适应动量轮动（Trend-Gated Adaptive Rotation, TGAR）

    分层逻辑
    --------
    【第 1 层：市场状态闸门 regime ∈ [0,1]】
        基准（默认沪深300ETF 510300）三项条件加权求和：
            +0.4  收盘 > MA200     （长期趋势）
            +0.3  收盘 > MA60      （中期趋势）
            +0.3  R20 > 0          （短期动量）
        映射为权益敞口：regime >= 0.7 → 1.0；0.35~0.7 → 0.5；< 0.35 → 0.5（不空仓）
        默认只有两档：`>= regime_mid` 满敞口，否则半敞口（不空仓）。
        早期的第三档（`regime_low`）在默认参数下与第二档敞口相同，实测取 0.2/0.35/0.5
        结果完全一致（见 README 7.5 的"死参数"），因此默认关闭；要复现三档消融时显式传
        `regime_low`（ablation.py 的 J 组即如此）。这是消融结论：趋势闸门只降半仓、
        不空仓，年化与回撤都更优。未用敞口配置债券 ETF，无合适债券则持现金。

    【第 2 层：标的初筛（趋势 + 流动性 + 拥挤度）】
        · 上市满 min_hist 个交易日
        · 收盘 > MA60（只买上升趋势，不抄底）
        · 近 20 日日均成交额 >= min_amount（可容纳实盘冲击）
        · 量比 vr(5/60) < max_vr（剔除短期成交异常放大的拥挤品种）

    【第 3 层：横截面打分】
        score = z(R20)   （默认，skip 5 日）
        备选 score = 0.35·z(R20) + 0.25·z(R60) + 0.20·z(Sharpe60) + 0.20·z(-σ60)
        备选 score_mode="resid" / "r20+resid" / "r20+resid_filter"：接入残差动量
        （`etfquant/resid_mom.py`，预计算的 z 值矩阵由外部传入）。三者默认关闭：
        A/B 见 README 7.6 ⑦，权重 0.2/0.3 × 窗口 60/120/250 共 6 组，**0 组**能通过
        "2020-22 与 2023-26 两段同向改善"的检验，属数据挖掘曲面，不采纳。

    【第 3.5 层：第二排序键（可选，默认关闭）】
        `secondary` / `secondary_weight`：把次要因子以很小权重（0.05 量级）加到分数上，
        只翻动 z20 差 < 0.1 的近平局，不改主排序。A/B 见 README 7.12：
        52 周高点距离两档权重均未通过判据（2020-22 夏普 0.53→0.49/0.46）。

    【第 4 层：元标签仓位系数（可选，默认关闭）】
        `meta_exposure`：行=调仓日、值∈[0.5,1] 的乘法系数，在 regime 与回撤触发之后相乘，
        只缩放敞口、不改选股。A/B 见 README 7.13：三种映射全部跑输（夏普 0.69→0.60~0.65），
        元模型 AUC 仅 0.442——仓位层面没有可预测性。
        z 值按当日横截面标准化，缩尾 2%/98% 抑制极端值。

    【第 4 层：权重构造】
        Top N（默认 3）等权，单只上限 max_weight。
        等权在消融实验中优于波动率倒数加权（后者在 A 股 ETF 上系统性低配高弹性品种）。

    【第 5 层：换手与仓位控制】
        · 排名缓冲带：现有持仓只要仍位于 top(2N) 即保留，降低边界抖动带来的换手；
        · 市场状态闸门控制总仓位而非空仓离场：regime 低时降至 50% 权益 + 50% 债券，
          保留反弹参与度（消融显示"允许空仓"版本收益与 Calmar 均更差）；
        * 原设计的"组合级回撤熔断"经消融验证为负贡献（在底部减仓形成追跌），
          故默认关闭（dd_trigger=10.0 等效失效），参数保留供研究。

    重要结论（详见报告消融章节）
    --------------------------
    在本数据集上，四因子合成打分显著劣于纯 20 日动量（CAGR 17.7% vs 23.9%），
    因此默认 score_mode="mom20"。多因子仅作为可选模块保留，不做默认开启。
    """

    name = "S4-TGAR(自研)"

    def __init__(
        self,
        panel,
        max_vr: float = 3.0,
        dd_trigger: float = 10.0,
        w_mom_short: float = 0.35,
        w_mom_mid: float = 0.25,
        w_sharpe: float = 0.20,
        w_lowvol: float = 0.20,
        regime_mid: float = 0.7,
        # 第三档（防守档）默认**关闭**：默认参数下 expo_low == expo_mid，
        # regime_low 取任何值（实测 0.2 / 0.35 / 0.5）结果完全相同，是个"死参数"。
        # 传 None 表示只有两档；要复现早期的三档消融实验，显式传 regime_low（如 0.35）。
        regime_low: float | None = None,
        buffer_mult: int = 2,
        weight_mode: str = "equal",
        require_trend: bool = True,
        score_mode: str = "mom20",
        expo_high: float = 1.0,
        expo_mid: float = 0.5,
        expo_low: float | None = None,       # None = 与 expo_mid 相同（两档）
        gate_mode: str = "csi300",
        dedup_corr: float | None = None,
        dedup_window: int = 60,
        vol_target: float | None = None,
        vol_window: int = 60,
        vol_floor: float = 0.2,
        # 残差动量（可选第二因子）：预计算的 z 值矩阵，行=调仓日、列=代码。
        # 传 None 表示不用；score_mode 指定如何用它（见 target_weights）。
        resid: pd.DataFrame | None = None,
        resid_weight: float = 0.3,
        resid_min: float | None = None,
        # 第二排序键（可选）：同样是预计算的 z 值矩阵（行=调仓日、列=代码）。
        # 与 resid 的区别是**只能微调**——权重小到只会翻动近平局，见 target_weights。
        secondary: pd.DataFrame | None = None,
        secondary_weight: float = 0.0,
        # 元标签仓位系数（可选）：行=调仓日、值∈[0.5,1] 的乘法系数，只缩放敞口。
        # 不改选股、不改排序，见 etfquant/meta.py 与 README 7.13。
        meta_exposure: pd.Series | None = None,
        **kw,
    ):
        super().__init__(panel, **kw)
        self.max_vr = max_vr
        self.dd_trigger = dd_trigger
        self.w = (w_mom_short, w_mom_mid, w_sharpe, w_lowvol)
        self.regime_mid = regime_mid
        self.regime_low = regime_low
        self.buffer_mult = buffer_mult
        self.weight_mode = weight_mode
        self.require_trend = require_trend
        self.score_mode = score_mode
        # 两档时第三档直接复用 mid，避免出现"两个值相等却分成两档"的假自由度
        self.expo_map = (expo_high, expo_mid,
                         expo_mid if expo_low is None else expo_low)
        # 同质化去重：候选之间 vol_window 日收益相关性 ≥ dedup_corr 时，只留分数高的那只。
        # None = 关闭（现状）。纳指科技 + 纳指ETF 这种同指数双持就是靠它消掉的。
        # 闸门判据：csi300 = 用沪深300（历史口径）；breadth = 用池内非债券标的的等权净值。
        # 后者对"主题轮动型"组合更贴合——沪深300 是市值加权、被金融和白酒主导，
        # 和池子里 200 多只行业/跨境/科创 ETF 的走势并不是一回事。
        self.gate_mode = gate_mode
        self.dedup_corr = dedup_corr
        self.dedup_window = dedup_window
        # 波动率目标化：把权益仓位按「目标波动 / 近期已实现波动」缩放，下限 vol_floor。
        # None = 关闭（现状）。缩放腾出的敞口自动进入下方债券 rest。
        self.vol_target = vol_target
        self.vol_window = vol_window
        self.vol_floor = vol_floor
        self.resid = resid
        self.resid_weight = float(resid_weight)
        self.resid_min = resid_min
        self.secondary = secondary
        self.secondary_weight = float(secondary_weight)
        self.meta_exposure = meta_exposure
        self._nav_peak60: float | None = None
        self._nav: list[tuple[pd.Timestamp, float]] = []

    # ---------- 市场状态 ---------- #
    def regime_at(self, dt: pd.Timestamp) -> float:
        s = self._bench_series()
        hist = s.loc[:dt].dropna()
        if len(hist) < 210:
            return 1.0
        px = hist.iloc[-1]
        ma200 = hist.iloc[-200:].mean()
        ma60 = hist.iloc[-60:].mean()
        r20 = px / hist.iloc[-21] - 1 if len(hist) > 21 else 0.0
        score = 0.0
        score += 0.4 if px > ma200 else 0.0
        score += 0.3 if px > ma60 else 0.0
        score += 0.3 if r20 > 0 else 0.0
        return float(score)

    def _exposure(self, regime: float) -> float:
        """
        闸门 → 权益敞口。默认只有**两档**：`regime >= regime_mid` 满敞口，否则半敞口。

        早期的三档（`regime_low` 再切一刀）在默认参数下 `expo_low == expo_mid`，
        切与不切结果一样——「7.5 过拟合体检」把它列为死参数，因此默认关掉：
        `regime_low=None` 表示不启用第三档。要复现历史消融（0/0.5/1 三档）时显式传值。
        """
        if regime >= self.regime_mid:
            return self.expo_map[0]
        if self.regime_low is None:
            return self.expo_map[1]
        if regime >= self.regime_low:
            return self.expo_map[1]
        return self.expo_map[2]

    # ---------- 回撤熔断（用策略自身净值，逐日跟踪） ---------- #
    def _update_nav_state(self, dt: pd.Timestamp, bench_px: float) -> None:
        """用基准净值近似策略净值的回撤状态，避免前视（策略净值需回测后才知）。"""
        self._nav.append((dt, bench_px))

    def _drawdown_triggered(self, dt: pd.Timestamp) -> bool:
        s = self._bench_series().loc[:dt].dropna().iloc[-60:]
        if len(s) < 30:
            return False
        return bool(s.iloc[-1] / s.max() - 1.0 < -self.dd_trigger)

    # ---------- 防守 ---------- #
    def _defensive(self, dt: pd.Timestamp) -> str | None:
        if not self.defensive:
            return None
        r20 = self.fb.r20_raw.loc[dt, self.defensive].dropna()
        if r20.empty:
            return None
        return str(r20.idxmax())

    # ---------- 闸门判据 ---------- #
    def _bench_series(self) -> pd.Series:
        """
        闸门/熔断用的"市场状态"序列。

        csi300  —— 沪深300 收盘价（历史口径）。注意它是市值加权、被金融与白酒主导，
                   和池子里 200 多只行业/跨境/科创 ETF 的走势并不是一回事。
        breadth —— 池内非债券标的各自归一后的等权平均，相当于"全市场 ETF 等权指数"。
                   它是选股用的同一批标的，因此不是独立基准，但作为"多数品种在涨还是跌"
                   的广度信号是合理的，且只用到当日及以前的价格。
        """
        if self.gate_mode == "breadth":
            cols = [c for c in self.p.close.columns if self.cat.get(c) != "债券"]
            sub = self.p.close[cols] if cols else self.p.close
            first = sub.apply(lambda x: x.dropna().iloc[0] if x.notna().any() else np.nan)
            return (sub / first.replace(0, np.nan)).mean(axis=1)
        if self.benchmark:
            return self.p.close[self.benchmark]
        # 退化：用宽基类标的的等权净值代理
        wide = [t for t in self.p.close.columns if self.cat.get(t) == "宽基"]
        sub = self.p.close[wide] if wide else self.p.close
        norm = sub / sub.apply(lambda s: s.dropna().iloc[0] if s.notna().any() else np.nan)
        return norm.mean(axis=1)

    # ---------- 同质化去重 / 波动率目标 ---------- #
    def _dedup_rank(self, dt: pd.Timestamp, ranked: list[str], look: int = 12) -> list[str]:
        """
        把"高度同质"的标的挤出前排，返回重排后的候选序列。

        只看前 ``look`` 只算相关性就够了：后面的本来也进不了 Top N。
        被挤掉的不是删除，而是排到队尾——这样缓冲带与补位逻辑仍能在
        "候选不够"时把它们捡回来，不会因为去重把仓位打空。
        """
        if not self.dedup_corr or len(ranked) < 2:
            return ranked
        codes = [c for c in ranked[:look] if c in self.p.close.columns]
        if len(codes) < 2:
            return ranked
        sub = self.p.close.loc[:dt, codes].iloc[-(self.dedup_window + 1):]
        rets = sub.pct_change(fill_method=None).dropna(how="all")
        if len(rets) < 20:          # 数据不够就不下结论
            return ranked
        corr = rets.corr()
        keep: list[str] = []
        dup: list[str] = []
        for c in ranked:
            is_dup = False
            if c in corr.index:
                for k in keep:
                    if k not in corr.columns:
                        continue
                    v = corr.at[c, k]
                    if pd.notna(v) and v >= self.dedup_corr:
                        is_dup = True
                        break
            (dup if is_dup else keep).append(c)
        return keep + dup

    def _resid_row(self, dt: pd.Timestamp, cols: list[str]) -> pd.Series:
        """
        取当天可用的残差因子 z 值。

        用 `asof` 而不是精确匹配：预计算只在调仓日算，但若调用方传了别的日期
        （比如手工试算某一天），取最近一次已经算好的值，不会悄悄全变 NaN。
        """
        if self.resid is None or self.resid.empty:
            return pd.Series(dtype=float)
        idx = self.resid.index[self.resid.index <= pd.Timestamp(dt)]
        if len(idx) == 0:
            return pd.Series(dtype=float)
        return self.resid.loc[idx[-1]].reindex(cols)

    def _meta_lookup(self, dt: pd.Timestamp) -> float | None:
        """取当天生效的元标签仓位系数（asof：没有当天就用最近一次）。"""
        s = self.meta_exposure
        if s is None or len(s) == 0:
            return None
        idx = s.index[s.index <= pd.Timestamp(dt)]
        if len(idx) == 0:
            return None
        v = float(s.loc[idx[-1]])
        return v if np.isfinite(v) else None

    def _secondary_row(self, dt: pd.Timestamp, cols: list[str]) -> pd.Series:
        """取当天可用的第二排序键 z 值（asof 语义，与 `_resid_row` 一致）。"""
        if self.secondary is None or self.secondary.empty:
            return pd.Series(dtype=float)
        idx = self.secondary.index[self.secondary.index <= pd.Timestamp(dt)]
        if len(idx) == 0:
            return pd.Series(dtype=float)
        return self.secondary.loc[idx[-1]].reindex(cols)

    def _realized_vol(self, dt: pd.Timestamp, picks: list[str]) -> float:
        """候选等权组合近 vol_window 日的年化已实现波动率（仅用当天及以前的数据）。"""
        cols = [c for c in picks if c in self.p.close.columns]
        if not cols:
            return 0.0
        sub = self.p.close.loc[:dt, cols].iloc[-(self.vol_window + 1):]
        rets = sub.pct_change(fill_method=None).dropna(how="all")
        if len(rets) < 10:
            return 0.0
        port = rets.mean(axis=1)      # 等权近似，与 weight_mode="equal" 一致
        sd = float(port.std(ddof=1))
        return sd * float(np.sqrt(252)) if np.isfinite(sd) else 0.0

    # ---------- 主逻辑 ---------- #
    def target_weights(self, dt, eligible):
        # 债券不参与打分：其波动量级比股票类小一个数量级，R20 与权益品种不可比；
        # 债券只作为 "未用敞口" 的防守配置，由下方 rest 统一分配，避免重复计入。
        is_bond = pd.Series([self.cat.get(t) == "债券" for t in eligible.index], index=eligible.index)
        elig = self.liquid_at(dt, eligible & ~is_bond)
        regime = self.regime_at(dt)
        expo = self._exposure(regime)

        if self._drawdown_triggered(dt):
            expo *= 0.5

        # 元标签：在 regime 与回撤触发之后，再乘一个 ×0.5~×1 的系数。
        # 放在最后是为了让"最终权益敞口"只有一个乘法链，便于审计核对。
        if self.meta_exposure is not None and len(self.meta_exposure):
            k = self._meta_lookup(dt)
            if k is not None:
                expo *= float(k)

        equity_w: dict[str, float] = {}
        if expo > 0:
            above = self.fb.above60.loc[dt].reindex(elig.index).fillna(False)
            vr = self.fb.vr.loc[dt].reindex(elig.index)
            cand = elig & (vr.fillna(1.0) < self.max_vr)
            if self.require_trend:
                cand = cand & above
            cols = list(cand[cand].index)
            if len(cols) < self.top_n:
                cols = list(elig[elig].index)
            if cols:
                z20 = self.fb.z_r20.loc[dt, cols]
                if self.score_mode == "mom20":
                    score = z20
                elif self.score_mode == "resid":
                    # 残差单独当主因子：用来回答"它能不能替代 R20"，而不是"能不能加分"
                    score = self._resid_row(dt, cols)
                elif self.score_mode == "r20+resid":
                    # 混合打分。缺残差数据的标的（新上市）按 0（中性）处理，
                    # 而不是整只剔除——否则池子会在 warmup 期悄悄变小。
                    rz = self._resid_row(dt, cols).reindex(cols).fillna(0.0)
                    score = (1 - self.resid_weight) * z20 + self.resid_weight * rz
                elif self.score_mode == "r20+resid_filter":
                    # 质量过滤：主排序仍是 R20，只把残差为负（跑输自己的 beta 解释）的剔掉
                    rz = self._resid_row(dt, cols)
                    keep = rz[rz >= (self.resid_min or 0.0)].index
                    score = z20.reindex(keep)
                else:
                    score = (
                        self.w[0] * z20
                        + self.w[1] * self.fb.z_r60.loc[dt, cols]
                        + self.w[2] * self.fb.z_sharpe60.loc[dt, cols]
                        + self.w[3] * self.fb.z_negvol60.loc[dt, cols]
                    )
                score = score.replace([np.inf, -np.inf], np.nan)
                # 第二排序键：权重故意取得很小（0.05 量级）。z 值在 ±2 内，
                # 贡献最多 ±0.1，因此**只能翻动 z20 差 < 0.1 的近平局**，
                # 不会把 R20 的第一名挤掉。这与"在并列时用次要信息打破平局"
                # 是同一件事的可实现版本（真正的硬分桶会让次要因子越权）。
                if self.secondary is not None and self.secondary_weight > 0:
                    sec = self._secondary_row(dt, list(score.index))
                    score = blend_secondary(score, sec, self.secondary_weight)
                score = score.dropna()
                if not score.empty:
                    # 缩尾会把横截面顶端的几只压成同一个 z 值，形成并列。
                    # 若不指定次序，pandas 的 quicksort 对同分元素的顺序是
                    # 实现相关且不稳定的——同一份数据换 pandas 版本/平台就可能
                    # 选出不同标的。对每日要出实盘信号的工具，这不可接受。
                    # 这里用原始 R20 作第二排序键：既保证确定性，
                    # 也符合"并列时选动量确实更强的那只"的直觉。
                    tie = self.fb.r20.loc[dt].reindex(score.index).fillna(-np.inf)
                    score = pd.DataFrame({"s": score, "r": tie}).sort_values(
                        ["s", "r"], ascending=[False, False]
                    )["s"]
                    ranked = self._dedup_rank(dt, list(score.index))
                    picks: list[str] = [h for h in self.holdings if h in ranked[: self.top_n * self.buffer_mult]]
                    picks = picks[: self.top_n]
                    for t in ranked:
                        if len(picks) >= self.top_n:
                            break
                        if t not in picks:
                            picks.append(t)
                    if picks and self.vol_target:
                        rv = self._realized_vol(dt, picks)
                        if rv > 1e-9:
                            # 只减不增：目标波动高于当前波动时不加杠杆。
                            # 缩放后 expo 变小，下方 rest 自动变大 → 转成债券，
                            # 与"降权益、买债券、不留现金"的既有口径一致。
                            expo *= min(1.0, max(self.vol_floor, self.vol_target / rv))
                    if picks:
                        if self.weight_mode == "equal":
                            w = pd.Series(1.0 / len(picks), index=picks) * expo
                        else:
                            w = self._inv_vol_weights(dt, picks) * expo
                        equity_w = self._cap_weights(w, cap=min(self.max_weight, expo))

        rest = max(0.0, 1.0 - sum(equity_w.values()))
        if rest > 1e-6:
            d = self._defensive(dt)
            if d is not None:
                equity_w[d] = equity_w.get(d, 0.0) + rest
        return equity_w


STRATEGY_REGISTRY = {
    "s1_baseline": MomentumBaseline,
    "s2_dual": DualMomentum,
    "s3_multi": MultiFactorRotation,
    "s4_tgar": TrendGatedAdaptive,
}
