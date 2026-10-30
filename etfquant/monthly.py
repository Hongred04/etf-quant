"""
前向验证月报：把「实盘跟踪」里那条纸上净值，按月切一段写成 Markdown。

为什么要专门做月报
------------------
过拟合体检（README 7.5）的结论是：真正的证据只能来自**前向**——
`data/signal_archive.csv` 记的是"当时给出的建议"，之后不会再被参数调整改写。
但那个数字藏在网页的一个页面里，一个月不看就不知道有没有偏离。

月报只做三件事：
1. 这个月有没有调仓、调到了什么；
2. 纸上净值与同期回测**差多少**（这是核心：差值为 0 说明执行没走样，
   持续为负说明有真实的执行损耗，持续为正反而要怀疑记账口径）；
3. 把口径固定写一遍（样本内上限 vs 对外预期），避免看多了曲线就把它当承诺。

为什么把渲染做成纯函数
----------------------
月报要能被测。`render_markdown(data)` 只吃一个 dict、只吐字符串，
不读文件不碰行情，这样"表头缺列""没有交易期""预期区间缺失"这些边界
可以在毫秒级写完测掉，而不必等到月初跑一遍真数据才发现排版塌了。
"""
from __future__ import annotations

from datetime import date, datetime

MONTH_FMT = "%Y-%m"


def prev_month(today: date | None = None) -> str:
    """上一个月（月报在每月 1 日跑，回顾的是刚过去的那一个月）。"""
    first = (today or date.today()).replace(day=1)
    return date.fromordinal(first.toordinal() - 1).strftime(MONTH_FMT)


def month_key(ts) -> str:
    """任意日期（str / date / Timestamp）→ 'YYYY-MM'。"""
    if hasattr(ts, "strftime"):
        return ts.strftime(MONTH_FMT)
    return str(ts)[:7]


def slice_series(series: list[dict], month: str) -> list[dict]:
    """从 `build_track()` 的 series（[{date, paper, backtest}]）里取某个月。"""
    return [p for p in series if str(p.get("date", ""))[:7] == month]


def month_return(points: list[dict], field: str = "paper") -> float | None:
    """月内收益：本月最后一个点 ÷ 本月第一个点 − 1。不足两个点返回 None。"""
    vals = [float(p[field]) for p in points if p.get(field) is not None]
    if len(vals) < 2 or vals[0] <= 0:
        return None
    return vals[-1] / vals[0] - 1


def _pct(v: float | None, d: int = 1, signed: bool = False) -> str:
    if v is None:
        return "—"
    return f"{v * 100:+.{d}f}%" if signed else f"{v * 100:.{d}f}%"


def _pp(v: float | None, d: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v * 100:+.{d}f}pp"


