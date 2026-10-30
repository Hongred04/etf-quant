"""
对外口径：把「样本内年化」换算成可以对外引用的预期区间。

为什么要有这个模块
------------------
回测年化 17.4% 是**样本内**结果：29 个变体在同一段历史上比过大小，
挑参数本身带来乐观偏差（PBO 43%、样本外夏普衰减 26%）。
滚动前向（每 6 个月用过去 2 年重挑一次参数、纯样本外拼起来）只有 12.6%，
它才是"如果照这套流程实盘、能指望多少"的锚点。

问题在于这个口径要出现在四个地方：README、桌面界面、手机 App、HTML 报告。
四处各写一遍数字，改一次就要改四处，漏一处就会出现「页面写 17.4%、报告写 12.6%」
这种自相矛盾——而且没有任何报错。所以换算规则收在本模块，其他地方只消费结果。

区间怎么取
----------
取锚点**向下取整到 1 个百分点，再各放宽 1pp**：12.62% → 11%~13%。
故意偏保守：向上取整会把 12.62 说成 14%，那是把"最好的情况"当预期。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

#: 锚点两侧各放宽多少（1 个百分点）
BAND_PAD = 0.01


def band_of(anchor: float, pad: float = BAND_PAD) -> tuple[float, float]:
    """锚点 → 对外区间。向下取整再各让 `pad`，宁低不高。"""
    if anchor is None or not math.isfinite(anchor):
        raise ValueError("锚点必须是有限数值")
    low = math.floor(round(anchor * 100, 6)) / 100 - pad
    low = max(0.0, round(low, 4))
    high = round(low + 2 * pad, 4)
    return low, high


def expectation(
    sample_cagr: float | None,
    oos_cagr: float | None,
    pbo: float | None = None,
    n_variants: int | None = None,
) -> dict:
    """
    组装口径字典。四端（README / 桌面 / 手机 / 报告）共用同一份结构。

    `sample_cagr` 是回测年化（样本内上限），`oos_cagr` 是滚动前向锚点。
    锚点缺失时无法给区间——宁可返回 `None` 让界面不显示，也不要临时拿样本内
    数字乘个"打个七折"的系数，那种拍脑袋的折扣比不写更糟。
    """
    if sample_cagr is not None and not math.isfinite(sample_cagr):
        sample_cagr = None
    if oos_cagr is None or not math.isfinite(oos_cagr):
        raise ValueError("缺少滚动前向锚点，无法给出对外区间")

    low, high = band_of(float(oos_cagr))
    out = {
        "band": [low, high],
        "label": f"{low * 100:.0f}%~{high * 100:.0f}%",
        "anchor": round(float(oos_cagr), 4),
        "sample": round(float(sample_cagr), 4) if sample_cagr is not None else None,
        "discount": None,
        "pbo": round(float(pbo), 4) if pbo is not None else None,
        "n_variants": int(n_variants) if n_variants is not None else None,
    }
    if sample_cagr:
        out["discount"] = round(1 - float(oos_cagr) / float(sample_cagr), 4)
    return out


def from_summary(summary: dict | None, sample_cagr: float | None = None) -> dict | None:
    """
    从 `output/overfit_summary.json` 取口径。

    优先用文件里现成的 `expectation`（由 `scripts/overfit_check.py` 写入）；
    老版本文件没有这个字段时，用 `wf_cagr` 现算——这样升级代码后不必先重跑
    那个几分钟的脚本，界面也不会突然空一块。

    `sample` 一律以调用方传入的为准（它来自**当天**的回测产物）：锚点和区间是
    几个月才变一次的研究结论，但"样本内上限"跟着每一次回测走——数据更新一天，
    CAGR 就可能从 17.45% 变 17.34%，若继续沿用文件里的旧值，README 的核心结论
    和口径表会各说各话。折扣也跟着重算。
    """
    if not summary:
        return None
    got = summary.get("expectation")
    if isinstance(got, dict) and got.get("band"):
        if sample_cagr is not None and got.get("sample") != round(float(sample_cagr), 4):
            got = {**got, "sample": round(float(sample_cagr), 4)}
            try:
                got["discount"] = round(1 - float(got["anchor"]) / float(sample_cagr), 4)
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        return got
    anchor = summary.get("wf_cagr")
    if anchor is None:
        return None
    try:
        return expectation(sample_cagr, anchor, summary.get("pbo"), summary.get("n_variants"))
    except ValueError:
        return None


def load(fp: str | Path, sample_cagr: float | None = None) -> dict | None:
    """读 `overfit_summary.json` 并给出径；文件缺失/损坏返回 None（界面不显示即可）。"""
    fp = Path(fp)
    if not fp.exists():
        return None
    try:
        summary = json.loads(fp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return from_summary(summary, sample_cagr)
