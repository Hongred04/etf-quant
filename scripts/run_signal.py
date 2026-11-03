"""
生成最新交易日的实盘选股信号（TGAR 策略）。

用法
----
python scripts/run_signal.py
python scripts/run_signal.py --strategy s1_baseline --date 2026-10-23
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from etfquant.io_utils import atomic_to_csv
from etfquant.pipeline import load_panel
from etfquant.strategy import STRATEGY_REGISTRY, TrendGatedAdaptive
from etfquant.rebalance import last_rebalance, schedule, trading_days_between
from etfquant.universe import POOL_MODES, apply_pool

PANEL_START = "2018-01-02"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="s4_tgar")
    ap.add_argument("--date", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-archive", action="store_true", help="只出信号，不写归档")
    ap.add_argument("--pool", default="full", choices=list(POOL_MODES),
                    help="池子口径：full=全部标的（默认）；core=剔除纯大盘宽基（实验，实测无改善）")
    args = ap.parse_args()

    panel = load_panel(
        ROOT / "data" / "etf_pool.csv", ROOT / "data" / "cache", PANEL_START, "2026-12-31"
    )
    panel = apply_pool(panel, args.pool)
    index = panel.close.index

    # 「信号日」= 数据截止日；「调仓日」= 它之前（含当日）最近一个调仓日。
    # 两者必须分开：策略每 5 个交易日才换一次仓，但行情每天都有。
    # 若拿数据日当信号日重算，连续三天运行会得到三份不同的持仓，照做等于每天换仓。
    data_date = pd.Timestamp(args.date) if args.date else index[-1]
    if data_date not in index:
        # get_indexer 越界返回 -1，而 index[-1] 取的是"最后一个元素"——
        # 传入早于数据起点的日期会静默给出最新交易日的信号。必须显式拦住。
        pos = index.get_indexer([data_date], method="ffill")[0]
        if pos < 0:
            raise SystemExit(
                f"[ERR] 信号日期 {data_date.date()} 早于数据起点 "
                f"{index[0].date()}，请改用更晚的日期"
            )
        data_date = index[pos]

    strat = STRATEGY_REGISTRY[args.strategy](panel)
    targets = strat.generate()  # 回溯历史持仓，填充缓冲带状态

    sig_date = last_rebalance(strat.rebalance_days(), data_date)
    if sig_date is None:
        raise SystemExit(
            f"[ERR] 数据只到 {data_date.date()}，还不够 {strat.warmup} 根 K 线的预热期，"
            f"尚无任何调仓日"
        )
    sched = schedule(index, sig_date, strat.rebalance_freq)

    # 权重直接取 generate() 的结果：那是回测同一份目标，避免再调一次 target_weights
    # 时用上已被改写的缓冲带状态（self.holdings 此刻指向最后一期持仓）。
    target = targets.get(sig_date)
    if not target:
        elig = strat.eligible_at(sig_date)
        target = strat.target_weights(sig_date, elig)
    srt = pd.Series(target).sort_values(ascending=False)

    meta = panel.meta
    rows = []
    for tk, wt in srt.items():
        pdate = panel.last_date.get(tk) if panel.last_date is not None else None
        pdate = pd.Timestamp(pdate) if pdate is not None and pd.notna(pdate) else None
        rows.append(
            {
                "代码": tk,
                "名称": meta.loc[tk, "name"] if tk in meta.index else "",
                "类别": meta.loc[tk, "category"] if tk in meta.index else "",
                "目标权重": round(float(wt), 4),
                # 参考价取**数据截止日**收盘：下单时要付的是这个价，
                # 而调仓日收盘价只是决策依据。两者相差几天时按前者算手数才不会买错。
                "最新价": round(float(panel.close.loc[data_date, tk]), 3),
                "R20%": round(float(strat.fb.r20.loc[sig_date, tk]) * 100, 2)
                if pd.notna(strat.fb.r20.loc[sig_date, tk]) else np.nan,
                "R60%": round(float(strat.fb.r60.loc[sig_date, tk]) * 100, 2)
                if pd.notna(strat.fb.r60.loc[sig_date, tk]) else np.nan,
                "年化波动%": round(float(strat.fb.vol60.loc[sig_date, tk]) * 100, 2)
                if pd.notna(strat.fb.vol60.loc[sig_date, tk]) else np.nan,
                "MA60之上": bool(strat.fb.above60.loc[sig_date, tk]),
                "价格日期": data_date.date().isoformat(),
                "价格陈旧": bool(pdate is not None and pdate < data_date),
            }
        )
    out = pd.DataFrame(rows)

    # 空仓/防守提示
    equity_cats = {"宽基", "行业", "主题", "跨境", "商品", "转债"}
    eq_w = out[out["类别"].isin(equity_cats)]["目标权重"].sum() if not out.empty else 0.0
    bond_w = out[out["类别"].isin(["债券"])]["目标权重"].sum() if not out.empty else 0.0

    gap = trading_days_between(index, data_date, sched["next_rebalance"])
    held = trading_days_between(index, sched["exec_date"], data_date)
    print("=" * 78)
    print(f"调仓日: {sig_date.date()}    数据截止: {data_date.date()}    策略: {strat.name}")
    print(f"执行日: {sched['exec_date'].date()} 开盘"
          f"（{'已执行，距今 ' + str(held) + ' 个交易日' if held > 0 else '待执行'}）"
          f"  |  下次调仓: {sched['next_rebalance'].date()}"
          f"{'（估算）' if sched['next_estimated'] else ''}"
          f"，还有 {gap} 个交易日")
    if isinstance(strat, TrendGatedAdaptive):
        print(f"市场状态 regime = {strat.regime_at(sig_date):.1f} | "
              f"权益目标敞口 = {strat._exposure(strat.regime_at(sig_date)):.0%}"
              f"{'（触发回撤熔断，敞口减半）' if strat._drawdown_triggered(sig_date) else ''}")
    print(f"风险资产合计 {eq_w:.1%} | 债券类合计 {bond_w:.1%} | 现金 {max(0, 1 - eq_w - bond_w):.1%}")
    print("=" * 78)
    if out.empty:
        print("当日无合格标的，建议全部持有现金。")
    else:
        print(out.to_string(index=False))

    non_cn = out[out["类别"].isin(["跨境", "商品"])]["目标权重"].sum() if not out.empty else 0.0
    if non_cn > 0:
        print(f"\n[提示] 其中跨境/商品类占 {non_cn:.1%}。当前闸门以沪深300 判定市场状态，"
              f"该类标的的仓位亦受 A 股状态约束；若需按资产类别独立判定，请调整 regime 逻辑。")

    fp = Path(args.out) if args.out else ROOT / "output" / f"signal_{sig_date.date()}.csv"
    atomic_to_csv(out, fp, index=False, encoding="utf-8-sig")
    print(f"\n[OK] 信号已写入 {fp}")

    # 供 Web 控制台读取的状态摘要
    meta = {
        "date": str(sig_date.date()),
        # 调仓日历：界面靠这几个字段说清"哪天定的、哪天买、下次哪天换"，
        # 不再让前端自己用"数据日 +1"猜执行日（那会把非调仓日也当成执行日）。
        "rebalance_date": str(sig_date.date()),
        "data_date": str(data_date.date()),
        "exec_date": str(sched["exec_date"].date()),
        "next_rebalance": str(sched["next_rebalance"].date()),
        "next_estimated": bool(sched["next_estimated"]),
        "rebalance_freq": int(strat.rebalance_freq),
        "days_to_next": int(gap),
        "held_days": int(held),
        "strategy": strat.name,
        "regime": float(strat.regime_at(sig_date)) if isinstance(strat, TrendGatedAdaptive) else None,
        "exposure": float(strat._exposure(strat.regime_at(sig_date)))
        if isinstance(strat, TrendGatedAdaptive) else None,
        "equity_weight": round(float(eq_w), 4),
        "bond_weight": round(float(bond_w), 4),
        "n_holdings": int(len(out)),
        "note": (
            "跨境/商品类标的的仓位同样受 A 股状态闸门约束；实盘建议按资产类别独立判定市场状态。"
            if non_cn > 0 else ""
        ),
        "stale_price_count": int(out["价格陈旧"].sum()) if "价格陈旧" in out else 0,
        "stale_price_names": list(out.loc[out["价格陈旧"], "名称"]) if "价格陈旧" in out else [],
        "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    (ROOT / "output" / "signal_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[OK] 状态摘要已写入 output/signal_meta.json")

    # 归档：signal_YYYY-MM-DD.csv 会被下次重算覆盖，复盘要的是"当时给出什么"。
    # 同一信号日整体替换，重复生成不会叠加。
    if not args.no_archive and not out.empty:
        try:
            from etfquant.track import archive_path, archive_rows, upsert_signal
            # signal_date 记调仓日：同一调仓窗口内反复运行会整体替换同一条，
            # 归档因此天然是"每期一条"，不会再出现"每天一份新清单"。
            # 参考价必须取调仓日收盘（不是"最新价"），否则补跑时会写下未来价格。
            rows = archive_rows(out, panel.close, sig_date)
            for row, (_, r) in zip(rows, out.iterrows()):
                row["exec_date"] = str(sched["exec_date"].date())
            n = upsert_signal(archive_path(ROOT / "data"), rows, source="live")
            print(f"[OK] 已归档 {n} 条信号到 data/signal_archive.csv（source=live）")
        except Exception as exc:  # noqa: BLE001 —— 归档失败不该让出信号这一步失败
            print(f"[WARN] 信号归档失败：{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