def render_markdown(d: dict) -> str:
    """
    把月报数据渲染成 Markdown。

    `d` 的键见 `scripts/monthly_report.py` 的 `build_month_data()`；
    缺字段一律降级为"—"，不要抛异常——月报的价值在于"每月都有"，
    为了一行表格能显示而让整份月报失败是本末倒置。
    """
    month = d.get("month", "")
    paper = d.get("paper") or {}
    back = d.get("backtest") or {}
    exp = d.get("expect") or {}
    diff = None
    if paper.get("total") is not None and back.get("total") is not None:
        diff = float(paper["total"]) - float(back["total"])

    out = [
        f"# ETF Quant 月报 · {month}",
        "",
        f"> 生成 {d.get('generated_at', '—')} · 数据截至 {d.get('data_date', '—')} · "
        f"纸上净值（按归档信号复算，不是你的实盘账户）",
        "",
        "## 一、跟踪概览",
        "",
        "| 项目 | 值 |",
        "|---|---|",
        f"| 累计期数 | {d.get('count', 0)} 期（真实 {d.get('live', 0)} / 回填 {d.get('backfill', 0)}） |",
        f"| 跟踪区间 | {d.get('first', '—')} ~ {d.get('last', '—')} |",
        f"| 纸上累计 | {_pct(paper.get('total'), 1, signed=True)} |",
        f"| 同期回测 | {_pct(back.get('total'), 1, signed=True)} |",
        f"| 偏差 | {_pp(diff)} |",
        f"| 本月纸上收益 | {_pct(d.get('month_ret'), 2, signed=True)} |",
        f"| 本月回测收益 | {_pct(d.get('month_ret_bt'), 2, signed=True)} |",
        "",
    ]

    periods = d.get("periods") or []
    out += ["## 二、本月调仓", ""]
    if periods:
        out += ["| 信号日 | 买入日 | 期间收益 | 累计 | 持仓 |", "|---|---|---|---|---|"]
        for p in periods:
            hold = "、".join(
                f"{h.get('name', h.get('code', ''))} {float(h.get('weight') or 0) * 100:.0f}%"
                for h in (p.get("holdings") or [])
            )
            out.append(
                f"| {p.get('signal_date', '—')} | {p.get('exec_date') or '未执行'} | "
                f"{_pct(p.get('ret'), 2, signed=True)} | {_pct(p.get('cum'), 1, signed=True)} | "
                f"{hold or '—'} |"
            )
    else:
        out.append("本月没有调仓（5 个交易日一次的节奏下，短月份可能刚好没有）。")
    out.append("")

    nxt = d.get("next_reb")
    out += [
        "## 三、下一期",
        "",
        f"下次调仓 **{nxt or '—'}**"
        + (f"，还有 {d['days_to_next']} 个交易日" if d.get("days_to_next") is not None else "")
        + "。",
        "",
    ]

    if exp.get("label"):
        out += [
            "## 四、口径",
            "",
            f"- 回测年化 {_pct(exp.get('sample'), 1)} 是**样本内上限**，不是预期收益；",
            f"- 可对外引用 **{exp['label']}**，锚点 {_pct(exp.get('anchor'), 1)}"
            f" 来自「每 6 个月重挑一次参数」的纯样本外年化"
            + (f"，挑选带来的折扣约 {_pct(exp.get('discount'), 0)}" if exp.get("discount") is not None else "")
            + "；",
        ]
        if exp.get("pbo") is not None:
            out.append(f"- PBO {_pct(exp['pbo'], 1)}：样本内冠军在样本外掉到中位数以下的概率。")
        out.append("")

    al = d.get("alpha")
    if al:
        out += ["## 五、alpha 还在不在", ""]
        out += ["| 项目 | 值 |", "|---|---|"]
        out.append(f"| 本月残差（因子解释不了的部分） | {_pp(al.get('resid'))} |")
        if al.get("pct") is not None:
            out.append(f"| 历史分位 | {al['pct'] * 100:.0f}%（{al.get('hist_n', 0)} 个月） |")
        if al.get("z") is not None:
            out.append(f"| z | {al['z']:+.1f}（单月噪声 ±{_pct(al.get('sigma_month'))}） |")
        if al.get("expo_month") is not None:
            out.append(f"| 本月平均权益敞口 | {al['expo_month']:.2f}"
                       f"（全期平均 {al.get('expo_avg', float('nan')):.2f}） |")
        if al.get("mkt_month") is not None:
            out.append(f"| 本月沪深300 | {_pct(al['mkt_month'], 1, signed=True)} |")
        if al.get("frozen_at"):
            out.append(f"| β 基准 | 冻结于 {al['frozen_at']}（`output/diagnostics.json`） |")
        if al.get("window_n"):
            out.append(f"| 自真实跟踪起累计残差 | {_pp(al.get('window_cum'))}"
                       f"（{al.get('window_start')} 起 {al['window_n']} 个月） |")
            if al["window_n"] >= 6 and al.get("window_ann") is not None:
                out.append(f"| 折年化 | {_pct(al['window_ann'], 1, signed=True)}"
                           f" ± {_pct(al.get('window_se'))}（1 个标准误） |")
            else:
                out.append(f"| 折年化 | 样本不足（需 ≥6 个月）；当前标准误 ±{_pct(al.get('window_se'))} |")
        out.append("")
        out.append("读数规则（事先写定，不临时解释）：")
        out.append("")
        out.append(f"- 单月 |z| < 2 不解读——月度调仓策略单月 ±{_pct(al.get('sigma_month'))} 属常态；")
        if al.get("months_to_detect"):
            out.append(f"- **\"证明 alpha 存在\"做不到**：要让 {_pct(al.get('alpha_ann'))} 与 0 分开，"
                       f"需要约 {al['months_to_detect']:.0f} 个月（{al['months_to_detect'] / 12:.0f} 年）；")
        if al.get("months_to_5pp"):
            out.append(f"- 累计残差的标准误降到 5pp 需要约 {al['months_to_5pp']:.0f} 个月；")
        out.append("- 能及时抓到的只有**断裂**：某月 |z| > 3 说明执行、记账或数据出了问题，当天查。")
        out.append("- 残差同时包含**选股**与**闸门择时**，本表不拆分：拆开需要条件 β 模型，"
                   "属下一步工作（见 README 7.10）。敞口明显偏离平均值时，先按闸门解释。")
        out.append("- 残差用回测净值计算（纸上净值与它的偏差见第一节）；β 取 `output/diagnostics.json`，"
                   "一经确定即冻结，不每月重估。")
        out.append("")

    warns = d.get("warnings") or []
    if warns:
        out += ["## 六、提示", ""]
        out += [f"- {w}" for w in warns]
        out.append("")

    out += [
        "---",
        "",
        "纸上净值按 T 日收盘出信号、T+1 开盘成交、双边 0.10% 成本复算；"
        "与回测的差异只应来自「信号被冻结」这一件事。回测基于历史数据，不构成投资建议。",
        "",
    ]
    return "\n".join(out)


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")
