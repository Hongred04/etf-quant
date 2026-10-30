"""
统一命令行入口。

设计目标
--------
1. **人和 Agent 只记一套命令**：界面上点「更新行情并重算」，终端里就是
   `python -m etfquant refresh`；两者跑同一串脚本、同一套参数。
2. **不重复实现**：每条子命令只转发到 `scripts/` 里的某个脚本，参数原样透传，
   所以 README 写脚本路径、终端敲短命令，永远不会分叉。
3. **能自检**：`doctor` 一次给出数据截止日、主线绩效、对外口径，适合无界面环境
   （Agent、定时任务）判断状态，不必打开网页。

用法
----
python -m etfquant refresh                 # 更新行情 → 回测 → 信号（界面同款）
python -m etfquant signal                  # 只出信号
python -m etfquant status                  # 今天该不该动手、下次哪天换
python -m etfquant doctor                  # 数据新鲜度 + 绩效 + 口径
python -m etfquant monthly                 # 上月月报
python -m etfquant audit                   # 23 项口径自检
python -m etfquant test                    # 单元测试
python -m etfquant research                # 列出一次性实验脚本
python -m etfquant research ab_momvol      # 运行其中一个（参数透传）
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
RESEARCH = SCRIPTS / "research"

# 子命令 → 脚本（以下都在 scripts/ 根目录）。研究类脚本在 scripts/research/ 下，见 research 子命令。
COMMANDS: dict[str, tuple[str, str]] = {
    "universe": ("build_universe.py", "扫描全市场 ETF"),
    "download": ("download_history.py", "下载 / 增量更新历史日线"),
    "backtest": ("run_backtest.py", "运行四策略回测"),
    "signal": ("run_signal.py", "生成最新选股信号（钉在最近调仓日）"),
    "report": ("make_report.py", "生成 HTML 研究报告"),
    "status": ("rebalance_status.py", "今天该不该动手、下次哪天换"),
    "monthly": ("monthly_report.py", "生成前向验证月报"),
    "audit": ("audit.py", "23 项口径自检"),
    "test": ("test_runner.py", "运行单元测试"),
    "mobile-data": ("export_mobile_data.py", "导出 App 数据 data.js"),
    "verify-engine": ("verify_mobile_engine.py", "手机端引擎与桌面端对拍"),
    "backfill": ("backfill_archive.py", "回填历史归档信号"),
    "sync-readme": ("sync_readme.py", "把 README 数字同步为最新产物"),
    "overfit": ("overfit_check.py", "过拟合体检（PBO / DSR / 滚动前向）"),
    "deep": ("deep_check.py", "深度诊断（IC / 归因 / 执行 / 容量 / 误差棒）"),
    "shares": ("collect_shares.py", "采集 ETF 份额日快照（历史不可得，只能日积月累）"),
    "dsr": ("dsr.py", "按试验登记簿重算 Deflated Sharpe"),
    "manifest": ("manifest.py", "生成复现清单（输入/输出 SHA256）"),
}


def _run(fp: Path, extra: list[str]) -> int:
    if not fp.exists():
        print(f"[ERR] 未找到脚本 {fp}")
        return 1
    return subprocess.call([sys.executable, str(fp), *extra], cwd=str(ROOT))


def _doctor() -> int:
    """数据新鲜度 + 主线绩效 + 对外口径，一次说完。"""
    import pandas as pd

    sys.path.insert(0, str(ROOT))
    from etfquant.expectation import load as load_exp

    out = ROOT / "output"
    nav_fp, perf_fp = out / "nav_main.csv", out / "perf_main.csv"
    if not nav_fp.exists() or not perf_fp.exists():
        print("[FAIL] 缺少回测产物，先跑：python -m etfquant backtest")
        return 1

    nav = pd.read_csv(nav_fp, index_col=0, parse_dates=True)
    print(f"数据截止   {nav.index[-1].strftime('%Y-%m-%d')}（{len(nav)} 个交易日）")
    # K 线入口状态：被限流时 download 会整体失败，先看这里再决定要不要重试
    from etfquant.datafeed import endpoint_health  # noqa: PLC0415
    print("K线入口    " + "  ".join(f"{k} {v}" for k, v in endpoint_health().items()))

    perf = pd.read_csv(perf_fp, index_col=0)
    hit = perf[perf["策略"].astype(str).str.startswith("S4")]
    if hit.empty:
        print("[WARN] perf_main.csv 里没有 TGAR 行")
        return 1
    r = hit.iloc[0]
    print(f"TGAR       CAGR {r['年化收益(CAGR)'] * 100:.2f}% · "
          f"MaxDD {r['最大回撤'] * 100:.2f}% · 夏普 {r['夏普比率']:.3f} · "
          f"卡玛 {r['卡玛比率']:.2f}")

    exp = load_exp(out / "overfit_summary.json", sample_cagr=float(r["年化收益(CAGR)"]))
    if exp:
        print(f"对外口径   {exp['label']}（样本内 {exp['sample'] * 100:.1f}% 为上限，"
              f"锚点 {exp['anchor'] * 100:.1f}%，PBO {exp['pbo'] * 100:.1f}%）")
    print("下一步     python -m etfquant audit（23 项自检） / status（今天该不该动手）")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="etfquant", description="ETF 量化选股工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, (_, help_text) in COMMANDS.items():
        p = sub.add_parser(name, help=help_text)
        p.add_argument("args", nargs=argparse.REMAINDER)

    p_ref = sub.add_parser("refresh", help="更新行情 → 回测 → 信号（界面同款）")
    p_ref.add_argument("args", nargs=argparse.REMAINDER)

    p_res = sub.add_parser("research", help="列出 / 运行一次性实验脚本")
    p_res.add_argument("name", nargs="?", help="脚本名（不带 .py）")
    p_res.add_argument("args", nargs=argparse.REMAINDER)

    sub.add_parser("doctor", help="数据截止日 + 绩效 + 对外口径")

    ns = ap.parse_args(argv)
    extra = getattr(ns, "args", []) or []

    if ns.cmd == "doctor":
        return _doctor()

    if ns.cmd == "refresh":
        # 顺序不能换：回测依赖新行情，信号依赖新回测
        for step in ("download", "backtest", "signal"):
            script, label = COMMANDS[step]
            print(f"\n=== {label} ===")
            code = _run(SCRIPTS / script, extra if step == "download" else [])
            if code != 0:
                print(f"[ERR] {label} 失败（退出码 {code}），流程中止")
                return code

        # 份额快照：顺手记一笔。失败不影响主流程——它的用途是"半年后才有得测"，
        # 没必要因为它今天挂了就阻断回测与信号。
        print("\n=== 采集 ETF 份额快照 ===")
        if _run(SCRIPTS / "collect_shares.py", []) != 0:
            print("[WARN] 份额快照采集失败（不影响回测与信号）")
        return 0

    if ns.cmd == "research":
        if not ns.name:
            for fp in sorted(RESEARCH.glob("*.py")):
                print(f"  {fp.stem}")
            print("\n运行：python -m etfquant research <名字> [参数]")
            return 0
        return _run(RESEARCH / f"{ns.name}.py", ns.args or [])

    script, _ = COMMANDS[ns.cmd]
    return _run(SCRIPTS / script, extra)


if __name__ == "__main__":
    raise SystemExit(main())
