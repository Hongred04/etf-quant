"""
生成 HTML 研究报告。

python scripts/make_report.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from etfquant import metrics
from etfquant.io_utils import atomic_write_text
from etfquant.report import (
    df_to_html, render_page, svg_annual_bars, svg_drawdown, svg_heatmap, svg_line_chart,
)

OUT = ROOT / "output"
SELF = "S4-TGAR(自研)"
BENCH = "基准-沪深300"


def pct(x: float, d: int = 2) -> str:
    if x is None or not np.isfinite(x):
        return "-"
    return f"{x * 100:.{d}f}%"


def main() -> None:
    nav = pd.read_csv(OUT / "nav_main.csv", index_col=0, parse_dates=True)
    perf = pd.read_csv(OUT / "perf_main.csv", index_col=0)
    ladder = pd.read_csv(OUT / "ladder.csv", index_col=0)
    ablation = pd.read_csv(OUT / "ablation.csv", index_col=0)
    sub2 = pd.read_csv(OUT / "subsample2.csv", index_col=0)
    inv = pd.read_csv(ROOT / "data" / "etf_history_inventory.csv")
    pool = pd.read_csv(ROOT / "data" / "etf_pool.csv")
    sig_fp = sorted(OUT.glob("signal_*.csv"))[-1]
    sig = pd.read_csv(sig_fp, dtype={"代码": str})

    nav_self = nav[SELF].dropna()
    nav_bench = nav[BENCH].dropna()

    # ---- 实验结果动态取值，避免正文数字与 CSV 脱节 ----
    def _row(df, kw):
        hit = [i for i in df.index if kw in i]
        if not hit:
            raise KeyError(f"未找到行：{kw}")
        return df.loc[hit[0]]

    def LG(kw, col="年化收益(CAGR)"):
        return float(_row(ladder, kw)[col])

    def AB(kw, col="年化收益(CAGR)"):
        return float(_row(ablation, kw)[col])

    bench_cagr = metrics.cagr(nav_bench)
    bench_mdd = metrics.max_drawdown(nav_bench)

    front_self = float(sub2.loc["前段 2019-2022", "最终模型_CAGR"])
    front_bench = float(sub2.loc["前段 2019-2022", "基准CAGR"])
    front_rel = "略超" if front_self > front_bench else "跑输"

    def AV(year: int, col: str = SELF) -> float:
        try:
            return float(ann.loc[year, col])
        except (KeyError, ValueError):
            return float("nan")
    p_self = perf.loc[perf["策略"] == SELF].iloc[0]

    # ---------------- KPI ----------------
    # ---------------- 对外口径 ----------------
    # 回测年化是样本内上限，"能指望多少"要用滚动前向锚点换算的区间。
    # 换算规则在 etfquant/expectation.py，这里只消费，避免报告与界面各写一套。
    from etfquant.expectation import load as load_expectation

    exp = load_expectation(OUT / "overfit_summary.json",
                           sample_cagr=float(p_self["年化收益(CAGR)"]))
    exp_label = exp["label"] if exp else ""
    exp_note = (f"可对外引用的预期是 <b>{exp['label']}</b>——回测数字是<b>样本内上限</b>，"
                f"锚点 {pct(exp['anchor'], 1)} 来自「每 6 个月重挑一次参数」的纯样本外年化，"
                f"挑选带来的折扣约 {pct(exp['discount'], 0)}。"
                if exp else "")

    kpis = [
        (pct(p_self["累计收益"], 1), f"累计收益 ({nav.index[0]:%Y-%m}~{nav.index[-1]:%Y-%m})"),
        (pct(p_self["年化收益(CAGR)"]), "年化收益 CAGR（样本内）"),
        *([(exp_label, "对外预期")] if exp_label else []),
        (f"{p_self['夏普比率']:.2f}", "夏普比率"),
        (pct(p_self["最大回撤"]), "最大回撤"),
        (f"{p_self['卡玛比率']:.2f}", "卡玛比率"),
        (pct(p_self["超额收益"], 1), "相对沪深300超额"),
    ]
    kpi_html = '<div class="kpis">' + "".join(
        f'<div class="kpi"><div class="v">{v}</div><div class="l">{l}</div></div>' for v, l in kpis
    ) + "</div>"

    # ---------------- 绩效表 ----------------
    cols = ["累计收益", "年化收益(CAGR)", "年化波动", "夏普比率", "最大回撤",
            "卡玛比率", "超额收益", "Beta", "信息比率", "年均换手(单边)"]
    pt = perf[cols].copy()
    pt.index = perf["策略"].tolist()
    perf_html = df_to_html(
        pt, pct_cols=("累计收益", "年化收益(CAGR)", "年化波动", "最大回撤", "超额收益"),
        num_cols=("夏普比率", "卡玛比率", "Beta", "信息比率", "年均换手(单边)"),
    )

    # ---------------- 图表 ----------------
    fig_nav = svg_line_chart(nav, title="策略净值对比（对数刻度，初始=1）")
    fig_dd = svg_drawdown(nav_self, nav_bench)
    ann = pd.DataFrame({
        c: metrics.annual_returns(nav[c]) for c in nav.columns
    })
    ann.index = ann.index.year
    fig_ann = svg_annual_bars(ann[["S1-动量基线", SELF, BENCH]])
    fig_heat = svg_heatmap(metrics.monthly_return_table(nav_self))

    # 年度收益表
    ann_tbl = (ann * 100).round(2)
    ann_tbl.index = [f"{y}年" for y in ann_tbl.index]
    ann_html = df_to_html(ann_tbl, num_cols=tuple(ann_tbl.columns))

    # ---------------- 实验表 ----------------
    lad = ladder[["年化收益(CAGR)", "夏普比率", "最大回撤", "卡玛比率", "年均换手", "累计收益"]].copy()
    ladder_html = df_to_html(
        lad, pct_cols=("年化收益(CAGR)", "最大回撤", "累计收益"),
        num_cols=("夏普比率", "卡玛比率", "年均换手"),
    )

    abl = ablation[["年化收益(CAGR)", "夏普比率", "最大回撤", "卡玛比率", "年均换手"]].copy()
    abl_html = df_to_html(
        abl, pct_cols=("年化收益(CAGR)", "最大回撤"),
        num_cols=("夏普比率", "卡玛比率", "年均换手"),
    )

    sub_tbl = sub2.copy()
    sub_tbl = sub_tbl[["基准CAGR", "纯动量_CAGR", "最终模型_CAGR",
                       "基准MaxDD", "纯动量_MaxDD", "最终模型_MaxDD"]]
    sub_html = df_to_html(sub_tbl, pct_cols=tuple(sub_tbl.columns))

    # ---------------- 资产池 ----------------
    cat = pool.groupby("category").agg(
        数量=("ticker", "size"), 成交额亿元=("amount_yuan", lambda s: round(s.sum() / 1e8, 1))
    ).sort_values("数量", ascending=False)
    pool_html = df_to_html(cat, num_cols=("成交额亿元",))

    # ---------------- 今日信号 ----------------
    sig_show = sig.copy().reset_index(drop=True)
    sig_html = df_to_html(sig_show, pct_cols=("目标权重",), num_cols=("最新价", "R20%", "R60%", "年化波动%"),
                          show_index=False)

    date_range = f"{nav.index[0].date()} ~ {nav.index[-1].date()}"
    n_days = len(nav)
    # 与 CAGR 口径保持一致：用日历天数，而不是交易日/252
    # （A 股每年约 242 个交易日，252 折算会把 7.8 年写成 7.5 年）
    years = metrics.years_elapsed(nav)

    body = f"""
{kpi_html}
<div class="note">
<b>结论先行。</b>基于全市场 260 只 ETF 的 20 日动量轮动 + 趋势闸门策略，在 {date_range}
（{years:.1f} 年）的回测中取得年化 <b>{pct(p_self['年化收益(CAGR)'])}</b> 的收益，
最大回撤 <b>{pct(p_self['最大回撤'])}</b>，夏普 <b>{p_self['夏普比率']:.2f}</b>，
卡玛 <b>{p_self['卡玛比率']:.2f}</b>。相对同期沪深300 全收益基准（CAGR {pct(bench_cagr)}、最大回撤 {pct(bench_mdd)}）
年化超额约 {pct(p_self['年化收益(CAGR)'] - bench_cagr)}。<br>
{exp_note}<br>
需要强调：超额收益的 <b>几乎全部来自 2023-2026 段</b>，2019-2022 段{front_rel}基准（详见第六节 6.3），
这不是一个"全时段稳定跑赢"的模型，而是一个"熊市抗跌 + 结构性行情中捕捉行业轮动"的模型。
</div>

