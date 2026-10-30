"""
缠论结构的最小可计算实现（K线包含 → 分型 → 笔），只用于**检验可回测性**。

为什么只做到"笔"，不做线段/中枢/背驰
------------------------------------
本模块的目的不是复刻缠论，而是回答一个更基础的问题：
**按实时可见的数据画出来的笔，事后会不会被改写？**（重绘 / repaint）
如果会，那么任何以笔/中枢为信号的"历史回测"都在偷偷使用未来数据，
回测越漂亮越不可信——这与 7.5 节对过拟合的判定是同一条纪律。

规则里的每一处都是**显式选择**（缠论各流派对这些细节有分歧）：
1. 包含关系：与前一根合并K线比较，向上取高高、向下取低低；
2. 分型：合并后连续三根，中间最高 = 顶分型、中间最低 = 底分型；
3. 笔：顶底交替，两个分型中心至少相隔 `min_gap` 根合并K线（默认 4，即中间至少 3 根）；
4. **笔的延伸**：出现同向更极端的分型时，上一笔端点后移——这正是重绘的来源。

一条笔的终点在实时中要等**反向分型确认**才成立（天然滞后），
"后移"又会把已经成立的端点作废（重绘）。两者叠加，是缠论式结构难以回测的根本原因。
"""
from __future__ import annotations

import pandas as pd


def merge_bars(df: pd.DataFrame) -> pd.DataFrame:
    """处理K线包含关系，返回合并后的K线（high/low + 位置序号 pos）。"""
    rows: list[dict] = []
    direction = 0
    for r in df.itertuples():
        h, l = float(r.high), float(r.low)
        if not rows:
            rows.append({"date": r.Index, "high": h, "low": l})
            continue
        last = rows[-1]
        inside = (h <= last["high"] and l >= last["low"]) or (h >= last["high"] and l <= last["low"])
        if inside:
            if direction >= 0:
                last["high"] = max(last["high"], h)
                last["low"] = max(last["low"], l)
            else:
                last["high"] = min(last["high"], h)
                last["low"] = min(last["low"], l)
            continue
        direction = 1 if h > last["high"] else -1
        rows.append({"date": r.Index, "high": h, "low": l})
    out = pd.DataFrame(rows)
    out["pos"] = range(len(out))
    return out.set_index("date")


def fractals(merged: pd.DataFrame) -> list[dict]:
    """顶/底分型：三根合并K线中间那根。返回 [{date, pos, kind, price}]。"""
    out = []
    hi, lo = merged["high"].to_numpy(), merged["low"].to_numpy()
    pos = merged["pos"].to_numpy()
    dates = merged.index
    for i in range(1, len(merged) - 1):
        if hi[i] > hi[i - 1] and hi[i] > hi[i + 1]:
            out.append({"date": dates[i], "pos": int(pos[i]), "kind": "top", "price": float(hi[i])})
        elif lo[i] < lo[i - 1] and lo[i] < lo[i + 1]:
            out.append({"date": dates[i], "pos": int(pos[i]), "kind": "bottom", "price": float(lo[i])})
    return out


def pivots(fx: list[dict], min_gap: int = 4) -> list[dict]:
    """
    把分型过滤成交替的拐点（笔的端点）。

    * 同向分型：只有"更极端"才替换上一条拐点（端点后移 = 重绘）；
    * 反向分型：间隔不足 `min_gap` 根合并K线则忽略。
    """
    out: list[dict] = []
    for f in fx:
        if not out:
            out.append(dict(f))
            continue
        last = out[-1]
        if f["kind"] == last["kind"]:
            more_extreme = f["price"] > last["price"] if f["kind"] == "top" else f["price"] < last["price"]
            if more_extreme:
                out[-1] = dict(f)          # 端点后移
            continue
        if f["pos"] - last["pos"] < min_gap:
            continue
        out.append(dict(f))
    return out


def strokes(pv: list[dict]) -> list[dict]:
    """相邻拐点两两成笔。"""
    return [{"start": pv[i]["date"], "end": pv[i + 1]["date"],
             "dir": "up" if pv[i]["kind"] == "bottom" else "down"} for i in range(len(pv) - 1)]


def structure_asof(df: pd.DataFrame, asof, min_gap: int = 4) -> list[dict]:
    """只用 `asof` 及以前的数据（严格 prefix）计算笔。"""
    sub = df.loc[:pd.Timestamp(asof)]
    if len(sub) < 10:
        return []
    merged = merge_bars(sub)
    return strokes(pivots(fractals(merged), min_gap=min_gap))


