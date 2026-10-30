"""
深度诊断：这条 alpha 到底是什么、值多少、能装多少钱、误差棒多宽。

与 `overfit_check.py` 的分工
---------------------------
* `overfit_check` 问"这个数字有多少来自挑选"（PBO / DSR / 滚动前向）；
* 本脚本问**为什么**：信号有预测力吗、收益是 alpha 还是 beta、执行吃掉多少、
  容量多大、单一历史路径的置信区间多宽。

用法：
    python scripts/deep_check.py            # 快速诊断（IC / 分位 / 归因 / 执行 / 容量 / 误差棒）
    python scripts/deep_check.py --sweep    # 追加集中度扫描（多跑 6 次回测，约 4 分钟）

产物：output/diagnostics.json（README 7.10 引用的唯一数据源）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from etfquant import diagnostics as D  # noqa: E402
from etfquant import metrics  # noqa: E402
from etfquant.backtest import Backtester  # noqa: E402
from etfquant.io_utils import atomic_write_text  # noqa: E402
from etfquant.pipeline import load_panel  # noqa: E402
from etfquant.strategy import TrendGatedAdaptive  # noqa: E402

POOL = ROOT / "data" / "etf_pool.csv"
CACHE = ROOT / "data" / "cache"
OUT = ROOT / "output"
S4 = "S4-TGAR(自研)"
MKT, SMALL = "sh510300", "sh512100"
START = "2019-01-02"
SWEEP_START = "2022-01-04"     # 该时点池内已有 127 只，top_n=50 也能正常建仓


def pct(x: float, nd: int = 1) -> str:
    return "—" if x is None or not np.isfinite(x) else f"{x*100:.{nd}f}%"


def num(x: float, nd: int = 2) -> str:
    return "—" if x is None or not np.isfinite(x) else f"{x:.{nd}f}"


def bpf(x: float) -> str:
    return "—" if x is None or not np.isfinite(x) else f"{x:+.1f}"


def targets_frame(sig: dict, columns) -> pd.DataFrame:
    """把 strategy.generate() 的 {日期: {代码: 权重}} 变成稠密矩阵。

    必须是**完整组合**（卖出的标的显式记 0）。用稀疏表示 + ffill 会让"已卖出"
    的标的被沿用下去，而用 fillna(0) 又会把"当天没信号"误判成清仓——
    这两种错都不会报错，只会让换手和执行成本算成另一个数。
    """
    idx = pd.DatetimeIndex(sorted(sig))
    tw = pd.DataFrame(0.0, index=idx, columns=list(columns))
    for d, w in sig.items():
        for k, v in w.items():
            if k in tw.columns:
                tw.loc[d, k] = float(v)
    return tw


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", action="store_true", help="追加集中度扫描（多跑若干次回测）")
    ap.add_argument("--force", action="store_true",
                    help="覆盖已冻结的诊断快照（会改写月报的 β 基准，历史累计残差不再可比）")
    args = ap.parse_args(argv)

    diag_fp = OUT / "diagnostics.json"
    if diag_fp.exists() and not args.force:
        try:
            old = json.loads(diag_fp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            old = {}
        frozen = (old.get("meta") or {}).get("frozen_at")
        if frozen:
            old_reg = old.get("regression") or {}
            print(f"[拒绝覆盖] 诊断快照已冻结于 {frozen}，"
                  f"α={old_reg.get('alpha_ann')}、β_mom={old_reg.get('beta_mom')}")
            print("           月报的『alpha 还在不在』以这份 β 为基准，改写会让历史累计残差不可比。")
            print("           确实要更新请加 --force（并接受月报需要重算历史）。")
            return 2

    panel = load_panel(POOL, CACHE, "2018-01-02", "2026-12-31")
    strat = TrendGatedAdaptive(panel)
    close, open_ = panel.close, panel.open_
    sig_dict = strat.generate()
    tw = targets_frame(sig_dict, close.columns)
    dates = tw.index[tw.index >= pd.Timestamp(START)]
    tw = tw.loc[dates]
    result: dict = {"meta": {
        # frozen_at 是这份快照的"凭证"：月报读它的 β 当固定基准，
        # 没有时间戳就无法判断某个历史数字是哪一版算出来的。
        "frozen_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "start": str(dates[0].date()), "end": str(close.index[-1].date()),
        "n_decisions": int(len(dates)), "n_names": int(close.shape[1]),
        "target_rowsum_min": float(tw.sum(axis=1).min()),
        "target_rowsum_max": float(tw.sum(axis=1).max()),
    }}
    if abs(tw.sum(axis=1).median() - 1.0) > 1e-6:
        print(f"[!] 目标权重行和异常（中位 {tw.sum(axis=1).median():.3f}），"
              f"执行/容量两个数字会失真")

    # ------------------------------------------------------------------ 1 信号层
    print(f"\n[1/7] 信号层：R20 的预测力（{len(dates)} 个决策日，横截面最大 {close.shape[1]} 只）")
    z = strat.fb.z_r20.loc[dates]
    decay = D.ic_decay(close, z, dates, horizons=(1, 5, 10, 20, 60), min_names=20)
    print(f"    {'持有期':>5} {'IC':>7} {'ICIR':>6} {'NW t':>6} {'胜率':>5}   分位价差 Q9−Q0（同持有期）")
    rows = []
    for h, r in decay.iterrows():
        fwd_h = D.fwd_return(close, h).reindex(dates)
        qs = D.quantile_spread(z, fwd_h, dates, n_groups=10, min_names=50)
        spread = float(qs["Q9"] - qs["Q0"])
        print(f"    {h:>3}d {r['ic']:>7.3f} {r['icir']:>6.2f} {r['t']:>6.2f} {r['hit']*100:>4.0f}%"
              f"   {spread:>+7.1f}bp")
        rows.append({"horizon": int(h),
                     **{k: (None if not np.isfinite(v) else float(v)) for k, v in r.items()},
                     "q9_minus_q0_bp": spread,
                     "quantiles_bp": {k: (None if not np.isfinite(v) else float(v))
                                      for k, v in qs.items()}})
    result["ic_decay"] = rows

    print("    被选中的 top3 vs 全池（这是最贴近策略的检验）")
    tails = {}
    for h in (5, 20):
        fwd_h = D.fwd_return(close, h).reindex(dates)
        tp = D.tail_spread(z, fwd_h, dates, top=3, min_names=20)
        tails[h] = tp
        print(f"      {h:>2}d  均值 {tp['mean_bp']:>+6.1f}bp  中位 {tp['median_bp']:>+6.1f}bp  "
              f"胜率 {tp['hit']*100:>3.0f}%   t(NW) {num(tp['t'])}  vs 朴素 t {num(tp['t_naive'])}")
    result["tail_spread"] = {str(k): v for k, v in tails.items()}

    # ------------------------------------------------------------------ 2 分区间
    print("\n[2/7] 分区间：信号在衰减吗（判据跑前写定：斜率 t<−2 判衰减，|t|<1 判无证据）")
    periods = {"2019-2022": ("2019-01-01", "2022-12-31"),
               "2023-2026": ("2023-01-01", "2026-12-31")}
    ps = D.period_summary(z, close, dates, periods, horizon=5, top=3)
    print(f"    {'区间':<11}{'决策日':>5}{'IC':>8}{'IC t':>7}{'Q9−Q0':>9}{'top3−池':>9}{'t':>7}{'胜率':>6}")
    for r in ps.to_dict("records"):
        if "ic" not in r:
            print(f"    {r['period']:<11}{r['n']:>5}   样本不足")
            continue
        print(f"    {r['period']:<11}{r['n']:>5}{r['ic']:>8.3f}{r['ic_t']:>7.2f}"
              f"{bpf(r['q_spread_bp']):>9}{bpf(r['tail_mean_bp']):>9}"
              f"{num(r['tail_t']):>7}{r['tail_hit']*100:>5.0f}%")

    fwd5 = D.fwd_return(close, 5).reindex(dates)
    ic5 = D.rowwise_spearman(z, fwd5, min_names=20)
    ann = ic5.groupby(ic5.index.year).agg(ic="mean", n="count")
    print("    年度 IC：" + "  ".join(f"{y}:{v:+.3f}({int(c)})"
                                     for y, v, c in zip(ann.index, ann["ic"], ann["n"])))
    trends = {
        "IC": D.trend_test(ic5),
        "Q9−Q0": D.trend_test(D.quantile_gap_series(z, fwd5, dates)),
        "top3−池": D.trend_test(D.tail_spread_series(z, fwd5, dates)),
    }
    for k, v in trends.items():
        print(f"    趋势 {k:<8} 每年 {bpf(v['slope_per_year']*1e4)}bp   t={num(v['t'])}   → {v['verdict']}")
    result["periods"] = ps.to_dict("records")
    result["ic_annual"] = {int(y): {"ic": float(v), "n": int(c)}
                           for y, v, c in zip(ann.index, ann["ic"], ann["n"])}
    result["trend"] = trends

    # ------------------------------------------------------------------ 3 归因
    print("\n[3/7] 因子归因：17% 里有多少是 beta")
    nav_all = pd.read_csv(OUT / "nav_main.csv", index_col=0, parse_dates=True)
    nav, bench_all = nav_all[S4], nav_all["基准-沪深300"]
    ret_all = close.pct_change(fill_method=None)
    factors = pd.DataFrame({
        "mkt": ret_all[MKT],
        "mom": D.momentum_factor(close, lookback=20, skip=5, q=0.10, min_names=20),
        "smb": ret_all[SMALL] - ret_all[MKT],
    }).dropna()
    reg = D.factor_regression(nav, factors)
    print(f"    年化 alpha {pct(reg['alpha_ann'],1)}（t={reg['alpha_t']:.2f}）   "
          f"β_mkt {reg['beta_mkt']:.2f}(t={reg['t_mkt']:.0f})  "
          f"β_mom {reg['beta_mom']:.2f}(t={reg['t_mom']:.0f})  "
          f"β_smb {reg['beta_smb']:.2f}(t={reg['t_smb']:.0f})")
    print(f"    R² {reg['r2']:.2f}（只放 mkt 时 {reg['r2_mkt_only']:.2f}）"
          f" → 动量 beta 额外解释 {(reg['r2']-reg['r2_mkt_only'])*100:.0f}pp 的方差")
    result["regression"] = {k: (None if isinstance(v, float) and not np.isfinite(v) else v)
                            for k, v in reg.items()}

    # ------------------------------------------------------------------ 3 闸门
    print("\n[3/7] 闸门机制：关闸期间动量因子本身是不是亏的")
    w = pd.read_csv(OUT / "weights_s4_tgar_main.csv", index_col=0, parse_dates=True)
    cat = panel.meta["category"] if "category" in panel.meta else pd.Series(dtype=object)
    expo = D.equity_exposure(w, cat)
    split = D.exposure_split(factors["mom"], expo)
    print(f"    权益满仓天数占比 {(expo >= 0.75).mean()*100:.0f}%，半仓 {(expo < 0.75).mean()*100:.0f}%"
          f"（毛敞口恒为 1.0：另一半在债券里，只看行和会误判成「闸门从不生效」）")
    print(f"    动量因子年化：闸门开 {pct(split['on']['mean_ann'],1)}（n={split['on']['n']}）  "
          f"闸门关 {pct(split['off']['mean_ann'],1)}（n={split['off']['n']}）")
    result["gate"] = {"equity_full_share": float((expo >= 0.75).mean()), **split}

    # ------------------------------------------------------------------ 4 执行
    print("\n[4/7] 执行缺口：信号日收盘 → 次日开盘（按目标权重，不是每日漂移）")
    gap = D.execution_gap(tw, open_, close, amount=panel.amount, min_amount=2e7)
    a, lq = gap["all"], gap["liquid"]
    print(f"    成交 {gap['n_trades']} 笔，其中 {gap['share_illiquid']*100:.0f}% 落在成交额 <2000 万的"
          f"薄量日（open 可能只是零星成交价，不代表可成交价）")
    print(f"    全样本  ：买 {a['buy_gap_mean_bp']:+.1f}bp  卖 {a['sell_gap_mean_bp']:+.1f}bp"
          f"  → 每单位 {a['per_unit_bp']:+.1f}bp（中位 {a['median_bp']:+.1f}bp），"
          f"年化 {a['annual_drag']*100:+.2f}%")
    print(f"    可成交笔：买 {lq['buy_gap_mean_bp']:+.1f}bp  卖 {lq['sell_gap_mean_bp']:+.1f}bp"
          f"  → 每单位 {lq['per_unit_bp']:+.1f}bp（中位 {lq['median_bp']:+.1f}bp），"
          f"年化 {lq['annual_drag']*100:+.2f}%"
          f"（年换手 {lq['turnover_per_year']:.1f} 倍）")
    result["execution"] = gap

    # ------------------------------------------------------------------ 5 容量
    print("\n[5/7] 容量：建完一次仓需要几天（ADV20 × 10% 参与率）")
    adv = panel.amount.rolling(20).mean().shift(1)
    cap = D.capacity(tw, adv, aum_grid=(1e7, 5e7, 1e8, 5e8), participation=0.10)
    for r in cap.itertuples():
        print(f"    AUM {r.AUM/1e4:>6.0f}万  中位 {r.median_days:>5.1f} 天  p90 {r.p90_days:>5.1f}  "
              f"p99 {r.p99_days:>6.1f}  >3 天的比例 {r.share_over_3d*100:>3.0f}%")
    result["capacity"] = cap.to_dict("records")

    # ------------------------------------------------------------------ 6 误差棒
    print("\n[6/7] 误差棒：循环块 bootstrap（block=20，2000 次）")
    r = nav.pct_change(fill_method=None).dropna()
    cal_per_day = (nav.index[-1] - nav.index[0]).days / len(nav)
    boot = D.block_bootstrap(r, block=20, n_boot=2000, seed=7, cal_per_day=cal_per_day)
    point = {"cagr": float(metrics.cagr(nav)), "mdd": float(metrics.max_drawdown(nav)),
             "sharpe": float(metrics.sharpe(nav))}
    print(f"    CAGR {pct(point['cagr'],1):>7}  95% CI [{pct(boot['cagr']['p2.5'],1)}, "
          f"{pct(boot['cagr']['p97.5'],1)}]  中位 {pct(boot['cagr']['p50'],1)}")
    print(f"    MDD  {pct(point['mdd'],1):>7}  95% CI [{pct(boot['mdd']['p2.5'],1)}, "
          f"{pct(boot['mdd']['p97.5'],1)}]  中位 {pct(boot['mdd']['p50'],1)}")
    print(f"    夏普 {num(point['sharpe']):>7}  95% CI [{boot['sharpe']['p2.5']:.2f}, "
          f"{boot['sharpe']['p97.5']:.2f}]")
    print(f"    P(CAGR ≤ 0) = {boot['p_cagr_le_0']*100:.1f}%")
    result["bootstrap"] = {"point": point, "block": boot["block"], "n_boot": 2000,
                           "cagr": boot["cagr"], "mdd": boot["mdd"],
                           "sharpe": boot["sharpe"], "p_cagr_le_0": boot["p_cagr_le_0"]}

    # ------------------------------------------------------------------ 集中度
    if args.sweep:
        print(f"\n[+] 集中度扫描：只改 top_n，闸门/缓冲带/成本完全不动，统一起点 {SWEEP_START}")
        bt = Backtester(close, open_)
        bench = bench_all.loc[SWEEP_START:]   # 必须是基准序列，不是策略自己
        sweep = [{"top_n": 0, "label": "沪深300", "cagr": float(metrics.cagr(bench)),
                  "mdd": float(metrics.max_drawdown(bench)),
                  "sharpe": float(metrics.sharpe(bench)), "turnover": 0.0}]
        for n in (1, 3, 5, 10, 20, 50):
            sg = TrendGatedAdaptive(panel, top_n=n).generate()
            res = bt.run(sg, start=SWEEP_START)
            v = res.nav
            yrs = max((v.index[-1] - v.index[0]).days / 365.25, 1e-9)
            sweep.append({
                "top_n": n, "label": f"top{n}",
                "cagr": float(metrics.cagr(v)), "mdd": float(metrics.max_drawdown(v)),
                "sharpe": float(metrics.sharpe(v)),
                "turnover": float(res.turnover.sum() / yrs),
            })
            s = sweep[-1]
            print(f"    top{n:<3} CAGR {pct(s['cagr'],1):>7}  回撤 {pct(s['mdd'],1):>7}  "
                  f"夏普 {s['sharpe']:.2f}  年换手 {s['turnover']:>5.1f}")
        result["sweep"] = sweep

    atomic_write_text(OUT / "diagnostics.json", json.dumps(result, ensure_ascii=False, indent=2))
    print("\n[OK] 已写 output/diagnostics.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
