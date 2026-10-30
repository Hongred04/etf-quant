"""
实盘持仓：存储、估值、与今日信号对比。

为什么单独成模块
----------------
持仓估值看起来只是「份额 × 现价」，但它同时被三种调用方使用：
Web 接口、单元测试、以及打包时的数据导出。写在 server.py 里就只能靠启动服务验证，
边界情况（缺行情、成本为 0、份额非整手）会一路飘到界面上。

这里只做纯计算，不读行情、不发请求：行情由调用方以 ``quotes`` 传入。
好处是同一份逻辑可以在测试里用假数据跑，不受网络与缓存影响。

口径
----
* 浮动盈亏 = 市值 − 成本额（不含已实现盈亏、不含手续费），这是券商 App 的常见口径
* 今日盈亏 = 份额 × (现价 − 昨收)，昨收缺失则不计入
* 仓位占比 = 市值 ÷ 总资产（总资产 = 持仓市值 + 现金）
* 调仓建议的目标权重直接取今日信号里的 weight（合计 100%），
  即「按策略满仓运行」；用户想留多少现金是另一个维度，不在这里替他决定
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from pathlib import Path

from .io_utils import atomic_write_text

CODE_RE = re.compile(r"^(sh|sz)\d{6}$")
LOT = 100  # A 股 ETF 一手 100 份

EMPTY = {"version": 1, "updated_at": None, "cash": 0.0, "items": []}


class HoldingError(ValueError):
    """持仓录入校验失败。消息直接面向用户，不要带堆栈信息。"""


# --------------------------------------------------------------------------- #
# 存储
# --------------------------------------------------------------------------- #
def load(path: str | Path) -> dict:
    """读取持仓文件。不存在或损坏时返回空结构，不让整个页面挂掉。"""
    fp = Path(path)
    if not fp.exists():
        return json.loads(json.dumps(EMPTY))
    try:
        raw = json.loads(fp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        # 损坏的文件不要静默覆盖，先备份再返回空结构
        try:
            fp.replace(fp.with_suffix(fp.suffix + ".broken"))
        except OSError:
            pass
        return json.loads(json.dumps(EMPTY))

    if not isinstance(raw, dict):
        return json.loads(json.dumps(EMPTY))
    out = json.loads(json.dumps(EMPTY))
    out["cash"] = _to_float(raw.get("cash"), 0.0)
    out["updated_at"] = raw.get("updated_at")
    items = raw.get("items")
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            code = str(it.get("code") or "").strip()
            if not CODE_RE.match(code):
                continue  # 单条坏了就丢这一条，不连累其余持仓
            out["items"].append({
                "code": code,
                "name": str(it.get("name") or code),
                "shares": _to_float(it.get("shares"), 0.0),
                "cost": _to_float(it.get("cost"), 0.0),
                "buy_date": str(it.get("buy_date") or ""),
                "note": str(it.get("note") or "")[:60],
            })
    return out


def save(path: str | Path, data: dict) -> dict:
    """原子写盘。返回写入后的结构（updated_at 已刷新）。"""
    payload = {
        "version": 1,
        "updated_at": datetime.now().replace(microsecond=0).isoformat(),
        "cash": round(_to_float(data.get("cash"), 0.0), 2),
        "items": list(data.get("items") or []),
    }
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


# --------------------------------------------------------------------------- #
# 录入校验
# --------------------------------------------------------------------------- #
def _to_float(v, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def validate_item(raw: dict, known: dict[str, str] | None = None) -> dict:
    """校验并规范化一条持仓。``known`` 是 code → name，用于补名称。"""
    if not isinstance(raw, dict):
        raise HoldingError("提交的数据格式不对")
    code = str(raw.get("code") or "").strip().lower()
    if not CODE_RE.match(code):
        raise HoldingError("代码格式应为 sh510300 / sz159915 这样的 6 位数字")

    shares = _to_float(raw.get("shares"))
    cost = _to_float(raw.get("cost"))
    if shares <= 0:
        raise HoldingError("份额必须大于 0")
    if shares > 1e9:
        raise HoldingError("份额数值异常（超过 10 亿份），请检查是否多输入了 0")
    if cost <= 0:
        raise HoldingError("成本价必须大于 0")
    if cost > 1e5:
        raise HoldingError("成本价异常（超过 10 万元/份），请检查小数点")

    buy_date = str(raw.get("buy_date") or "").strip()
    if buy_date:
        try:
            date.fromisoformat(buy_date)
        except ValueError as exc:
            raise HoldingError("买入日期格式应为 YYYY-MM-DD") from exc
        if date.fromisoformat(buy_date) > date.today():
            raise HoldingError("买入日期不能晚于今天")

    name = str(raw.get("name") or "").strip()
    if not name and known:
        name = known.get(code, "")

    return {
        "code": code,
        "name": name or code,
        "shares": round(shares, 2),
        "cost": round(cost, 4),
        "buy_date": buy_date,
        "note": str(raw.get("note") or "").strip()[:60],
    }


def upsert(data: dict, item: dict) -> dict:
    """新增或覆盖同一代码的持仓（同一代码只保留一条，成本按新值整体覆盖）。"""
    items = [i for i in (data.get("items") or []) if i["code"] != item["code"]]
    items.append(item)
    items.sort(key=lambda i: (i["code"],))
    data = dict(data)
    data["items"] = items
    return data


def remove(data: dict, code: str) -> dict:
    code = str(code or "").strip().lower()
    data = dict(data)
    data["items"] = [i for i in (data.get("items") or []) if i["code"] != code]
    return data


def build_from_weights(rows: list[dict], amount: float) -> tuple[list[dict], float]:
    """
    按今日信号的权重，把一笔资金摊成整手持仓（「一键建仓」）。

    返回 (items, leftover_cash)。手数向下取整，剩余资金留在现金里，
    不做「差一点就凑一手」的向上取整——那会让总投入超出用户填的金额。
    """
    amount = _to_float(amount)
    if amount <= 0:
        raise HoldingError("投入金额必须大于 0")

    items: list[dict] = []
    spent = 0.0
    for r in rows or []:
        w = _to_float(r.get("weight"))
        px = _to_float(r.get("price"))
        if w <= 0 or px <= 0:
            continue
        budget = amount * w
        lots = int(budget // (px * LOT))
        if lots <= 0:
            continue
        shares = lots * LOT
        cost_value = shares * px
        spent += cost_value
        items.append({
            "code": r["code"],
            "name": r.get("name") or r["code"],
            "shares": shares,
            "cost": round(px, 4),
            "buy_date": date.today().isoformat(),
            "note": "按信号建仓",
        })
    return items, round(max(0.0, amount - spent), 2)


# --------------------------------------------------------------------------- #
# 估值
# --------------------------------------------------------------------------- #
def value(data: dict, quotes: dict[str, dict]) -> dict:
    """
    估值。``quotes`` 形如 {code: {"price": 1.23, "prev": 1.20, "date": "2026-10-23",
    "name": "...", "live": True}}；缺行情的标的单独列出，不计入合计。
    """
    items = list(data.get("items") or [])
    cash = _to_float(data.get("cash"))
    today = date.today().isoformat()

    rows: list[dict] = []
    missing: list[str] = []
    total_mv = 0.0
    total_cost = 0.0
    day_pnl = 0.0

    for it in items:
        q = quotes.get(it["code"]) or {}
        px = _to_float(q.get("price"))
        prev = _to_float(q.get("prev"))
        shares = _to_float(it.get("shares"))
        cost = _to_float(it.get("cost"))
        cost_value = shares * cost
        # 今天买的仓，当日盈亏要用「现价 − 成本价」；用昨收会把买入前那段行情
        # 也算成自己的盈亏（收盘价建仓时凭空多出一笔当日亏损/盈利）。
        bought_today = str(it.get("buy_date") or "") >= today

        if px <= 0:
            missing.append(it.get("name") or it["code"])
            rows.append({
                **it, "price": None, "price_date": q.get("date"), "live": False,
                "market_value": None, "cost_value": round(cost_value, 2),
                "pnl": None, "pnl_pct": None, "day_pnl": None, "day_basis": None,
                "weight": None, "pos_weight": None, "lots": _lots(shares),
            })
            total_cost += cost_value
            continue

        mv = shares * px
        pnl = mv - cost_value
        total_mv += mv
        total_cost += cost_value

        row_day, basis = None, None
        if bought_today:
            row_day, basis = shares * (px - cost), "cost"
        elif prev > 0:
            row_day, basis = shares * (px - prev), "prev"
        if row_day is not None:
            day_pnl += row_day

        rows.append({
            **it,
            "price": round(px, 4),
            "price_date": q.get("date"),
            "live": bool(q.get("live")),
            "market_value": round(mv, 2),
            "cost_value": round(cost_value, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": (pnl / cost_value) if cost_value > 0 else None,
            "day_pnl": round(row_day, 2) if row_day is not None else None,
            "day_basis": basis,
            "lots": _lots(shares),
            "weight": None,          # 下面按合计回填
            "pos_weight": None,
        })

    total_assets = total_mv + cash
    prev_assets = total_assets - day_pnl

    for r in rows:
        if r.get("market_value") is not None:
            r["weight"] = r["market_value"] / total_mv if total_mv > 0 else None
            r["pos_weight"] = r["market_value"] / total_assets if total_assets > 0 else None

    rows.sort(key=lambda r: (r.get("market_value") is None, -(r.get("market_value") or 0)))

    return {
        "items": rows,
        "cash": round(cash, 2),
        "total_market_value": round(total_mv, 2),
        "total_cost": round(total_cost, 2),
        "total_pnl": round(total_mv - total_cost, 2),
        "total_pnl_pct": ((total_mv - total_cost) / total_cost) if total_cost > 0 else None,
        "day_pnl": round(day_pnl, 2),
        "day_pnl_pct": (day_pnl / prev_assets) if prev_assets > 0 else None,
        "total_assets": round(total_assets, 2),
        "position_ratio": (total_mv / total_assets) if total_assets > 0 else 0.0,
        "missing_quotes": missing,
        "count": len(items),
    }


def _lots(shares: float) -> float:
    return round(shares / LOT, 2)


# --------------------------------------------------------------------------- #
# 与今日信号对比
# --------------------------------------------------------------------------- #
def advise(valued: dict, signal_rows: list[dict], threshold: float = 0.02) -> dict:
    """
    把实际仓位与今日信号的目标仓位逐只对比，给出加/减/清仓提示。

    差额小于 ``threshold``（默认 2 个百分点）的动作忽略——ETF 场内最小变动
    就是一手的钱，2% 以下反复买卖只会给券商交手续费。
    """
    target = {}
    for r in signal_rows or []:
        w = _to_float(r.get("weight"))
        if w > 0:
            target[r["code"]] = {"weight": w, "name": r.get("name") or r["code"], "price": _to_float(r.get("price"))}

    total_assets = _to_float(valued.get("total_assets"))
    held = {r["code"]: r for r in valued.get("items") or []}

    plan: list[dict] = []
    for code, t in target.items():
        h = held.get(code)
        actual_w = _to_float(h.get("pos_weight")) if h else 0.0
        diff = t["weight"] - actual_w
        px = _to_float(h.get("price")) if h else t["price"]
        delta_value = diff * total_assets
        lots = int(abs(delta_value) // (px * LOT)) if px > 0 else 0
        if abs(diff) < threshold or lots == 0:
            action = "hold" if h else "skip"
        else:
            action = "add" if diff > 0 else "cut"
        plan.append({
            "code": code,
            "name": (h or {}).get("name") or t["name"],
            "target_weight": t["weight"],
            "actual_weight": actual_w,
            "diff": diff,
            "delta_value": round(delta_value, 2),
            "lots": lots,
            "price": px if px > 0 else None,
            "action": action,
            "held_shares": (h or {}).get("shares"),
        })

    for code, h in held.items():
        if code in target:
            continue
        plan.append({
            "code": code,
            "name": h.get("name") or code,
            "target_weight": 0.0,
            "actual_weight": _to_float(h.get("pos_weight")),
            "diff": -_to_float(h.get("pos_weight")),
            "delta_value": -_to_float(h.get("market_value")),
            "lots": _lots(_to_float(h.get("shares"))),
            "price": h.get("price"),
            "action": "exit",
            "held_shares": h.get("shares"),
        })

    order = {"exit": 0, "cut": 1, "add": 2, "hold": 3, "skip": 4}
    plan.sort(key=lambda p: (order.get(p["action"], 9), -abs(p["diff"])))

    acts = [p for p in plan if p["action"] in ("add", "cut", "exit")]
    buy_value = sum(p["delta_value"] for p in acts if p["delta_value"] > 0)
    sell_value = -sum(p["delta_value"] for p in acts if p["delta_value"] < 0)

    return {
        "plan": plan,
        "needs_action": len(acts),
        "buy_value": round(buy_value, 2),
        "sell_value": round(sell_value, 2),
    }
