#!/usr/bin/env python
"""一条命令跑完所有自检：单测 + 审计（可选界面冒烟）。

为什么需要它
------------
之前"记得跑测试"完全靠人。改完前端忘了跑冒烟、改完数据层忘了跑审计，
都属于会被遗漏的那一类。这里把顺序固定下来，任何一步失败立即停止并返回非零。

用法
----
    .venv/Scripts/python.exe scripts/check_all.py            # 单测 + 审计
    .venv/Scripts/python.exe scripts/check_all.py --with-ui  # 再加 12 个界面用例
    .venv/Scripts/python.exe scripts/check_all.py --no-cov   # 跳过覆盖率（快 20 秒）
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(name: str, cmd: list[str]) -> tuple[bool, float]:
    print(f"\n{'=' * 70}\n== {name}\n{'=' * 70}")
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=str(ROOT))
    return proc.returncode == 0, time.time() - t0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-ui", action="store_true", help="附带界面冒烟（需要 Edge）")
    ap.add_argument("--no-cov", action="store_true", help="跳过覆盖率统计")
    args = ap.parse_args()

    py = sys.executable
    steps: list[tuple[str, list[str]]] = []

    test_cmd = [py, "-m", "pytest", "tests", "-q"]
    if not args.no_cov:
        test_cmd += ["--cov=etfquant", "--cov=server", "--cov-report=term:skip-covered"]
    steps.append(("单元测试", test_cmd))
    steps.append(("口径审计", [py, "scripts/audit.py"]))
    if args.with_ui:
        steps.append(("界面冒烟", [py, "scripts/ui_smoke.py", "--no-shot"]))

    results = []
    for name, cmd in steps:
        ok, dur = run(name, cmd)
        results.append((name, ok, dur))
        if not ok:
            print(f"\n[FAIL] {name} 失败，后续步骤已跳过")
            break

    print(f"\n{'=' * 70}\n== 汇总\n{'=' * 70}")
    for name, ok, dur in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}  {dur:.1f}s")
    failed = [r for r in results if not r[1]]
    if failed:
        print(f"\n{len(failed)} 项失败")
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
