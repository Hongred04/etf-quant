"""
过拟合体检：CSCV/PBO + Deflated Sharpe + 滚动前向选择。

为什么需要这个脚本
------------------
本项目在同一段历史（2019-2026）上先后试过约 30 组变体（组合平滑、池子、闸门、
频率…），最后保留的那套参数"看起来最好"。这正是过拟合最典型的发生方式：
不是策略拟合了价格，而是**人挑参数时拟合了噪声**。

三个检验各回答一个问题：
1. CSCV/PBO      —— 「挑冠军」这个动作本身，有多少概率是在挑噪声？
2. Deflated SR   —— 试了 N 次之后，报告的 Sharpe 还有多显著？（顺带给出应有的折扣）
3. 滚动前向选择   —— 如果每 6 个月只用过去两年数据重挑一次参数，实盘会怎样？

用法
----
python scripts/overfit_check.py                 # 全部检验（约 5 分钟）
python scripts/overfit_check.py --quick         # 只跑 CSCV + DSR（复用已有网格结果）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from etfquant import metrics  # noqa: E402
from etfquant.io_utils import atomic_write_text, atomic_to_csv  # noqa: E402
from etfquant.backtest import Backtester  # noqa: E402
from etfquant.overfit import cscv_pbo, deflated_sharpe, walk_forward_select  # noqa: E402
from etfquant.pipeline import benchmark_series, load_panel  # noqa: E402
from etfquant.strategy import MomentumBaseline, TrendGatedAdaptive  # noqa: E402

START, END = "2019-01-02", "2026-12-31"
# END 故意写成一个"未来"的日期而不是当天：写死当天，数据一更新这套检验就会
# 静默地用旧窗口跑，而 summary 里的 n_days 看起来仍然正常。

# --------------------------------------------------------------------------- #
# 参数网格：项目实际探索过的方向，每项只动一个参数（外加几个双参数组合）
# 这样 CSCV 的结果对应"我们当初的搜索空间"，而不是凭空的笛卡尔积。
# --------------------------------------------------------------------------- #
GRID: list[tuple[str, dict]] = [
    ("默认", {}),
    # 调仓频率（前面已单独扫过 2/3/5/10/20）
    ("freq3", dict(rebalance_freq=3)),
    ("freq10", dict(rebalance_freq=10)),
    ("freq20", dict(rebalance_freq=20)),
    # 持仓只数
    ("top2", dict(top_n=2)),
    ("top4", dict(top_n=4)),
    ("top5", dict(top_n=5)),
    # 单只上限
    ("cap0.35", dict(max_weight=0.35)),
    ("cap0.60", dict(max_weight=0.60)),
    ("cap1.00", dict(max_weight=1.00)),
    # 排名缓冲带（1 = 关闭）
    ("buffer1", dict(buffer_mult=1)),
    ("buffer3", dict(buffer_mult=3)),
    # 拥挤度阈值
    ("vr2", dict(max_vr=2.0)),
    ("vr5", dict(max_vr=5.0)),
    # 闸门阈值
    ("rmid0.6", dict(regime_mid=0.6)),
    ("rmid0.8", dict(regime_mid=0.8)),
    # 敞口映射。注意：`regime_low` 已在 2026-10-29 降级为"默认关闭的第三档"——
    # 移除前实测 0.2 / 0.35 / 0.5 结果完全相同（死参数，见 README 7.5）。
    # 这里保留一个真正的"分档空仓"探针（第三档敞口归零），因为它是唯一有区分度的用法。
    ("gate_empty", dict(expo_low=0.0, regime_low=0.35)),
    ("expo_mid0.7", dict(expo_mid=0.7)),
    # 打分与权重
    ("score_multi", dict(score_mode="multi")),
    ("weight_invvol", dict(weight_mode="inv_vol")),
    # 初筛阈值
    ("minamt5e7", dict(min_amount=5e7)),
    ("minamt1e7", dict(min_amount=1e7)),
    ("minhist120", dict(min_hist=120)),
    ("minhist500", dict(min_hist=500)),
    # 双参数组合：搜索时最容易"刚好凑出来"的那几类
    ("freq3+top2", dict(rebalance_freq=3, top_n=2)),
    ("freq10+top4", dict(rebalance_freq=10, top_n=4)),
    ("freq5+buffer1", dict(rebalance_freq=5, buffer_mult=1)),
    ("cap0.35+top4", dict(max_weight=0.35, top_n=4)),
    ("top2+cap0.6", dict(top_n=2, max_weight=0.60)),
]


def _write_csv(df: pd.DataFrame, fp: Path, note: str = "", **kw) -> Path:
    """
    原子写 CSV，并容忍"文件正被别的程序打开"（Windows 上很常见：只看了一眼
    结果文件、或预览窗口没关，`os.replace` 就会报 WinError 5）。

    退路是写到同目录的 `xxx.new.csv` 并打印提示——宁可让人手动换一下，
    也不要静默丢失这一节的输出。
    """
    try:
        return atomic_to_csv(df, fp, **kw)
    except PermissionError:
        alt = fp.with_name(f"{fp.stem}.new{fp.suffix}")
        atomic_to_csv(df, alt, **kw)
        print(f"      [警告] {fp.name}{note} 被占用，已改写到 {alt.name}")
        return alt


def run_variant(panel, kwargs: dict) -> pd.Series:
    """跑一个变体，返回日收益序列。"""
    strat = TrendGatedAdaptive(panel, **kwargs)
    tg = strat.generate()
    res = Backtester(panel.close, panel.open_).run(tg, start=START, end=END)
    return res.nav.pct_change().dropna()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="跳过网格重跑，直接读 output/overfit_returns.csv")
    ap.add_argument("--blocks", type=int, default=16, help="CSCV 分块数（偶数）")
    ap.add_argument("--is-days", type=int, default=504, help="滚动选择：样本内窗口（交易日）")
    ap.add_argument("--oos-days", type=int, default=126, help="滚动选择：样本外窗口（交易日）")
    args = ap.parse_args()

    rets_fp = ROOT / "output" / "overfit_returns.csv"
    meta_fp = ROOT / "output" / "overfit_grid.csv"

    if args.quick and rets_fp.exists():
        rets = pd.read_csv(rets_fp, index_col=0, parse_dates=True)
        grid = pd.read_csv(meta_fp, index_col=0)
    else:
        print("[1/4] 加载面板…")
        panel = load_panel(ROOT / "data" / "etf_pool.csv", ROOT / "data" / "cache",
                           "2018-01-02", END)
        bench = benchmark_series(panel.close, "sh510300")
        print(f"      {panel.close.shape[1]} 只 · {panel.close.index[0].date()} ~ "
              f"{panel.close.index[-1].date()}")

        print(f"[2/4] 跑 {len(GRID)} 个变体（每个约 7 秒）…")
        cols, rows = {}, []
        t0 = time.time()
        for i, (name, kw) in enumerate(GRID, 1):
            try:
                r = run_variant(panel, kw)
            except Exception as exc:  # noqa: BLE001
                print(f"      [跳过] {name}: {type(exc).__name__}: {exc}")
                continue
            nav = (1 + r).cumprod()
            st = metrics.summarize(nav, bench)
            cols[name] = r
            rows.append({
                "variant": name, "params": json.dumps(kw, ensure_ascii=False),
                "cagr": float(st["年化收益(CAGR)"]), "sharpe": float(st["夏普比率"]),
                "mdd": float(st["最大回撤"]), "vol": float(st["年化波动"]),
            })
            print(f"      [{i:>2}/{len(GRID)}] {name:<18} CAGR {rows[-1]['cagr']*100:5.2f}% "
                  f"Sharpe {rows[-1]['sharpe']:.3f}  ({time.time()-t0:.0f}s)")

        rets = pd.DataFrame(cols)
        grid = pd.DataFrame(rows).set_index("variant")
        # 统一原子写：直接 to_csv 覆盖时，中途崩溃会留下半个文件被下游读到；
        # 而且 Windows 上若文件正被杀软/索引器占用，还会抛 PermissionError。
        _write_csv(rets, rets_fp, encoding="utf-8-sig")
        _write_csv(grid, meta_fp, encoding="utf-8-sig", index=True)

        # 参照：纯 20 日动量（无闸门、无缓冲带）——"不加复杂度"的基线
        try:
            s1 = MomentumBaseline(panel)
            r1 = Backtester(panel.close, panel.open_).run(s1.generate(), start=START, end=END)
            s1r = r1.nav.pct_change().dropna()
            st1 = metrics.summarize(r1.nav, bench)
            print(f"      [参照] 纯动量 CAGR {st1['年化收益(CAGR)']*100:.2f}% "
                  f"Sharpe {st1['夏普比率']:.3f} MDD {st1['最大回撤']*100:.2f}%")
            _write_csv(s1r.to_frame("ret"), ROOT / "output" / "overfit_s1.csv",
                       encoding="utf-8-sig")
        except Exception as exc:  # noqa: BLE001
            print(f"      [参照] 纯动量跑失败：{exc}")

    rets = rets.dropna(how="any")
    print(f"\n[3/4] 检验：{rets.shape[1]} 个变体 × {len(rets)} 个交易日")

    # ---------- 1) CSCV / PBO ----------
    cscv = cscv_pbo(rets.to_numpy(), n_blocks=args.blocks)
    print("\n" + "=" * 78)
    print("① 挑冠军是不是在挑噪声（CSCV / PBO）")
    print("=" * 78)
    print(f"  分块 {args.blocks} 块 · 组合数 {cscv['n_combos']}")
    print(f"  PBO = {cscv['pbo']:.1%}   （样本内冠军在样本外掉到中位数以下的概率）")
    print(f"  样本内冠军的样本外 Sharpe：中位 {np.median(cscv['oos_sharpe']):.3f} · "
          f"均值 {np.mean(cscv['oos_sharpe']):.3f} · "
          f"P10 {np.percentile(cscv['oos_sharpe'], 10):.3f}")
    print(f"  样本内 Sharpe 中位 {np.median(cscv['is_sharpe']):.3f} "
          f"→ 样本外 {np.median(cscv['oos_sharpe']):.3f}"
          f"（衰减 {np.median(cscv['is_sharpe'])-np.median(cscv['oos_sharpe']):.3f}）")
    pd.DataFrame({
        "is_sharpe": cscv["is_sharpe"], "oos_sharpe": cscv["oos_sharpe"],
        "omega": cscv["omega"], "lambda": cscv["lambda"],
        "picked": [rets.columns[i] for i in cscv["chosen"]],
    }).pipe(_write_csv, ROOT / "output" / "overfit_cscv.csv",
            encoding="utf-8-sig", index=False)

    # ---------- 2) Deflated Sharpe ----------
    # DSR 用**超额**收益（扣无风险利率），与项目报告的"夏普比率"同一口径；
    # 直接用原始收益会把 Sharpe 抬高约 0.1，DSR 也就跟着虚高。
    rf_daily = metrics.DEFAULT_RF / 252
    excess = rets - rf_daily
    best_name = grid["sharpe"].idxmax()
    best_r = excess[best_name].to_numpy()
    daily_sr = grid["sharpe"] / np.sqrt(252)          # 由年化 Sharpe 反推日频，仅用于取方差
    sr_var = float(np.var(daily_sr, ddof=1))
    n_grid = len(grid)
    print("\n" + "=" * 78)
    print("② 试了这么多次之后，Sharpe 还剩多少显著性（Deflated Sharpe）")
    print("=" * 78)
    print(f"  网格内最优：{best_name} · 年化 Sharpe {grid.loc[best_name, 'sharpe']:.3f} · "
          f"CAGR {grid.loc[best_name, 'cagr']*100:.2f}%")
    for label, n_trials in (("只算本网格", n_grid), ("计入历史全部试验（约 60）", 60)):
        ds = deflated_sharpe(best_r, n_trials=n_trials, sr_variance=sr_var)
        print(f"  {label:<22} N={n_trials:<3} 临界 Sharpe {ds['sr0']:.2f} · "
              f"实际 {ds['sr']:.2f} · DSR = {ds['dsr']:.1%}")
    ds_default = deflated_sharpe(excess["默认"].to_numpy(), n_trials=n_grid, sr_variance=sr_var)
    print(f"  默认参数（未被挑选）      N={n_grid:<3} DSR = {ds_default['dsr']:.1%}")

    # ---------- 3) 滚动前向选择 ----------
    print("\n" + "=" * 78)
    print("③ 每 6 个月重挑一次参数，实盘会怎样（滚动前向）")
    print("=" * 78)
    # 只把"默认 + 每个参数的一个档位"放进选择池，避免用 32 个近似重复的变体人为抬高选择噪声
    pool = ["默认", "freq3", "freq10", "top2", "top4", "cap0.35", "cap0.60",
            "buffer1", "buffer3", "vr2", "rmid0.6", "gate_empty", "score_multi"]
    pool = [c for c in pool if c in rets.columns]
    wf = walk_forward_select(rets[pool], is_days=args.is_days, oos_days=args.oos_days)
    if not wf.empty:
        _write_csv(wf, ROOT / "output" / "overfit_walk.csv",
                   encoding="utf-8-sig", index=False)
        print(wf.to_string(index=False))
        # 把各窗口的 OOS 收益串起来
        chain = float(np.prod(1 + wf["oos_ret"]) - 1)
        years = len(wf) * args.oos_days / 252
        cagr = (1 + chain) ** (1 / years) - 1 if years > 0 else 0.0
        fixed = rets["默认"].loc[wf["oos_start"].iloc[0]:]
        fixed_cagr = metrics.cagr((1 + fixed).cumprod())
        print(f"\n  滚动选择（样本外拼接）：累计 {chain*100:6.2f}% · 年化 {cagr*100:6.2f}% · "
              f"同期固定默认 {fixed_cagr*100:6.2f}%")
        print(f"  选中的变体：{'、'.join(wf['picked'].tolist())}")
        print(f"  事后最优（不可实现）：{'、'.join(wf['hindsight'].tolist())}")

        # ---------- 4) 参数平台 ----------
        print("\n" + "=" * 78)
        print("④ 默认值是不是踩在「平台」上（各参数单独变动时的 Sharpe）")
        print("=" * 78)
        for prefix in ("freq", "top", "cap", "buffer", "vr", "rmid", "rlow", "expo",
                       "minamt", "minhist", "score", "weight"):
            sub = grid[grid.index.str.startswith(prefix)]
            if len(sub) < 2:
                continue
            line = "  ".join(f"{k}:{v:.3f}" for k, v in sub["sharpe"].items())
            base = grid.loc["默认", "sharpe"]
            spread = sub["sharpe"].max() - sub["sharpe"].min()
            print(f"  {prefix:<8} {line}   （极差 {spread:.3f}）")

    # ---------- 5) 参数集成 ----------
    print("\n" + "=" * 78)
    print("⑤ 参数集成能不能抵消「单点参数运气」（两两等权合成）")
    print("=" * 78)
    print("  说明：按日收益等权合成 ≈ 把资金对半分给两个 sleeve，每个调仓日再平衡。")
    segs = [("19-22", "2019-01-02", "2022-12-30"), ("23-26", "2023-01-03", END)]
    combos = [
        ("单用 5 日（现状）", ("默认",)),
        ("5 日 + 10 日", ("默认", "freq10")),
        ("5 日 + 20 日", ("默认", "freq20")),
        ("3/5/10 日三频", ("freq3", "默认", "freq10")),
        ("5 日 + 逆波动加权", ("默认", "weight_invvol")),
        ("5 日 + rmid0.8", ("默认", "rmid0.8")),
    ]
    rows = []
    for label, names in combos:
        names = [n for n in names if n in rets.columns]
        if not names:
            continue
        r = rets[list(names)].mean(axis=1)
        nav = (1 + r).cumprod()
        # 只用不依赖基准的指标：--quick 模式下没有加载面板/基准
        row = {"组合": label, "只数": len(names),
               "年化": float(metrics.cagr(nav)), "夏普": float(metrics.sharpe(nav)),
               "回撤": float(metrics.max_drawdown(nav))}
        for lab, a, b in segs:
            rr = r.loc[a:b]
            row[f"{lab}年化"] = float((1 + rr).prod() ** (252 / max(1, len(rr))) - 1)
        rows.append(row)
    ens = pd.DataFrame(rows)
    print(ens.assign(年化=lambda d: (d.年化 * 100).round(2),
                     夏普=lambda d: d.夏普.round(3),
                     回撤=lambda d: (d.回撤 * 100).round(2),
                     **{c: (ens[c] * 100).round(1) for c in ens.columns if c.endswith("年化") and c != "年化"}
                     ).to_string(index=False))
    _write_csv(ens, ROOT / "output" / "overfit_ensemble.csv",
               encoding="utf-8-sig", index=False)

    # ---------- 汇总落盘（Web「效果验证」页读这个文件） ----------
    summary = {
        "n_variants": int(rets.shape[1]),
        "n_days": int(rets.shape[0]),
        "pbo": round(float(cscv["pbo"]), 4),
        "is_sharpe_median": round(float(np.median(cscv["is_sharpe"])), 4),
        "oos_sharpe_median": round(float(np.median(cscv["oos_sharpe"])), 4),
        "best_variant": str(best_name),
        "best_cagr": round(float(grid.loc[best_name, "cagr"]), 4),
        "best_sharpe": round(float(grid.loc[best_name, "sharpe"]), 4),
        "default_rank": int(grid["sharpe"].rank(ascending=False, method="min").loc["默认"]),
        "dsr_best": round(float(deflated_sharpe(best_r, n_trials=n_grid, sr_variance=sr_var)["dsr"]), 4),
        "dsr_default": round(float(ds_default["dsr"]), 4),
    }
    if not wf.empty:
        chain = float(np.prod(1 + wf["oos_ret"]) - 1)
        years = len(wf) * args.oos_days / 252
        fixed = rets["默认"].loc[wf["oos_start"].iloc[0]:]
        summary.update({
            "wf_cagr": round(float((1 + chain) ** (1 / years) - 1), 4),
            "wf_fixed_cagr": round(float(metrics.cagr((1 + fixed).cumprod())), 4),
            "wf_windows": int(len(wf)),
            "wf_picked": wf["picked"].tolist(),
        })
    # 对外口径：样本内年化 → 可对外引用的区间（README / 桌面 / 手机 / 月报 共用这一份）
    from etfquant.expectation import expectation

    if "wf_cagr" in summary:
        try:
            summary["expectation"] = expectation(
                float(grid.loc["默认", "cagr"]), summary["wf_cagr"],
                summary["pbo"], summary["n_variants"],
            )
        except (ValueError, KeyError) as exc:  # 锚点算不出来就别写，界面会退回不显示
            print(f"[警告] 无法生成对外口径：{exc}")

    atomic_write_text(ROOT / "output" / "overfit_summary.json",
                      json.dumps(summary, ensure_ascii=False, indent=2))

    print("\n结论要点已写入 README「7.5 过拟合体检」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
