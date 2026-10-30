"""
打印当前调仓状态：今天该不该动手、下次哪天换。

为什么要一个命令行入口
----------------------
界面里的提示只在打开页面时看得到；脚本 / 定时任务 / Agent 需要一个不依赖浏览器的
判断入口。这里复用同一套 `etfquant.rebalance`，避免又写一遍相位逻辑。

用法
----
python scripts/rebalance_status.py            # 人读的一行摘要
python scripts/rebalance_status.py --json     # 机器读
python scripts/rebalance_status.py --check    # 只在"今天要动手"时退出码 0，其余为 3

退出码：
    0  今天需要动手（买入日或调仓日）
    3  今天不需要动手
    1  出错（没有信号、数据不足等）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出 JSON（供脚本/Agent 解析）")
    ap.add_argument("--check", action="store_true", help="只在今天需要动手时返回 0")
    args = ap.parse_args()

    sig = server.api_signal()
    if "error" in sig:
        print(f"[ERR] {sig['error']}", file=sys.stderr)
        return 1

    remind = sig.get("remind") or {}
    need_action = bool(remind.get("should"))
    payload = {
        "today": sig.get("today"),
        "status": sig.get("status"),
        "need_action": need_action,
        "rebalance_date": sig.get("reb_date"),
        "exec_date": sig.get("exec_date"),
        "next_rebalance": sig.get("next_reb"),
        "next_estimated": sig.get("next_est"),
        "days_to_next": sig.get("days_to_next"),
        "held_days": sig.get("held_days"),
        "title": remind.get("title", ""),
        "body": remind.get("body", ""),
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        label = {
            "pending": "还没到买入日",
            "exec_today": "今天开盘买入",
            "holding": "持有中，无需操作",
            "today": "今天是调仓日，收盘后重算",
            "due": "调仓日已过，清单作废，请重算",
        }.get(payload["status"], payload["status"])
        print(f"今天 {payload['today']} · {label}")
        print(f"  调仓日 {payload['rebalance_date']} · 买入日 {payload['exec_date']} · "
              f"下次 {payload['next_rebalance']}"
              f"{'（估算）' if payload['next_estimated'] else ''}"
              f"（还有 {payload['days_to_next']} 个交易日）")
        if need_action:
            print(f"  → {payload['title']}：{payload['body']}")

    if args.check:
        return 0 if need_action else 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
