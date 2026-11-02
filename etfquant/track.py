"""
实盘信号归档与「如果照做」的纸上净值。

为什么要单独归档
----------------
`output/signal_YYYY-MM-DD.csv` 是"当前这份清单"，每次重算都会覆盖同名文件。
它回答不了"上周给出的建议到底是什么"，而复盘需要的正是那个**当时**的快照：
数据修订、参数调整之后，回测净值会跟着变，归档不会。

两种记录必须分开
----------------
* ``source="live"``     —— 当日真实输出的信号，跑过一次就固定下来；
* ``source="backfill"`` —— 事后用当前策略回放历史得出的信号，只用于给页面垫底，
  它本质上仍是样本内重算，不能当成"当时的建议"。

页面上两者的颜色/标签要分开，否则复盘会自欺。

口径
----
纸上净值直接复用 ``Backtester``：信号日收盘生成、次一交易日开盘成交、
双边成本 0.10%（fee 0.0003 + slippage 0.0007），与主回测完全一致，
避免"复盘用另一套记账，结果对不上"。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from .backtest import Backtester
from .io_utils import atomic_to_csv
from .metrics import cagr, max_drawdown

COLUMNS = [
    "signal_date", "exec_date", "code", "name", "category",
    "weight", "ref_price", "source", "created_at",
]


def archive_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "signal_archive.csv"


def load_archive(fp: str | Path) -> pd.DataFrame:
    fp = Path(fp)
    if not fp.exists():
        return pd.DataFrame(columns=COLUMNS)
    try:
        df = pd.read_csv(fp, dtype={"code": str, "signal_date": str, "exec_date": str})
    except (pd.errors.ParserError, OSError, UnicodeDecodeError):
        return pd.DataFrame(columns=COLUMNS)
    for c in COLUMNS:
        if c not in df.columns:
            df[c] = None
    return df[COLUMNS]


def upsert_signal(fp: str | Path, rows: list[dict], source: str = "live") -> int:
    """
    写入一期信号。**同一 signal_date 整体替换**，重复生成不会叠加出两份。

    live 记录不会被 backfill 覆盖：后者只在当日没有 live 时才写。
    """
    fp = Path(fp)
    if not rows:
        return 0
    sig_date = str(rows[0]["signal_date"])
    df = load_archive(fp)
    if not df.empty:
        same = df["signal_date"].astype(str) == sig_date
        if source == "backfill" and (df.loc[same, "source"] == "live").any():
            return 0
        df = df[~same]
    created = datetime.now().replace(microsecond=0).isoformat()
    new = pd.DataFrame([{**r, "source": source, "created_at": created} for r in rows])
    out = pd.concat([df, new[COLUMNS]], ignore_index=True)
    out = out.sort_values(["signal_date", "weight"], ascending=[True, False])
    atomic_to_csv(out, fp, index=False, encoding="utf-8-sig")
    return len(new)


def _same_weights(a: dict[str, float], b: dict[str, float], tol: float = 1e-6) -> bool:
    if set(a) != set(b):
        return False
    return all(abs(a[k] - b[k]) <= tol for k in a)


def archive_targets(df: pd.DataFrame, collapse_unchanged: bool = True) -> dict[pd.Timestamp, dict[str, float]]:
    """
    归档 → {信号日: {代码: 权重}}，可直接喂给 Backtester。

    ``collapse_unchanged`` 会合并"目标权重没变"的连续记录。这不是优化，是正确性问题：
    用户每天点一次「生成选股信号」，归档就会每天多一条；若原样喂给回测器，
    等于**每天把组合拉回目标权重**，产生策略本身不存在的再平衡成本
    （实测把纸上净值压低约 0.4 个百分点）。信号没变就不该有交易。
    """
    if df.empty:
        return {}
    out: dict[pd.Timestamp, dict[str, float]] = {}
    prev: dict[str, float] | None = None
    for d, g in sorted(df.groupby("signal_date"), key=lambda kv: str(kv[0])):
        w = {str(r.code): float(r.weight) for r in g.itertuples() if float(r.weight) > 1e-6}
        if not w:
            continue
        if collapse_unchanged and prev is not None and _same_weights(prev, w):
            continue
        out[pd.Timestamp(str(d))] = w
        prev = w
    return out


def period_spans(index: pd.DatetimeIndex, signal_dates: list[pd.Timestamp]) -> list[tuple]:
    """
    把每期信号映射成 `(买入日, 期末日)`；买入日还没到（数据没覆盖）时给 `(None, None)`。

    * 买入日 = 信号日之后第一个交易日（T 日收盘定、T+1 开盘买）
    * 期末日 = **下一期**买入日；最后一期没有"下一期"，取数据最后一天

    这个函数单独抽出来，是因为最后一期的终点曾经写成"整个索引"，
    于是 `b[0]` 变成数据起点 2018-01-02，最后一期的期间收益算成 −8.8% 的假数字。
    """
    out: list[tuple] = []
    for i, d in enumerate(signal_dates):
        nxt = signal_dates[i + 1] if i + 1 < len(signal_dates) else None
        a = index[index > d]
        if len(a) == 0:
            out.append((None, None))
            continue
        b = index[index > nxt] if nxt is not None else index[-1:]
        out.append((a[0], b[0] if len(b) else index[-1]))
    return out


def signal_fidelity(
    archived: pd.DataFrame, strategy_targets: dict, tol: float = 1e-4
) -> list[dict]:
    """逐期比对「live 归档信号」与「回测在同一天给出的目标权重」。

    为什么这是前向验证的前提
    ------------------------
    纸上净值只有在"记录的信号 = 策略当日真实输出"时才是在检验策略。
    如果两者不一致（手工改过、用了旧参数、归档时价格口径不同），
    后面算出来的偏差就不能归因于市场——它测的是另一套东西。

    只比 live 期：backfill 是用**当前**策略回放的，与回测按构造一致，
    拿它来证明"信号保真"是循环论证。
    """
    if archived is None or archived.empty or "source" not in archived.columns:
        return []
    live = archived[archived["source"] == "live"]
    if live.empty:
        return []
    out: list[dict] = []
    for d, grp in live.groupby("signal_date"):
        ts = pd.Timestamp(str(d))
        w_live = {str(c): float(w) for c, w in zip(grp["code"], grp["weight"])}
        w_bt = {str(k): float(v) for k, v in (strategy_targets.get(ts) or {}).items()}
        codes = set(w_live) | set(w_bt)
        l1 = sum(abs(w_live.get(c, 0.0) - w_bt.get(c, 0.0)) for c in codes) / 2.0
        max_diff = max((abs(w_live.get(c, 0.0) - w_bt.get(c, 0.0)) for c in codes), default=0.0)
        out.append({
            "signal_date": ts.strftime("%Y-%m-%d"),
            "n_live": len(w_live),
            "n_backtest": len(w_bt),
            "overlap": len(set(w_live) & set(w_bt)),
            "l1": round(float(l1), 6),
            "max_diff": round(float(max_diff), 6),
            "match": bool(set(w_live) == set(w_bt) and max_diff <= tol),
        })
    return out


def build_track(panel, strategy_targets: dict, archived: pd.DataFrame) -> dict:
    """
    计算纸上净值，并与"同期回测"对齐比较。

    对齐口径：两条曲线都从**首期信号的执行日**开始，到数据末日为止。
    回测用同一套 Backtester 与成本，差异只来自"信号是否被冻结"。
    """
    tg = archive_targets(archived)
    if not tg:
        return {"count": 0, "periods": [], "series": []}

    dates = sorted(tg)
    # 执行日 = 信号日之后的第一个交易日；Backtester 自己按 T+1 开盘处理，
    # 这里只需要把起点放在信号日当天，净值从次日开始。
    start = str(dates[0].date())
    end = str(panel.close.index[-1].date())

    bt = Backtester(panel.close, panel.open_)
    paper = bt.run(tg, start=start, end=end)
    ref = bt.run(strategy_targets, start=start, end=end)

    p_nav, r_nav = paper.nav, ref.nav
    idx = p_nav.index.intersection(r_nav.index)
    p_nav, r_nav = p_nav.reindex(idx), r_nav.reindex(idx)

    series = [
        {"date": d.strftime("%Y-%m-%d"), "paper": round(float(p), 6), "backtest": round(float(r), 6)}
        for d, p, r in zip(idx, p_nav.values, r_nav.values)
    ]

    # 每期：该期信号 + 持有到下一次调仓的收益
    periods = []
    for d, (e0, e1) in zip(dates, period_spans(idx, dates)):
        if e0 is None:
            # 最新一期：执行日还没开盘（或数据没到），保留占位，别让它从列表里消失
            rows = archived[archived["signal_date"].astype(str) == str(d.date())]
            periods.append({
                "signal_date": str(d.date()), "exec_date": "", "ret": None,
                "cum": round(float(p_nav.iloc[-1] / float(p_nav.iloc[0]) - 1), 6),
                "source": str(rows["source"].iloc[0]) if not rows.empty else "live",
                "pending": True,
                "holdings": [
                    {"code": str(r.code), "name": str(r.name), "weight": round(float(r.weight), 4),
                     "price": round(float(r.ref_price), 4) if pd.notna(r.ref_price) else None}
                    for r in rows.itertuples()
                ],
            })
            continue
        rows = archived[archived["signal_date"].astype(str) == str(d.date())]
        ret = float(p_nav.loc[e1] / p_nav.loc[e0] - 1) if e0 in p_nav.index and e1 in p_nav.index else None
        periods.append({
            "signal_date": str(d.date()),
            "exec_date": e0.strftime("%Y-%m-%d"),
            "ret": round(ret, 6) if ret is not None else None,
            # cum 用**期末**净值：这样"最后一期的累计"就等于纸上总收益，
            # 用执行日净值会让人看到"最新一期累计 +9.6%、总收益 11.0%"两个对不上的数。
            "_cum_end": e1,
            "source": str(rows["source"].iloc[0]) if not rows.empty else "live",
            "holdings": [
                {"code": str(r.code), "name": str(r.name), "weight": round(float(r.weight), 4),
                 "price": round(float(r.ref_price), 4) if pd.notna(r.ref_price) else None}
                for r in rows.itertuples()
            ],
        })

    base = float(p_nav.iloc[0])
    for p in periods:
        if p.get("pending"):
            continue          # 占位期在构造时已算好 cum，别在这里被覆盖成 None
        t = p.pop("_cum_end", None)
        t = pd.Timestamp(t) if t is not None else None
        p["cum"] = (round(float(p_nav.loc[t] / base - 1), 6)
                    if t is not None and t in p_nav.index and base > 0 else None)

    # 前向验证：live 期从首期执行日到数据末日的偏差，与回填期分开算。
    # 全样本的 paper-vs-backtest 在回填段按构造恒等，把它算进"偏差"会稀释问题。
    fidelity = signal_fidelity(archived, strategy_targets)
    live_all = [p for p in periods if p.get("source") == "live"]
    live_exec = [p["exec_date"] for p in live_all if p.get("exec_date")]
    fwd = {
        "live_periods": len(live_all),        # 已归档的 live 期数（含待执行）
        "live_executed": len(live_exec),      # 已产生实际持有收益的期数
        "live_days": 0,
        "divergence": None,
        "verdict": "还没有 live 记录",
        "need": 8,
    }
    if live_exec:
        start = min(live_exec)
        sub = [s for s in series if s["date"] >= start]
        if len(sub) >= 2:
            dp = sub[-1]["paper"] / sub[0]["paper"] - 1
            db = sub[-1]["backtest"] / sub[0]["backtest"] - 1
            fwd["live_days"] = len(sub)
            fwd["divergence"] = round(float(dp - db), 6)
            # 保真度是前提：信号不一致时，"样本不足"这种软结论会掩盖真正的问题
            if any(not f["match"] for f in fidelity):
                fwd["verdict"] = "信号与回测不一致，先查归档"
            elif fwd["live_executed"] < fwd["need"]:
                fwd["verdict"] = "样本不足，只看方向不做结论"
            elif abs(fwd["divergence"]) <= 0.01:
                fwd["verdict"] = "与回测一致"
            else:
                fwd["verdict"] = "偏差偏大，需要排查"

    return {
        "count": len(dates),
        "first": str(dates[0].date()),
        "last": str(dates[-1].date()),
        "paper": {
            "total": round(float(p_nav.iloc[-1] / p_nav.iloc[0] - 1), 6),
            "cagr": round(cagr(p_nav), 6),
            "mdd": round(max_drawdown(p_nav), 6),
        },
        "backtest": {
            "total": round(float(r_nav.iloc[-1] / r_nav.iloc[0] - 1), 6),
            "cagr": round(cagr(r_nav), 6),
            "mdd": round(max_drawdown(r_nav), 6),
        },
        "series": series,
        "periods": periods,
        "fidelity": fidelity,
        "forward": fwd,
        "live_count": int(archived.groupby("signal_date")["source"].first().eq("live").sum()),
        "backfill_count": int(archived.groupby("signal_date")["source"].first().eq("backfill").sum()),
    }