<h2 id="s1">一、功能范围与交付内容</h2>

<h3>1.1 功能范围</h3>
<table>
<thead><tr><th>模块</th><th>能力</th><th>状态</th></tr></thead>
<tbody>
<tr><td>数据层</td><td>全市场 ETF 自动发现（枚举 1.8 万个代码段）、批量实时快照、前复权日线分段拼接、本地 CSV 缓存与增量更新</td><td>已实现</td></tr>
<tr><td>标的池</td><td>资产类别映射、同指数去重、流动性/规模/历史长度过滤、点时（point-in-time）可交易判定</td><td>已实现</td></tr>
<tr><td>因子层</td><td>动量（20/60/120 日，支持 skip）、均线偏离、已实现波动、下行波动、风险调整动量、趋势斜率、滚动回撤、量比、RSRS、横截面 z-score / 分位 / 缩尾</td><td>已实现</td></tr>
<tr><td>策略层</td><td>4 个可比策略：动量基线、双动量、多因子、TGAR 自研策略；参数化可开关各模块</td><td>已实现</td></tr>
<tr><td>回测层</td><td>事件驱动日频引擎，T 日信号 / T+1 开盘成交，双边成本、权重漂移、停牌前填、现金仓位</td><td>已实现</td></tr>
<tr><td>绩效层</td><td>CAGR / 波动 / Sharpe / Sortino / MaxDD / Calmar / 胜率 / Alpha / Beta / 信息比率 / 年度月度分解 / 换手与成本</td><td>已实现</td></tr>
<tr><td>信号层</td><td>按最新交易日输出目标持仓权重、市场状态、对应因子值，导出 CSV</td><td>已实现</td></tr>
<tr><td>验证层</td><td>单元测试（含无前视偏差检验）、消融实验、增量归因、子样本稳定性</td><td>已实现</td></tr>
</tbody></table>

