"""
生成前向验证月报（Markdown）。

用法
----
    python scripts/monthly_report.py                # 上月
    python scripts/monthly_report.py --month 2026-10
    python scripts/monthly_report.py --month 2026-10 --json   # 只打印结构化结果

产物：`output/reports/monthly_<YYYY-MM>.md`

为什么不用网页截图
------------------
月报要能存下来做对比（"上个月偏差 0.00pp，这个月 −0.12pp"），也要能被 Agent
直接读出来讲给用户听。Markdown 是最小的公约数：人能看、diff 得动、
贴进任何文档都不掉格式。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from etfquant import alpha_track as AT  # noqa: E402
from etfquant import diagnostics as DG  # noqa: E402
from etfquant import monthly  # noqa: E402
from etfquant import rebalance as RB  # noqa: E402
from etfquant import track as T  # noqa: E402
from etfquant.expectation import load as load_expectation  # noqa: E402
from etfquant.io_utils import atomic_write_text  # noqa: E402
from etfquant.pipeline import load_panel  # noqa: E402
from etfquant.strategy import TrendGatedAdaptive  # noqa: E402

DATA = ROOT / "data"
OUT = ROOT / "output"
SELF = "S4-TGAR(自研)"


def _perf():
    fp = OUT / "perf_main.csv"
    if not fp.exists():
        return None
    try:
        df = pd.read_csv(fp, index_col=0)
    except (OSError, pd.errors.ParserError):
        return None
    hit = df[df["策略"] == SELF]
    return None if hit.empty else hit.iloc[0]


def _alpha_block(panel, month: str, live_start: str | None) -> dict | None:
    """
    从 `output/diagnostics.json` 取冻结的 β 与 α，算这个月的不可解释残差。

    缺文件、缺字段一律返回 None（整段不显示），不让月报因为"少了一张表"而失败。
    """
    fp = OUT / "diagnostics.json"
    if not fp.exists():
        return None
    try:
        doc = json.loads(fp.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        return None
    reg = doc.get("regression") or {}
    frozen_at = (doc.get("meta") or {}).get("frozen_at")
    if reg.get("alpha_ann") is None:
        return None
    betas = {k: reg.get(f"beta_{k}") for k in ("mkt", "mom", "smb")}
    if any(v is None for v in betas.values()):
        return None

    nav_fp = OUT / "nav_main.csv"
    if not nav_fp.exists():
        return None
    nav = pd.read_csv(nav_fp, index_col=0, parse_dates=True)[SELF]

    ret = panel.close.pct_change(fill_method=None)
    try:
        factors = pd.DataFrame({
            "mkt": ret["sh510300"],
            "mom": DG.momentum_factor(panel.close, lookback=20, skip=5, q=0.10, min_names=20),
            "smb": ret["sh512100"] - ret["sh510300"],
        }).dropna()
    except KeyError:
        return None
    block = AT.build_block(nav, factors, float(reg["alpha_ann"]), betas, month,
                           live_start=live_start)
    if block is None:
        return None
    block["frozen_at"] = frozen_at

    # 残差同时包含"选股"与"闸门择时"，但月报不拆（拆开需要条件 β 模型，见 README 7.10）。
    # 给它两个能立刻读懂的上下文：这个月的权益敞口、以及大盘涨跌。
    wfp = OUT / "weights_s4_tgar_main.csv"
    if wfp.exists():
        try:
            w = pd.read_csv(wfp, index_col=0, parse_dates=True)
            cat = panel.meta["category"] if "category" in panel.meta else pd.Series(dtype=object)
            expo = DG.equity_exposure(w, cat)
            idx = expo.index[expo.index.strftime("%Y-%m") == month]
            if len(idx):
                block["expo_month"] = float(expo.loc[idx].mean())
                block["expo_avg"] = float(expo.mean())
        except (OSError, KeyError, ValueError):
            pass
    mfp = nav.index[nav.index.strftime("%Y-%m") == month]
    if len(mfp):
        block["mkt_month"] = float((1 + factors["mkt"].reindex(mfp)).prod() - 1)
    return block


def build_month_data(month: str, today=None) -> dict:
    """把所有数字算出来装进一个 dict，渲染交给 `etfquant.monthly.render_markdown`。"""
    arch = T.load_archive(T.archive_path(DATA))
    if arch.empty:
        raise SystemExit("还没有归档信号（data/signal_archive.csv 为空）。先跑一次 run_signal.py。")

    panel = load_panel(DATA / "etf_pool.csv", DATA / "cache", "2018-01-02", "2026-12-31")
    strat = TrendGatedAdaptive(panel)
    res = T.build_track(panel, strat.generate(), arch)

    series = res.get("series") or []
    pts = monthly.slice_series(series, month)
    periods = [p for p in res.get("periods", []) if str(p.get("signal_date", ""))[:7] == month]

    index = panel.close.index
    data_date = index[-1]
    freq = int(getattr(strat, "rebalance_freq", 5) or 5)
    last_sig = pd.Timestamp(res["last"]) if res.get("last") else data_date
    nxt, estimated = RB.next_rebalance(index, last_sig, freq)
    days_to_next = RB.trading_days_between(index, data_date, nxt)
    if estimated:
        days_to_next = max(0, days_to_next)

    live_rows = arch[arch["source"] == "live"] if "source" in arch else arch.iloc[0:0]
    live_start = (str(live_rows["signal_date"].min())[:7] if not live_rows.empty else None)
    alpha = _alpha_block(panel, month, live_start)

    paper, back = res.get("paper") or {}, res.get("backtest") or {}
    row = _perf()
    exp = load_expectation(OUT / "overfit_summary.json",
                           sample_cagr=float(row["年化收益(CAGR)"]) if row is not None else None)

    warns = []
    if not res.get("live_count"):
        warns.append("还没有真实信号（live）归档，当前净值全部来自回填，只能说明「策略此刻会这样走」，"
                     "不能当成「当时给过的建议」。")
    if not pts:
        warns.append(f"{month} 没有净值点，可能当月还没开始跟踪，或数据没覆盖到。")
    if not periods:
        warns.append("本月没有调仓。")
    if len(index) and (pd.Timestamp.now().normalize() - data_date).days > 7:
        warns.append(f"数据截至 {data_date.date()}，已超过 7 天未更新，先更新行情再看结论。")
    if alpha and alpha.get("z") is not None and abs(alpha["z"]) > 3:
        warns.append(f"本月残差 z={alpha['z']:+.1f}（|z|>3）：这不是运气能解释的，"
                     "先查记账口径、归档信号与数据完整性。")
    if paper and back and abs((paper.get("total") or 0) - (back.get("total") or 0)) > 0.005:
        warns.append("纸上净值与同期回测偏差超过 0.5pct，检查归档信号是否被改写（live 不该被 backfill 覆盖）。")

    return {
        "month": month,
        "generated_at": monthly.now_stamp(),
        "data_date": str(data_date.date()),
        "count": int(res.get("count", 0)),
        "live": int(res.get("live_count", 0)),
        "backfill": int(res.get("backfill_count", 0)),
        "first": res.get("first", ""),
        "last": res.get("last", ""),
        "paper": paper,
        "backtest": back,
        "month_ret": monthly.month_return(pts, "paper"),
        "month_ret_bt": monthly.month_return(pts, "backtest"),
        "periods": [
            {
                "signal_date": p.get("signal_date"), "exec_date": p.get("exec_date"),
                "ret": p.get("ret"), "cum": p.get("cum"), "source": p.get("source"),
                "holdings": p.get("holdings") or [],
            }
            for p in periods
        ],
        "next_reb": str(nxt.date()),
        "days_to_next": int(days_to_next),
        "expect": exp,
        "alpha": alpha,
        "warnings": warns,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", help="YYYY-MM，默认上月")
    ap.add_argument("--out", default=str(OUT / "reports"), help="输出目录")
    ap.add_argument("--json", action="store_true", help="只打印结构化结果，不写文件")
    args = ap.parse_args()

    month = args.month or monthly.prev_month()
    if len(month) != 7 or month[4] != "-":
        raise SystemExit(f"--month 需要 YYYY-MM 格式，收到 {month!r}")

    data = build_month_data(month)
    text = monthly.render_markdown(data)

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        return 0

    fp = Path(args.out) / f"monthly_{month}.md"
    atomic_write_text(fp, text)

    paper, back = data["paper"], data["backtest"]
    diff = (paper.get("total") or 0) - (back.get("total") or 0)
    print(f"[OK] {fp}")
    print(f"     {month} · {data['count']} 期（live {data['live']} / 回填 {data['backfill']}）")
    print(f"     纸上累计 {paper.get('total', 0)*100:+.1f}%  同期回测 {back.get('total', 0)*100:+.1f}%  "
          f"偏差 {diff*100:+.2f}pp")
    print(f"     下次调仓 {data['next_reb']}（还有 {data['days_to_next']} 个交易日）")
    if data["expect"]:
        print(f"     对外预期 {data['expect']['label']}（样本内 {data['expect'].get('sample', 0)*100:.1f}% 视为上限）")
    for w in data["warnings"]:
        print(f"     [注意] {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