def repaint_rate(df: pd.DataFrame, dates, min_gap: int = 4, tol_days: int = 3) -> dict:
    """
    重绘率：实时看到的笔端点，事后还在不在原处。

    对每个采样日 t：算 t 时刻的笔，去掉最后一笔（天然临时），
    取更早的端点，与**最终**（全样本）端点比对：`tol_days` 个自然日内有同向端点 → 稳定。
    """
    final = pivots(fractals(merge_bars(df)), min_gap=min_gap)
    final_dates = sorted(p["date"] for p in final)
    total = stable = 0
    moved: list[dict] = []
    for t in dates:
        st = structure_asof(df, t, min_gap)
        for s in st[:-1]:
            e = s["end"]
            if e >= pd.Timestamp(t):
                continue
            total += 1
            if any(abs((e - f).days) <= tol_days for f in final_dates):
                stable += 1
            else:
                moved.append({"asof": str(t)[:10], "endpoint": str(e)[:10], "dir": s["dir"]})
    return {"总端点数": total, "稳定点数": stable,
            "重绘率": (1 - stable / total) if total else float("nan"),
            "样例": moved[:8]}


def online_states(high: pd.Series, low: pd.Series, min_gap: int = 4) -> pd.Series:
    """
    **逐日在线**状态机：每根K线到达时，只用当时可见的信息判断"当前处于向上笔还是向下笔"。

    为什么必须有这一版
    ------------------
    `structure_asof` 是"事后用全样本笔端点 + 固定滞后"的近似。它的问题不在滞后，
    而在**当前笔的端点本身是临时的**：上涨途中会出现一个个"临时顶"，事后被更高的顶替换。
    用全样本端点回放，等于提前知道了顶在哪，会把每一次本该发生的"假死叉"抹掉——
    实测这能把年化从个位数抬到 60%+。这不是策略有效，是未来函数。

    本函数把事件按到达顺序处理：
    合并K线 → 新合并K线出现时检查中间那根是否成分型 → 分型进 pivots（同向更极端则替换端点）
    → 状态 = 最后一个拐点是底则"向上笔"，是顶则"向下笔"。
    """
    h = high.to_numpy(dtype=float)
    l = low.to_numpy(dtype=float)
    idx = high.index
    out = pd.Series(False, index=idx)
    merged: list[dict] = []
    direction = 0
    pv: list[dict] = []

    def consume_fractal(pos: int) -> None:
        """合并K线 pos 处若构成分型，就并入 pivots（规则同 pivots()）。"""
        if pos < 1 or pos >= len(merged) - 1 + 1:
            return
        if pos + 1 >= len(merged):
            return
        m0, m1, m2 = merged[pos - 1], merged[pos], merged[pos + 1]
        kind = None
        if m1["high"] > m0["high"] and m1["high"] > m2["high"]:
            kind, price = "top", m1["high"]
        elif m1["low"] < m0["low"] and m1["low"] < m2["low"]:
            kind, price = "bottom", m1["low"]
        if kind is None:
            return
        f = {"date": m1["date"], "pos": m1["pos"], "kind": kind, "price": price}
        if not pv:
            pv.append(f)
            return
        last = pv[-1]
        if f["kind"] == last["kind"]:
            more = f["price"] > last["price"] if f["kind"] == "top" else f["price"] < last["price"]
            if more:
                pv[-1] = f
            return
        if f["pos"] - last["pos"] < min_gap:
            return
        pv.append(f)

    for i in range(len(h)):
        if not merged:
            merged.append({"date": idx[i], "high": h[i], "low": l[i], "pos": 0})
        else:
            last = merged[-1]
            inside = (h[i] <= last["high"] and l[i] >= last["low"]) or (
                h[i] >= last["high"] and l[i] <= last["low"])
            if inside:
                if direction >= 0:
                    last["high"] = max(last["high"], h[i])
                    last["low"] = max(last["low"], l[i])
                else:
                    last["high"] = min(last["high"], h[i])
                    last["low"] = min(last["low"], l[i])
            else:
                direction = 1 if h[i] > last["high"] else -1
                merged.append({"date": idx[i], "high": h[i], "low": l[i], "pos": len(merged)})
                # 新合并K线出现 → 前一根的"三根结构"定型，检查它是否成分型
                consume_fractal(len(merged) - 2)
        out.iloc[i] = bool(pv and pv[-1]["kind"] == "bottom")
    return out