<h3>1.2 交付内容</h3>
<table>
<thead><tr><th>文件</th><th>职责</th></tr></thead>
<tbody>
<tr><td><code>etfquant/datafeed.py</code></td><td>腾讯行情数据源：批量快照、前复权日线、640 根分段拼接、并发下载、本地缓存</td></tr>
<tr><td><code>etfquant/universe.py</code></td><td>ETF 全市场扫描（1.8 万代码段）、资产类别映射、同指数去重、流动性过滤、点时成分判定</td></tr>
<tr><td><code>etfquant/factors.py</code></td><td>因子库：动量、均线偏离、波动、下行波动、风险调整动量、趋势斜率、滚动回撤、量比、RSRS、横截面 z-score/分位/缩尾</td></tr>
<tr><td><code>etfquant/strategy.py</code></td><td>4 个策略实现 + 因子预计算缓存 + 权重约束（迭代削峰）</td></tr>
<tr><td><code>etfquant/backtest.py</code></td><td>事件驱动回测：T 日信号 / T+1 开盘成交、双边成本、权重漂移、现金仓位、停牌前填</td></tr>
<tr><td><code>etfquant/metrics.py</code></td><td>绩效指标：CAGR、波动、Sharpe、Sortino、MaxDD、Calmar、胜率、Alpha/Beta、信息比率、年度月度分解</td></tr>
<tr><td><code>etfquant/pipeline.py</code></td><td>把缓存 CSV 装配成回测宽表面板</td></tr>
<tr><td><code>etfquant/report.py</code></td><td>内联 SVG 图表（净值 / 回撤 / 年度柱 / 热力图）与 HTML 渲染</td></tr>
<tr><td><code>etfquant/cli.py</code></td><td>命令行入口</td></tr>
<tr><td><code>scripts/*.py</code></td><td>11 个可执行脚本：扫描、下载、回测、诊断、消融、增量归因、候选对比、信号、报告、<b>逻辑审计</b>、测试运行器</td></tr>
<tr><td><code>tests/test_core.py</code></td><td>9 项单元测试，其中 2 项验证因子无前视偏差</td></tr>
<tr><td><code>data/ · output/</code></td><td>标的池快照、日线缓存、绩效表、净值序列、持仓明细、信号 CSV、本报告</td></tr>
</tbody></table>

<h2 id="s2">二、数据来源与口径</h2>
<p>本机网络环境实测：GitHub、东方财富、新浪行情接口均不可直连，PyPI 与腾讯财经可用。
因此数据管道改为完全基于<b>腾讯财经公开行情接口</b>，不依赖任何付费 Token，也不需要 akshare / tushare。</p>
<table>
<thead><tr><th>用途</th><th>接口</th><th>说明</th></tr></thead>
<tbody>
<tr><td>ETF 全市场发现</td><td><code>qt.gtimg.cn/q=</code>（批量快照）</td><td>按代码段枚举 1.8 万个代码，60 个/批返回，实测全市场扫描 108 秒，命中 945 只 ETF</td></tr>
<tr><td>日线行情</td><td><code>web.ifzq.gtimg.cn/appstock/app/fqkline/get</code></td><td>前复权（qfq），单次上限 640 根，按 2 年窗口分段拼接；259 只 × 8.8 年下载耗时 65 秒</td></tr>
</tbody></table>
<p><b>口径与已知偏差：</b></p>
<ul>
<li>价格为<b>前复权</b>，收益率序列连续；ETF 分红较少，复权影响有限。</li>
<li>回测区间 {date_range}，共 {n_days} 个交易日。面板额外加载 2018 年数据用于 200 日均线预热。</li>
<li><b>幸存者偏差</b>：标的池取自 2026-10-23 时点在市的 ETF，期间清盘退出的基金未纳入，会高估策略容量与收益。</li>
<li><b>成分时点</b>：每日重新判定"上市满 250 交易日 + 成交额达标"，不使用今日成分倒推历史，避免前视。</li>
<li>成交额用"收盘价 × 成交量(手) × 100"近似，与真实成交额存在小额误差。</li>
</ul>

<h2 id="s3">三、可复用的开源参考</h2>
<p>GitHub 无法直连，通过检索取得以下真实项目的公开说明并作为实现参照，代码为本项目独立重写（不复用其代码，仅复用方法与结构）：</p>
<table>
<thead><tr><th>项目</th><th>复用内容</th><th>本项目差异</th></tr></thead>
<tbody>
<tr><td><b>AlphaRotationLab / A-Share-ETF-Rotation-Model</b><br><span style="color:#8b98ad;font-size:12px">MIT</span></td>
<td>策略骨架：20 日动量排名 + 200 日均线趋势过滤 + Top3 等权 + 每 5 交易日调仓 + 单边 0.15% 成本。本项目 <code>S1-动量基线</code> 即其忠实复刻。</td>
<td>数据源由 efinance 换为腾讯行情；标的池从 14 只扩展到 260 只（含跨境、商品、债券）；成本改为单边 0.10%</td></tr>
<tr><td><b>mosesmc52 / etf-trend-rp-vt</b></td>
<td>分层架构：趋势过滤 → 风险平价权重 → 波动率目标 → 现金/防守袖套 → 定期调仓。本项目第五层的"未用敞口配债券"沿用该思路</td>
<td>把风险平价权重替换为等权（消融显示前者在 A 股 ETF 上更差）；不接实盘 API</td></tr>
<tr><td><b>ttz-cn / sector_rotation_updateV3GRU</b></td>
<td>因子分域设计（技术面 / 拥挤度 / 基本面）与"拥挤度阈值调整仓位"的思想，启发本项目第 2 层的量比过滤</td>
<td>不引入 GRU 与组合优化（样本量不足、易过拟合），改用规则化打分</td></tr>
<tr><td><b>Antonacci 双动量</b>（公开方法论）</td>
<td>相对动量 + 绝对动量双重闸门</td>
<td>实现为 <code>S2-双动量</code> 作为对照策略</td></tr>
<tr><td><b>empyrical / quantstats</b></td>
<td>绩效指标定义口径（CAGR、Sharpe、Sortino、Calmar、MaxDD）</td>
<td>按同一口径自行实现于 <code>metrics.py</code>，避免引入重型依赖</td></tr>
</tbody></table>
<div class="note">
<b>复刻校验。</b>参考项目报告其简化模型在 2019 年大幅跑输基准（超额 -35.5%），原因是动量+200 日均线在熊转牛初期反应滞后。
本项目 <code>S1</code> 在 2019 年录得 {pct(AV(2019, 'S1-动量基线'))}，基准 {pct(AV(2019, BENCH))}，
超额 {pct(AV(2019, 'S1-动量基线') - AV(2019, BENCH))}，<b>方向与量级一致</b>，
可作为实现正确性的交叉验证。
</div>

<h2 id="s4">四、策略设计：TGAR</h2>
<p><b>Trend-Gated Adaptive Rotation（趋势闸门自适应轮动）</b>——五层结构，每层只解决一个明确问题。</p>

<h3>第 1 层｜市场状态闸门（决定"买多少"）</h3>
<p>以沪深300ETF（510300）为基准，用三项条件加权打分得到 regime ∈ [0,1]：</p>
<ul>
<li>收盘价 &gt; MA200（长期趋势）→ <code>+0.4</code></li>
<li>收盘价 &gt; MA60（中期趋势）→ <code>+0.3</code></li>
<li>20 日收益 &gt; 0（短期动量）→ <code>+0.3</code></li>
</ul>
<p>映射为权益目标敞口：<code>regime ≥ 0.7 → 100%</code>；<code>0.35 ≤ regime &lt; 0.7 → 50%</code>；<code>regime &lt; 0.35 → 50%</code>（不空仓）。
未使用的敞口配置到池内 20 日动量最强的债券 ETF。</p>

<h3>第 2 层｜标的初筛（决定"能不能买"）</h3>
<ul>
<li>上市满 <b>250 个交易日</b>（保证 200 日均线可计算）</li>
<li>近 20 日日均成交额 ≥ <b>2000 万元</b></li>
<li>收盘价 &gt; <b>MA60</b>（只买上升趋势，不抄底）</li>
<li>量比（5 日均量 / 60 日均量）&lt; <b>3.0</b>（剔除短期成交异常放大的拥挤品种）</li>
</ul>

<h3>第 3 层｜横截面打分（决定"买哪只"）</h3>
<p><code>score = z(R20)</code>，其中 R20 为剔除最近 5 个交易日的 20 日收益（规避短期反转）。
z 值按当日全池横截面标准化，2%/98% 缩尾。</p>
<div class="note">
此处与直觉相反：<b>四因子合成（R20 + R60 + Sharpe60 + 低波动）表现显著更差</b>
（CAGR {pct(LG('多因子'))} vs {pct(LG('闸门改为仅降半仓'))}，见第六节第 8 行）。原因是 A 股 ETF 的行业轮动周期以 3-8 周为主，
中期动量与低波动因子会系统性低配刚启动的高弹性品种。故默认使用纯短期动量。
</div>

<h3>第 4 层｜权重构造（决定"买多少只、各多少"）</h3>
<p>取 score 前 <b>3</b> 只等权，单只上限 45%。等权在消融中优于波动率倒数加权
（{pct(LG('闸门改为仅降半仓'))} vs {pct(LG('波动率倒数加权'))}）。</p>

<h3>第 5 层｜换手与风险控制</h3>
<ul>
<li><b>排名缓冲带</b>：现有持仓只要仍位于 top(2N)=top6 即保留。单独贡献 CAGR {pct(LG('排名缓冲带') - LG('拥挤度'), 1)}
（{pct(LG('拥挤度'))} → {pct(LG('排名缓冲带'))}）。</li>
<li><b>回撤熔断</b>：原设计在净值自 60 日高点回撤超 10% 时减半仓，<b>经消融验证为负贡献</b>
（开启后 CAGR 从 {pct(AB('完整模型'))} 降到 {pct(AB('回撤熔断'))}，回撤也未见改善），已默认关闭。
原因是该规则在下跌末期减仓，形成追跌。</li>
<li><b>调仓频率</b>：每 5 个交易日。改为 10 日 CAGR 降至 {pct(AB('调仓周期 10'))}，
20 日降至 {pct(AB('调仓周期 20'))}，说明 A 股行业轮动窗口很短，调仓过慢会显著失效。</li>
</ul>

<h3>4.1 选股条件汇总（可直接执行的规则）</h3>
<table>
<thead><tr><th>#</th><th>条件</th><th>阈值</th><th>作用</th></tr></thead>
<tbody>
<tr><td>1</td><td>标的范围</td><td>沪深 ETF，剔除货币，同指数只留成交额最大者</td><td>避免同质重复暴露</td></tr>
<tr><td>2</td><td>上市时长</td><td>≥ 250 交易日</td><td>因子可算、规避次新炒作</td></tr>
<tr><td>3</td><td>流动性</td><td>20 日均成交额 ≥ 2000 万</td><td>实盘可容纳资金与冲击成本</td></tr>
<tr><td>4</td><td>趋势</td><td>收盘 &gt; MA60</td><td>只做多头趋势</td></tr>
<tr><td>5</td><td>拥挤度</td><td>量比(5/60) &lt; 3.0</td><td>规避情绪过热后的反转</td></tr>
<tr><td>6</td><td>排序</td><td>R20(skip 5) 降序</td><td>核心 alpha 来源</td></tr>
<tr><td>7</td><td>持仓数</td><td>Top 3，等权，单只 ≤45%</td><td>集中度与分散度的平衡</td></tr>
<tr><td>8</td><td>总仓位</td><td>由 regime 决定 100%/50%</td><td>系统性风险控制</td></tr>
<tr><td>9</td><td>防守</td><td>剩余敞口配 R20 最强债券 ETF</td><td>提升组合夏普</td></tr>
<tr><td>10</td><td>调仓</td><td>每 5 交易日，T+1 开盘执行</td><td>匹配轮动周期，规避收盘价前视</td></tr>
</tbody></table>

<h2 id="s5">五、回测结果</h2>
{fig_nav}
<p style="color:#8b98ad;font-size:12.5px">单边成本 0.10%（佣金 0.03% + 冲击 0.07%）。S1 与 TGAR 净值形态接近，但 TGAR 在 2022、2024 的回撤明显更浅。</p>
{fig_dd}
{fig_ann}
<h3>5.1 核心绩效</h3>
{perf_html}
{ann_html}
{fig_heat}
<div class="note">
<b>四点必须说明的观察：</b>
<ol>
<li><b>2019 年跑输最明显</b>（TGAR {pct(AV(2019))} vs 基准 {pct(AV(2019, BENCH))}）。
动量策略在熊转牛首年存在结构性滞后——趋势闸门需要时间才转向多头，普涨行情里它也拿不到超额。</li>
<li><b>2022 年防守有效</b>：TGAR {pct(AV(2022))}，基准 {pct(AV(2022, BENCH))}。闸门提前降到半仓、剩余配债券起了作用。</li>
<li><b>2024 与 2026 年是超额主要来源</b>：2024 年 TGAR {pct(AV(2024))}（基准 {pct(AV(2024, BENCH))}），
2026 年至今 {pct(AV(2026))}（基准 {pct(AV(2026, BENCH))}）。这两段都是 A 股结构性分化、行业轮动剧烈的环境，
正是动量策略最擅长的场景。</li>
<li><b>换手 {p_self['年均换手(单边)']:.1f} 倍/年</b>，按单边 0.10% 计，年化摩擦成本约
{pct(2 * p_self['年均换手(单边)'] * 0.001)}，已计入净值。</li>
</ol>
</div>

<h2 id="s6">六、验证：增量归因、消融与子样本</h2>
<p>为回答"每个模块是否真的有用"，做了两轮实验。第一轮从复杂模型做减法（消融），
第二轮从最朴素的纯动量做加法（增量归因）。两轮结论并不完全一致，这本身就是信息。</p>

<h3>6.1 增量归因：从纯动量逐项叠加</h3>
<p>起点刻意设为最朴素的形态：20 日动量排序、Top3、等权、满仓、无任何过滤与闸门。</p>
{ladder_html}
<p><b>解读：</b></p>
<ul>
<li>纯动量起点 CAGR {pct(LG('纯动量'))}，已经很强——这说明<b>核心 alpha 来自动量本身</b>，
其余模块主要是在风险维度做交换。</li>
<li>单独加 MA60 趋势过滤降到 {pct(LG('趋势过滤'))}，再叠加拥挤度过滤回升到 {pct(LG('拥挤度'))}，
说明二者存在交互：趋势过滤剔除的是"排名高但仍在均线下方"的反弹品种，拥挤度过滤则确实有效。</li>
<li>缓冲带（第 4 行）把 CAGR 推到 {pct(LG('排名缓冲带'))}，Sharpe 从 {LG('拥挤度','夏普比率'):.2f}
升到 {LG('排名缓冲带','夏普比率'):.2f}，是唯一同时改善收益与风险调整后收益的模块。</li>
<li>闸门（第 5 行）以 {pct(LG('排名缓冲带') - LG('市场状态闸门'), 1)} 的收益换取
{pct(abs(LG('市场状态闸门','最大回撤')) - abs(LG('排名缓冲带','最大回撤')), 1)} 的回撤改善——
这是<b>风险预算的重新定价</b>，不是 alpha。</li>
<li>熔断（第 6 行）确认负贡献：收益从 {pct(LG('市场状态闸门'))} 降到 {pct(LG('回撤熔断'))}，
回撤反而从 {pct(LG('市场状态闸门','最大回撤'))} 恶化到 {pct(LG('回撤熔断','最大回撤'))}。</li>
<li>"闸门只降半仓、不空仓"（第 7 行）优于"允许空仓"（第 5 行），Calmar 从
{LG('市场状态闸门','卡玛比率'):.2f} 提升到 {LG('闸门改为仅降半仓','卡玛比率'):.2f}。
A 股熊市常有强反弹，完全离场的机会成本过高。</li>
<li>多因子（第 8 行 {pct(LG('多因子'))}）、波动率倒数加权（第 9 行 {pct(LG('波动率倒数加权'))}）、
Top5 分散（第 10 行 {pct(LG('Top5'))}）三者均不如第 7 行，说明<b>越简单越有效</b>。</li>
</ul>

<h3>6.2 模块消融（V1 体系：多因子 + 波动率加权 + 全闸门 + 熔断）</h3>
{abl_html}
<p>该表用于对比"复杂版"与"精简版"的相对优劣。值得注意的是<b>关闭市场状态闸门后 CAGR 反而升到 {pct(AB('关闭市场状态闸门'))}（完整模型 {pct(AB('完整模型'))}），但回撤从 {pct(AB('完整模型','最大回撤'))} 恶化到 {pct(AB('关闭市场状态闸门','最大回撤'))}</b>——
说明闸门在复杂版里的贡献同样只是风险维度。最终版本因此采用"纯动量 + 等权 + 半仓闸门"的精简组合。</p>

<h3>6.3 子样本稳定性</h3>
{sub_html}
<p>这是本报告最需要审慎对待的一张表。<b>策略的超额收益高度集中在 2023-2026 段</b>
（CAGR {pct(sub2.loc['后段 2023-2026', '最终模型_CAGR'])} vs 基准 {pct(sub2.loc['后段 2023-2026', '基准CAGR'])}），
而 2019-2022 段{front_rel}基准（{pct(sub2.loc['前段 2019-2022', '最终模型_CAGR'])} vs
{pct(sub2.loc['前段 2019-2022', '基准CAGR'])}），但最大回撤明显更浅
（{pct(sub2.loc['前段 2019-2022', '最终模型_MaxDD'])} vs {pct(sub2.loc['前段 2019-2022', '基准MaxDD'])}）。</p>
<p>合理的解读是：该策略在<b>震荡市与结构性行情</b>中效率最高（行业分化大、轮动清晰），
在<b>全面普涨的贝塔行情</b>中会跑输（2019、2024 的 9·24），在<b>系统性下跌</b>中提供保护（2022）。
接受这个特征，比相信一个"全天候跑赢"的故事更实际。</p>

<h2 id="s7">七、风险与局限</h2>
<ol>
<li><b>样本内优化偏差。</b>参数选择基于同一段 2019-2026 数据，虽然做了增量归因与子样本检验，
但仍无法排除对 2023-2026 结构性行情的拟合。{exp_note or ''}
真实样本外表现应显著低于 {pct(p_self['年化收益(CAGR)'])} 的样本内年化。</li>
<li><b>幸存者偏差。</b>标的池为当前在市 ETF，期间清盘的基金未纳入。</li>
<li><b>跨境 ETF 的敞口错配（已知缺陷）。</b>闸门以沪深300 判断市场状态，但标的池包含 62 只跨境 ETF 与 4 只商品 ETF。
当 A 股走弱而美股/商品走强时（如当前 2026-10-23 信号），跨境品种的仓位会被 A 股状态误砍。
这属于设计取舍而非 bug，但实盘中应改为按资产类别分别判定状态。</li>
<li><b>无盘中执行建模。</b>假设 T+1 开盘价全额成交；跨境 ETF 存在溢价与 T+2 交收差异，实际冲击可能高于 0.07%。</li>
<li><b>容量约束。</b>Top3 集中持仓 + 中小主题 ETF 流动性有限，策略容量估计在数亿元量级，不适合大规模资金。</li>
<li><b>参数敏感性。</b>调仓周期从 5 日改到 10 日，CAGR 从 {pct(p_self['年化收益(CAGR)'])} 掉到 {pct(AB('调仓周期 10'))}——策略对这一点极其敏感，
实盘若因故延迟调仓，收益会显著衰减。</li>
<li><b>不构成投资建议。</b>回测基于历史数据，未来不保证重现。</li>
</ol>

<h2 id="s8">八、当前选股信号（{sig['代码'].ne('').sum()} 只）</h2>
<p>信号日期 <b>{sig_fp.stem.replace('signal_', '')}</b>，按最新交易日收盘数据计算，次日开盘执行。</p>
{sig_html}

<h2 id="s9">九、使用方式</h2>
<pre style="background:#161b24;border:1px solid #2a3244;border-radius:10px;padding:16px;overflow-x:auto;font-size:12.5px;color:#c3ccda;line-height:1.7">
# 1. 扫描全市场 ETF（约 110 秒）
python scripts/build_universe.py

# 2. 下载历史日线（约 65 秒，之后走缓存）
python scripts/download_history.py --start 2018-01-01 --top 260

# 3. 四策略回测
python scripts/run_backtest.py --tag main

# 4. 生成最新选股信号
python scripts/run_signal.py

# 5. 验证实验
python scripts/ablation.py      # 模块消融
python scripts/grid2.py         # 增量归因
python -m etfquant research candidates   # 候选版本对比

# 6. 生成本报告
python scripts/make_report.py

# 单元测试
python -m pytest tests -q
</pre>

<h2 id="s10">十、资产池构成</h2>
{pool_html}
<p style="color:#8b98ad;font-size:12.5px">去重后共 {len(inv)} 只 ETF 有完整历史数据，覆盖宽基、行业、主题、跨境、商品、债券六类。</p>
"""

    footer = (
        "本报告由 etf-quant 自动生成。数据来源：腾讯财经公开行情接口。"
        "回测不代表未来收益，仅供研究与教育用途，不构成任何投资建议。"
        f"生成时间 {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}。"
    )
    html = render_page(
        "ETF 量化选股工具：策略设计与回测验证报告",
        f"TGAR 趋势闸门自适应轮动策略 ｜ 标的池 A 股 ETF × 260 ｜ 回测区间 {date_range} ｜ 单边成本 0.10%",
        body,
        footer,
    )
    fp = OUT / "ETF量化策略研究报告.html"
    atomic_write_text(fp, html, encoding="utf-8")
    print(f"[OK] 报告已生成: {fp}  ({len(html) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
