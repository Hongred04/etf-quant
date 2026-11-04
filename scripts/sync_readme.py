"""
把 README.md 里的绩效数字同步为 output/*.csv 的实际值。

为什么单独写一个脚本
------------------
README 里散落着十来张表和一堆正文数字。每次策略口径变动（改排序、改成本、
改年化方式）都要手工改一遍，改漏就出现"表里一个数、正文另一个数"。
这里统一从产物 CSV 取数重建，避免人肉对照。

用法
----
    python scripts/sync_readme.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from etfquant import metrics  # noqa: E402
from etfquant.expectation import load as load_expectation  # noqa: E402

OUT = ROOT / "output"
# 完整档案（全部表格与正文数字）搬到了 docs/README_full.md；根 README 只是精简门面，
# 由本脚本末尾单独维护 exp 口径块。
README = ROOT / "docs" / "README_full.md"
README_HOME = ROOT / "README.md"

NAME_MAP = {
    "S1-动量基线": "S1-动量基线（参考项目复刻）",
    "S2-双动量": "S2-双动量",
    "S3-多因子": "S3-多因子",
    "S4-TGAR(自研)": "**S4-TGAR（自研）**",
}
LADDER_NOTE = {
    "纯动量": "核心 alpha 来自动量本身",
    "趋势过滤": "单独加有害",
    "拥挤度": "与趋势过滤存在交互",
    "排名缓冲带": "同时改善收益与夏普、并降低换手",
    "市场状态闸门": "以收益换回撤",
    "回撤熔断": "**负贡献，已剔除**",
    "闸门改为仅降半仓": "优于允许空仓",
    "多因子": "**劣于纯动量，默认关闭**",
    "波动率倒数加权": "略劣于等权",
    "Top5": "分散过度",
}


def row_of(df: pd.DataFrame, kw: str) -> pd.Series:
    hit = [i for i in df.index if kw in str(i)]
    if not hit:
        raise KeyError(f"未找到行：{kw}")
    return df.loc[hit[0]]


def _collect_test_count() -> int | None:
    """
    数一遍测试（`--collect-only`，约 3 秒，不实际运行）；失败返回 None，同步不因此中断。

    四个文档里都写着"NNN 项单测"。2026-10-30 发现 README 写着 126 项（实际 217）、
    AGENTS.md 写着 176 项——漂了上百，而且没人会注意。这种数字机器能算，就不该手写。
    """
    try:
        out = subprocess.run(
            [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+) tests? collected", out.stdout or "")
    return int(m.group(1)) if m else None


def _sync_test_counts() -> int | None:
    """把 README / AGENTS / scripts/README 里写死的单测数量刷成实际值。

    用 `newline=""` 读写：这几个文件都是 CRLF，默认的通用换行会把整篇换成 LF，
    制造一份"整文件重写"的假 diff。
    """
    n = _collect_test_count()
    if not n:
        print("[WARN] 数不出测试数量，跳过单测数同步")
        return None
    targets = (
        # 这两行随 README 拆分搬进了完整档案（docs/README_full.md）。
        # 拆完后忘了改这里 → 同步静默失效（re.sub 不匹配就不改，只报"无变化"）。
        (ROOT / "docs" / "README_full.md",
         [(r"\| 验证层 \| \d+ 项单元测试", f"| 验证层 | {n} 项单元测试"),
          (r"├── tests/ +\d+ 项单元测试", f"├── tests/              {n} 项单元测试")]),
        (ROOT / "AGENTS.md", [(r"# \d+ 项单测", f"# {n} 项单测"),
                              (r"\| \d+ 项单测 \|", f"| {n} 项单测 |")]),
        (ROOT / "scripts" / "README.md", [(r"单元测试（\d+ 项）", f"单元测试（{n} 项）")]),
    )
    for fp, pats in targets:
        if not fp.exists():
            continue
        with open(fp, "r", encoding="utf-8", newline="") as fh:
            txt = fh.read()
        new = txt
        for pat, rep in pats:
            new = re.sub(pat, rep, new, count=1)
        if new != txt:
            with open(fp, "w", encoding="utf-8", newline="") as fh:
                fh.write(new)
            print(f"[OK] {fp.name} 单测数 → {n}")
    return n


def main() -> None:
    # 先刷文档里的计数，后面的读取就能拿到正确的数字
    _sync_test_counts()
    nav = pd.read_csv(OUT / "nav_main.csv", index_col=0, parse_dates=True)
    perf = pd.read_csv(OUT / "perf_main.csv", index_col=0).set_index("策略")
    ladder = pd.read_csv(OUT / "ladder.csv", index_col=0)
    sub = pd.read_csv(OUT / "subsample2.csv", index_col=0)
    abl = pd.read_csv(OUT / "ablation.csv", index_col=0)

    self_nav = nav["S4-TGAR(自研)"].dropna()
    bench = nav["基准-沪深300"].dropna()
    years = metrics.years_elapsed(self_nav)

    tg = perf.loc["S4-TGAR(自研)"]
    s1 = perf.loc["S1-动量基线"]
    b_cagr = metrics.cagr(bench)
    b_mdd = metrics.max_drawdown(bench)
    b_sharpe = metrics.sharpe(bench)
    b_vol = metrics.ann_vol(bench)
    b_cum = bench.iloc[-1] / bench.iloc[0] - 1
    b_calmar = metrics.calmar(bench)

    s = README.read_text(encoding="utf-8")
    orig = s

    d0 = self_nav.index[0].strftime("%Y-%m-%d")
    d1 = self_nav.index[-1].strftime("%Y-%m-%d")

    # ---------- 核心结论 ----------
    # 日期不再写死：以前写死 "2019-01-02 ~ 2026-10-23"，数据更新后正文与表格会出现
    # 两个不同的区间，而看起来都"正常"。
    s = re.sub(
        r"在 \d{4}-\d{2}-\d{2} ~ \d{4}-\d{2}-\d{2} 的回测中取得年化 \*\*[\d.]+%\*\*、最大回撤 \*\*[-\d.]+%\*\*、\n"
        r"夏普 \*\*[\d.]+\*\*、卡玛 \*\*[\d.]+\*\*（基准沪深300 全收益：年化 [\d.]+%、回撤 [-\d.]+%、夏普 [\d.]+）。",
        f"在 {d0} ~ {d1} 的回测中取得年化 **{tg['年化收益(CAGR)']*100:.1f}%**、"
        f"最大回撤 **{tg['最大回撤']*100:.1f}%**、\n"
        f"夏普 **{tg['夏普比率']:.2f}**、卡玛 **{tg['卡玛比率']:.2f}**"
        f"（基准沪深300 全收益：年化 {b_cagr*100:.1f}%、回撤 {b_mdd*100:.1f}%、夏普 {b_sharpe:.2f}）。",
        s, count=1,
    )

    # ---------- 第六节区间 ----------
    s = re.sub(
        r"(区间 )\d{4}-\d{2}-\d{2} ~ \d{4}-\d{2}-\d{2}（\d+ 个交易日）",
        lambda m: f"{m.group(1)}{d0} ~ {d1}（{len(self_nav)} 个交易日）",
        s, count=1,
    )

    # ---------- 绩效表 ----------
    rows = ["| 策略 | 累计收益 | CAGR | 年化波动 | 夏普 | 最大回撤 | 卡玛 | 超额收益 | 换手/年 |",
            "|---|---|---|---|---|---|---|---|---|"]
    for name, r in perf.iterrows():
        rows.append(
            f"| {NAME_MAP.get(name, name)} | {r['累计收益']*100:.1f}% | {r['年化收益(CAGR)']*100:.1f}% | "
            f"{r['年化波动']*100:.1f}% | {r['夏普比率']:.2f} | {r['最大回撤']*100:.1f}% | "
            f"{r['卡玛比率']:.2f} | {r['超额收益']*100:.1f}% | {r['年均换手(单边)']:.1f} |"
        )
    rows.append(
        f"| 基准 沪深300ETF | {b_cum*100:.1f}% | {b_cagr*100:.1f}% | {b_vol*100:.1f}% | "
        f"{b_sharpe:.2f} | {b_mdd*100:.1f}% | {b_calmar:.2f} | — | — |"
    )
    s = re.sub(
        r"\| 策略 \| 累计收益 \| CAGR \| 年化波动 \| 夏普 \| 最大回撤 \| 卡玛 \| 超额收益 \| 换手/年 \|\n"
        r"\|---\|---\|---\|---\|---\|---\|---\|---\|---\|\n(?:\|.*\n)+",
        "\n".join(rows) + "\n", s, count=1,
    )

    # ---------- 与 S1 的对比表述 ----------
    cum_gap = (tg["累计收益"] - s1["累计收益"]) * 100
    dd_gap = abs(s1["最大回撤"] - tg["最大回撤"]) * 100
    sh_gap = tg["夏普比率"] - s1["夏普比率"]
    ca_gap = tg["卡玛比率"] - s1["卡玛比率"]
    rel = "更高" if cum_gap > 0 else "低"
    para = (f"TGAR 的累计收益 {tg['累计收益']*100:.1f}%，比 S1 的 {s1['累计收益']*100:.1f}% "
            f"{rel} {abs(cum_gap):.1f}pct；但风险调整表现明显更优：回撤比 S1 浅 {dd_gap:.1f}pct，"
            f"Sharpe 高 {sh_gap:.2f}，Calmar 高 {ca_gap:.2f}。")
    s = re.sub(r"TGAR 与 S1 的总收益接近（[^）]*），但风险调整表现明显更优：[^\n]*",
               para, s, count=1)

    # ---------- 年度收益表 ----------
    ann = pd.DataFrame({c: metrics.annual_returns(nav[c].dropna()) for c in nav.columns})
    ann.index = ann.index.year
    yr = ["| 年份 | 基准 | S1 | TGAR |", "|---|---|---|---|"]
    for y in ann.index:
        yr.append(f"| {y} | {ann.loc[y, '基准-沪深300']*100:.1f} | "
                  f"{ann.loc[y, 'S1-动量基线']*100:.1f} | {ann.loc[y, 'S4-TGAR(自研)']*100:.1f} |")
    s = re.sub(r"\| 年份 \| 基准 \| S1 \| TGAR \|\n\|---\|---\|---\|---\|\n(?:\|.*\n)+",
               "\n".join(yr) + "\n", s, count=1)

    # ---------- 增量归因表 ----------
    lr = ["| 步骤 | CAGR | 夏普 | 最大回撤 | 换手/年 | 结论 |", "|---|---|---|---|---|---|"]
    for idx, r in ladder.iterrows():
        key = next((k for k in LADDER_NOTE if k in str(idx)), None)
        lr.append(f"| {str(idx).split('. ', 1)[-1]} | {r['年化收益(CAGR)']*100:.2f}% | "
                  f"{r['夏普比率']:.2f} | {r['最大回撤']*100:.2f}% | {r['年均换手']:.1f} | "
                  f"{LADDER_NOTE.get(key, '')} |")
    s = re.sub(r"\| 步骤 \| CAGR \| 夏普 \| 最大回撤 \| 换手/年 \| 结论 \|\n"
               r"\|---\|---\|---\|---\|---\|---\|\n(?:\|.*\n)+",
               "\n".join(lr) + "\n", s, count=1)

    # ---------- 子样本表 ----------
    sr = ["| 区间 | 基准 CAGR | 纯动量 CAGR | TGAR CAGR | 基准 MaxDD | TGAR MaxDD |",
          "|---|---|---|---|---|---|"]
    for i, r in sub.iterrows():
        sr.append(f"| {i} | {r['基准CAGR']*100:.1f}% | {r['纯动量_CAGR']*100:.1f}% | "
                  f"{r['最终模型_CAGR']*100:.1f}% | {r['基准MaxDD']*100:.1f}% | "
                  f"{r['最终模型_MaxDD']*100:.1f}% |")
    s = re.sub(r"\| 区间 \| 基准 CAGR \| 纯动量 CAGR \| TGAR CAGR \| 基准 MaxDD \| TGAR MaxDD \|\n"
               r"\|---\|---\|---\|---\|---\|---\|\n(?:\|.*\n)+",
               "\n".join(sr) + "\n", s, count=1)

    # ---------- 正文里的零散数字 ----------
    multi = row_of(ladder, "多因子")["年化收益(CAGR)"]
    best = row_of(ladder, "闸门改为仅降半仓")["年化收益(CAGR)"]
    abl_full = row_of(abl, "完整模型")["年化收益(CAGR)"]
    abl_dd = row_of(abl, "回撤熔断")["年化收益(CAGR)"]
    abl_10 = row_of(abl, "调仓周期 10")["年化收益(CAGR)"]
    abl_20 = row_of(abl, "调仓周期 20")["年化收益(CAGR)"]

    subs = [
        (r"\*\*多因子合成劣于纯短期动量\*\*（[\d.]+% vs [\d.]+%）",
         f"**多因子合成劣于纯短期动量**（{multi*100:.1f}% vs {best*100:.1f}%）"),
        (r"收益从 [\d.]+% 降到 [\d.]+%，回撤也未见改善。",
         f"收益从 {abl_full*100:.1f}% 降到 {abl_dd*100:.1f}%，回撤也未见改善。"),
        (r"5 日 → [\d.]+%，10 日 → [\d.]+%，20 日 → [\d.]+%。",
         f"5 日 → {tg['年化收益(CAGR)']*100:.1f}%，10 日 → {abl_10*100:.1f}%，"
         f"20 日 → {abl_20*100:.1f}%。"),
        (r"CAGR 从 [\d.]+% 掉到 [\d.]+%",
         f"CAGR 从 {tg['年化收益(CAGR)']*100:.1f}% 掉到 {abl_10*100:.1f}%"),
        (r"但 CAGR 也从 [\d.]+% 掉到 [\d.]+%",
         f"但 CAGR 也从 {tg['年化收益(CAGR)']*100:.1f}% 掉到 {abl_10*100:.1f}%"),
        (r"真实样本外表现应显著低于 [\d.]+%。",
         f"真实样本外表现应显著低于 {tg['年化收益(CAGR)']*100:.1f}%。"),
    ]
    for pat, rep in subs:
        s = re.sub(pat, rep, s)

    # 换手成本
    turn = float(row_of(abl, "完整模型")["年均换手"])
    s = re.sub(r"\*\*`换手 × 2 × 0\.10% = [\d.]+%`\*\*",
               f"**`换手 × 2 × 0.10% = {turn*2*0.001*100:.1f}%`**", s, count=1)

    # ---------- 对外口径（6.2 节表格 + 核心结论里的三处数字） ----------
    exp = load_expectation(OUT / "overfit_summary.json",
                           sample_cagr=float(tg["年化收益(CAGR)"]))
    if exp:
        rows = [
            "| 口径 | 值 | 含义 |",
            "|---|---|---|",
            f"| 回测年化（样本内上限） | {exp['sample']*100:.2f}% | 把\"挑参数的运气\"全部算成本事 |",
            f"| **对外预期** | **{exp['label']}** | **可以对外引用的数字** |",
            f"| 锚点：滚动前向 | {exp['anchor']*100:.2f}% | 每 6 个月只用过去 2 年重挑一次参数，拼出的纯样本外年化 |",
            f"| 折扣 | −{exp['discount']*100:.1f}% | 挑选过程本身吃掉的收益 |",
            f"| PBO | {exp['pbo']*100:.1f}% | 样本内冠军在样本外掉到中位数以下的概率 |",
        ]
        s = re.sub(
            r"\| 口径 \| 值 \| 含义 \|\n\|---\|---\|---\|\n(?:\|.*\n)+",
            "\n".join(rows) + "\n", s, count=1,
        )
        s = re.sub(r"> \*\*口径\*\*：[\d.]+% 是\*\*样本内\*\*上限",
                   f"> **口径**：{exp['sample']*100:.1f}% 是**样本内**上限", s, count=1)
        s = re.sub(r"可对外引用的是 \*\*[\d.]+%~[\d.]+%\*\*",
                   f"可对外引用的是 **{exp['label']}**", s, count=1)
        s = re.sub(r"纯样本外年化（[\d.]+%）",
                   f"纯样本外年化（{exp['anchor']*100:.2f}%）", s, count=1)

    # ---------- 7.10 诊断快照 ----------
    # 那一节的正文数字是手写的，机器能保证的只有这张表：
    # sync 用它生成（diagnostics.snapshot_rows），audit 用它逐格核对，格式只此一份。
    diag_fp = ROOT / "output" / "diagnostics.json"
    if diag_fp.exists():
        try:
            meta = (json.loads(diag_fp.read_text(encoding="utf-8")) or {}).get("meta") or {}
        except (OSError, json.JSONDecodeError):
            meta = {}
        frozen, dend = meta.get("frozen_at"), meta.get("end")
        if frozen and dend:
            try:
                from etfquant.diagnostics import snapshot_rows  # noqa: PLC0415
                dg = json.loads(diag_fp.read_text(encoding="utf-8")) or {}
                tbl = "\n".join(f"| {k} | {v} |" for k, v in snapshot_rows(dg))
                body = (
                    "<!-- diag:begin 由 scripts/sync_readme.py 从 output/diagnostics.json 生成，勿手改 -->\n"
                    "> 诊断快照：见下表。本节正文的数字与下表同源，**下表是机器生成的权威版本**；\n"
                    "> 重跑 `python -m etfquant deep --force` 会同时改写两者，"
                    "`python -m etfquant audit` 逐格核对下表。\n\n"
                    "| 指标 | 值 |\n|---|---|\n" + tbl + "\n<!-- diag:end -->")
                s = re.sub(r"<!-- diag:begin.*?<!-- diag:end -->", body, s, count=1, flags=re.S)
            except (KeyError, TypeError, ValueError) as exc:
                print(f"[WARN] 诊断区块未生成：{exc}")

    if s != orig:
        README.write_text(s, encoding="utf-8")
        print("[OK] docs/README_full.md 已更新")
    else:
        print("[--] docs/README_full.md 无变化")

    # ---------- 根 README（仓库门面）的 exp 口径块 ----------
    # 与档案同一数据源（expectation + perf/nav），保证四端口径不分叉。
    if README_HOME.exists() and exp:
        h = README_HOME.read_text(encoding="utf-8")
        body = (
            "<!-- exp:begin 由 scripts/sync_readme.py 生成，勿手改 -->\n"
            "| 口径 | 值 |\n|---|---|\n"
            f"| 回测年化（样本内上限） | {exp['sample']*100:.2f}% |\n"
            f"| **对外预期** | **{exp['label']}** |\n"
            f"| 锚点：滚动前向（纯样本外） | {exp['anchor']*100:.2f}% |\n"
            f"| 最大回撤 / 夏普 | {tg['最大回撤']*100:.1f}% / {tg['夏普比率']:.2f} |\n"
            f"| 基准：沪深300 全收益 | 年化 {b_cagr*100:.1f}%，回撤 {b_mdd*100:.1f}% |\n"
            f"| 区间 / 标的池 | {self_nav.index[0].year}-{self_nav.index[-1].year}"
            " / 全市场约 260 只 ETF |\n"
            "<!-- exp:end -->")
        h2 = re.sub(r"<!-- exp:begin.*?<!-- exp:end -->", body, h, count=1, flags=re.S)
        if h2 != h:
            README_HOME.write_text(h2, encoding="utf-8")
            print("[OK] 根 README 口径块已更新")
        else:
            print("[--] 根 README 口径块无变化")

    # ---------- 根 README 的核心-卫星对照表 ----------
    # 这张表原来是手写的：combo.json 每天随行情刷新（区间末端推进一天，
    # 年化就可能从 12.8% 变 12.9%），手写表必然隔天就假。
    # audit 的 J 节会逐格核对，这里保证「跑一次 sync 就自动对齐」。
    combo_fp = ROOT / "output" / "combo.json"
    if README_HOME.exists() and combo_fp.exists():
        try:
            cdoc = json.loads(combo_fp.read_text(encoding="utf-8"))
            cfgs = {round(float(r.get("core_weight", -1)), 2): r
                    for r in (cdoc.get("configs") or [])}
            cw = 0.5
            try:
                mdoc = json.loads((ROOT / "output" / "signal_meta.json").read_text(encoding="utf-8"))
                cw = round(float(mdoc.get("core_weight") or cw), 2)
            except (OSError, ValueError):
                pass
            sat, mix = cfgs.get(0.0), cfgs.get(cw)
            if sat and mix:
                d_cagr = (mix["cagr"] - sat["cagr"]) * 100
                d_mdd = abs(sat["mdd"] - mix["mdd"]) * 100
                # "降敞口到同样回撤只有 X%"：取 dd_frontier 里敞口 0.50 那一档
                delev_txt = ""
                dfp = ROOT / "output" / "dd_frontier.json"
                if dfp.exists():
                    try:
                        scean = json.loads(dfp.read_text(encoding="utf-8")).get("scenarios") or []
                        hit = [x for x in scean if "0.50" in str(x.get("name", ""))]
                        if hit:
                            delev_txt = (f"——降敞口到同样回撤只有 "
                                         f"{hit[0]['cagr'] * 100:.1f}% 年化。")
                    except (OSError, ValueError):
                        pass
                body = (
                    "<!-- combo:begin 由 scripts/sync_readme.py 从 output/combo.json 生成，勿手改 -->\n"
                    "> | | 年化 | 最大回撤 | Calmar |\n"
                    "> |---|---|---|---|\n"
                    f"> | 纯 TGAR（旧默认） | {sat['cagr']*100:.1f}% | "
                    f"−{abs(sat['mdd'])*100:.1f}% | {sat['calmar']:.2f} |\n"
                    f"> | **核心 {cw*100:.0f}% + 卫星 {(1-cw)*100:.0f}%（新默认）** | "
                    f"**{mix['cagr']*100:.1f}%** | **−{abs(mix['mdd'])*100:.1f}%** | "
                    f"**{mix['calmar']:.2f}** |\n"
                    ">\n"
                    f"> 收益少 {abs(d_cagr):.1f}pp，回撤少 {d_mdd:.1f}pp。"
                    f"这不是\"降敞口\"{delev_txt}\n"
                    "<!-- combo:end -->")
                h = README_HOME.read_text(encoding="utf-8")
                h2 = re.sub(r"<!-- combo:begin.*?<!-- combo:end -->", body, h, count=1, flags=re.S)
                if h2 != h:
                    README_HOME.write_text(h2, encoding="utf-8")
                    print("[OK] 根 README 核心-卫星表已更新")
                else:
                    print("[--] 根 README 核心-卫星表无变化")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"[WARN] 核心-卫星表未生成：{exc}")

    # ---------- 数字表：AGENTS.md 与 docs/summary_cn.md ----------
    # 样本内年化、对外预期、自助法区间、组合指标全都随行情漂移（区间末端推进一天，
    # 年化就可能变 0.05pp）。三份文档各手写一遍，等于每天手改三处。
    # 这里统一生成，两个文件各有一处 <!-- num:begin --> 块。
    num_docs = [ROOT / "AGENTS.md", ROOT / "docs" / "summary_cn.md"]
    if all(p.exists() for p in num_docs):
        try:
            ci_fp = OUT / "experiments" / "bootstrap_ci.csv"
            ci_row = None
            if ci_fp.exists():
                _ci = pd.read_csv(ci_fp)
                _hit = _ci[_ci["指标"] == "CAGR"]
                if len(_hit):
                    ci_row = _hit.iloc[0]
            lo = float(ci_row["P2.5"]) * 100 if ci_row is not None else None
            hi = float(ci_row["P97.5"]) * 100 if ci_row is not None else None
            p_loss = float(ci_row["P(CAGR<0)"]) * 100 if ci_row is not None else None
            p_bench = float(ci_row["P(CAGR<基准)"]) * 100 if ci_row is not None else None
            ci_worst = None
            if ci_row is not None:
                _mdd = _ci[_ci["指标"] == "最大回撤"]
                if len(_mdd):
                    ci_worst = abs(float(_mdd.iloc[0]["P2.5"])) * 100

            cagr = float(tg["年化收益(CAGR)"]) * 100
            mdd = float(tg["最大回撤"]) * 100
            sharpe = float(tg["夏普比率"])
            uw = metrics.underwater_stats(self_nav) or {}
            uw_txt = ""
            if uw.get("max_days"):
                uw_txt = (f"{uw['max_days'] / 365.25:.1f} 年"
                          f"（{str(uw.get('max_start'))[:7]} → {str(uw.get('max_end'))[:7]}）")

            cw_txt = ""
            mix_txt = ""
            cfp = OUT / "combo.json"
            if cfp.exists():
                _c = json.loads(cfp.read_text(encoding="utf-8"))
                _cfgs = {round(float(r.get("core_weight", -1)), 2): r for r in (_c.get("configs") or [])}
                _cw = 0.5
                _sat, _mix = _cfgs.get(0.0), _cfgs.get(_cw)
                if _sat and _mix:
                    mix_txt = (f"**{_mix['cagr'] * 100:.1f}% / −{abs(_mix['mdd']) * 100:.1f}%"
                               f" / 卡玛 {_mix['calmar']:.2f}**")

            bench_txt = f"同期基准年化 {b_cagr * 100:.1f}%"
            # 负号统一用 U+2212「−」，与文档其余部分一致（ASCII "-" 在正文里看着像连字符）
            ci_txt = (f"**[−{abs(lo):.2f}%, +{hi:.2f}%]**" if lo is not None else "—")

            agents_body = (
                "<!-- num:begin 由 scripts/sync_readme.py 生成，勿手改 -->\n"
                "| 口径 | 值 |\n|---|---|\n"
                f"| 样本内年化 | **{cagr:.1f}%**（回撤 −{abs(mdd):.1f}%，夏普 {sharpe:.2f}） |\n"
                f"| **对外预期** | **{exp['label']}**（锚点 {exp['anchor'] * 100:.1f}%，滚动前向样本外） |\n"
                f"| 95% 置信区间 | {ci_txt}（块自助法 20 日块 × 2000 次，7.25） |\n"
                f"| 亏钱概率 / 跑不赢沪深300 | "
                f"**{p_loss:.1f}% / {p_bench:.1f}%**（{bench_txt}） |\n"
                f"| **核心-卫星组合**（默认执行） | {mix_txt or '—'}（`docs/combo.md`） |\n"
                f"| 最长水下 | {uw_txt or '—'} |\n"
                "<!-- num:end -->")

            summary_body = (
                "<!-- num:begin 由 scripts/sync_readme.py 生成，勿手改 -->\n"
                "\n| 数字 | 值 | 含义 |\n|---|---|---|\n"
                f"| 样本内回测 | **{cagr:.1f}%/年**（回撤 −{abs(mdd):.1f}%） | 参数在同一段历史上挑过，是上限 |\n"
                f"| 对外口径 | **{exp['label']}** | 扣掉\"挑参数\"后的中枢，可对外引用 |\n"
                f"| 95% 置信区间 | {ci_txt} | 换一段历史可能落在哪 |\n"
                "\n**两个概率必须知道**：\n\n"
                f"- **亏钱的概率 {p_loss:.1f}%**\n"
                f"- **跑不赢\"直接买沪深300\"（{bench_txt}）的概率 {p_bench:.1f}%**\n"
                "\n风险侧：最大回撤 "
                f"−{abs(mdd):.1f}%（样本内）"
                + (f"，自助法下最坏可到 **−{ci_worst:.1f}%**；\n" if ci_worst is not None else "；\n")
                + f"最长水下期 {uw_txt or '—'}。\n"
                "<!-- num:end -->")

            for doc, body in zip(num_docs, [agents_body, summary_body]):
                txt = doc.read_text(encoding="utf-8")
                new = re.sub(r"<!-- num:begin.*?<!-- num:end -->", body, txt, count=1, flags=re.S)
                if new != txt:
                    doc.write_text(new, encoding="utf-8")
                    print(f"[OK] {doc.relative_to(ROOT)} 数字表已更新")
                else:
                    print(f"[--] {doc.relative_to(ROOT)} 数字表无变化")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"[WARN] 数字表未生成：{exc}")

    print(f"     TGAR  CAGR {tg['年化收益(CAGR)']*100:.2f}%  回撤 {tg['最大回撤']*100:.2f}%  "
          f"夏普 {tg['夏普比率']:.3f}  卡玛 {tg['卡玛比率']:.3f}")
    print(f"     基准  CAGR {b_cagr*100:.2f}%  回撤 {b_mdd*100:.2f}%  夏普 {b_sharpe:.3f}")
    print(f"     区间  {years:.2f} 日历年（{len(self_nav)} 个交易日）")


if __name__ == "__main__":
    main()
