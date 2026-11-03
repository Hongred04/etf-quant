#!/usr/bin/env python
"""界面冒烟检查：无头 Edge 打开每个页面，读探针结果并留截图。

为什么不用截图对比
------------------
字体渲染与抗锯齿会让像素级 diff 天天报假警。这里检查的是**结构与运行状态**：
JS 有没有抛错、页面有没有横向溢出、正文是不是空的、有没有 NaN/undefined 漏到界面上。
截图只作为人工复核的附件，不参与判定。

用法
----
    .venv/Scripts/python.exe scripts/ui_smoke.py            # 全部页面，含截图
    .venv/Scripts/python.exe scripts/ui_smoke.py --no-shot  # 只跑检查，快一半
    .venv/Scripts/python.exe scripts/ui_smoke.py --only track signal

依赖：本机安装 Edge（或 Chrome），通过 UI_SMOKE_BROWSER 指定其它路径。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHOT_DIR = ROOT / "ui_review" / "smoke"

BROWSER_CANDIDATES = [
    os.environ.get("UI_SMOKE_BROWSER", ""),
    r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    r"C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    r"C:/Program Files/Google/Chrome/Application/chrome.exe",
]

# 名称, 路径, 宽, 高, 是否应出现 K 线弹窗
CASES = [
    ("overview", "/#overview", 1280, 900, False),
    ("signal", "/#signal", 1280, 900, False),
    ("track", "/#track", 1280, 900, False),
    ("portfolio", "/#portfolio", 1280, 900, False),
    ("myholdings", "/#myholdings", 1280, 900, False),
    ("strategy", "/#strategy", 1280, 900, False),
    # research 页长期没进用例表：它恰恰是改动最多、硬编码数字最多的一页。
    # 12 个用例全绿时，这一页可能整块是旧数据，冒烟发现不了。
    ("research", "/#research", 1280, 900, False),
    ("data", "/#data", 1280, 900, False),
    ("narrow-overview", "/#overview", 430, 820, False),
    ("narrow-signal", "/#signal", 430, 820, False),
    ("narrow-track", "/#track", 430, 820, False),
    ("narrow-strategy", "/#strategy", 430, 820, False),
    ("narrow-research", "/#research", 430, 820, False),
    ("kline", "/?kline=sh516620&theme=dark", 1180, 860, True),
    ("narrow-kline", "/?kline=sh516620&theme=dark", 430, 820, True),
]

# 失效文案黑名单：修完回测记账（隔夜跳空漏乘净值）后，全样本年化从 17% 级降到 12% 级，
# 这些数字在任何页面出现都说明还有一处没改过来。只检"带小数点的整串"，
# 避免误伤 17.4% 之外的正常数字。
# 只放整句短语：裸数字会撞上真实行情（第一版写 "15.8%" 就误报了地产 ETF 权重）。
STALE_TEXTS = [
    "17.7% vs 纯动量 23.9%", "1/σ 加权 22.6%", "Top5 分散仅 15.8%",
    "12.5% → 14.7%", "累计超额仅 +0.9pp", "年化从 12.8% 掉到 6.9%",
    "组合卡玛有 88% 优于纯卫星", "对外口径 11%~13%",
]


def find_browser() -> str:
    for p in BROWSER_CANDIDATES:
        if p and Path(p).exists():
            return p
    raise SystemExit(
        "找不到 Edge/Chrome。用环境变量 UI_SMOKE_BROWSER 指定可执行文件路径。"
    )


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_ready(port: int, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/api/status"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001 —— 服务还没起来，继续等
            time.sleep(0.4)
    return False


def run_browser(browser: str, url: str, w: int, h: int, *,
                shot: Path | None, dump: bool) -> str:
    profile = tempfile.mkdtemp(prefix="uismoke-")
    cmd = [
        browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--no-first-run", "--no-default-browser-check",
        f"--user-data-dir={profile}",
        f"--window-size={w},{h}",
        "--force-device-scale-factor=1",
        "--virtual-time-budget=12000",
    ]
    if dump:
        cmd.append("--dump-dom")
    if shot:
        cmd.append(f"--screenshot={shot}")
    cmd.append(url)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=90,
                             encoding="utf-8", errors="replace")
        return out.stdout or ""
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def parse_probe(dom: str) -> dict | None:
    m = re.search(r"<title>UISMOKE (\{.*?\})</title>", dom, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def check(case, probe: dict | None, expect_kline: bool) -> list[str]:
    name = case
    if probe is None:
        return [f"{name}: 探针没写入 title（页面可能没加载完或 JS 崩了）"]
    bad = []
    if probe.get("errs"):
        bad.append(f"{name}: JS 报错 {probe['errs']}")
    if probe.get("fallback"):
        bad.append(f"{name}: 主探针未执行（boot 崩溃或页面卡死）")
    if probe.get("overflow"):
        bad.append(f"{name}: 横向溢出（scrollWidth {probe.get('sw')} > {probe.get('vw')}）")
    if probe.get("empty"):
        bad.append(f"{name}: 正文为空")
    if probe.get("failed"):
        bad.append(f"{name}: 页面渲染失败 → {probe['failed']}")
    if probe.get("badText"):
        bad.append(f"{name}: 界面上出现 NaN/undefined/Infinity")
    stale = probe.get("stale") or []
    if stale:
        bad.append(f"{name}: 界面残留旧口径数字 {stale}（回测记账已修，这些数已失效）")
    if bool(probe.get("kline")) != expect_kline:
        bad.append(f"{name}: K 线弹窗状态异常（期望 {expect_kline}）")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None, help="只跑这些用例（名称前缀）")
    ap.add_argument("--no-shot", action="store_true", help="不保存截图")
    ap.add_argument("--port", type=int, default=0, help="复用已在跑的服务端口")
    args = ap.parse_args()

    browser = find_browser()
    cases = [c for c in CASES if not args.only or any(c[0].startswith(o) for o in args.only)]
    if not cases:
        print("没有匹配的用例")
        return 2

    proc = None
    port = args.port
    if not port:
        port = free_port()
        print(f"[smoke] 启动服务 :{port}")
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "server.py"), "--port", str(port), "--no-browser"],
            cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    try:
        if not wait_ready(port):
            print("[smoke] 服务没就绪，退出")
            return 2

        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        failures: list[str] = []
        print(f"{'用例':18}{'页面':10}{'宽':>6}  结果")
        print("-" * 62)
        for name, path, w, h, expect_kline in cases:
            # 查询参数必须放在 hash 之前：/#track?uismoke=1 里的 search 是空的
            parts = urlsplit(path)
            query = dict(parse_qsl(parts.query))
            query["uismoke"] = "1"
            url = urlunsplit(("http", f"127.0.0.1:{port}",
                              parts.path or "/", urlencode(query), parts.fragment))
            dom = run_browser(browser, url, w, h, shot=None, dump=True)
            probe = parse_probe(dom)
            bad = check(name, probe, expect_kline)

            if not args.no_shot:
                shot = SHOT_DIR / f"{name}_{w}.png"
                run_browser(browser, url, w, h, shot=shot, dump=False)

            page = (probe or {}).get("page", "?")
            if bad:
                failures.extend(bad)
                print(f"{name:18}{page:10}{w:>6}  FAIL  {bad[0]}")
            else:
                print(f"{name:18}{page:10}{w:>6}  ok")

        print("-" * 62)
        if failures:
            print(f"发现 {len(failures)} 个问题：")
            for f in failures:
                print("  ·", f)
            return 1
        print(f"全部通过（{len(cases)} 个用例）"
              + ("" if args.no_shot else f"；截图在 {SHOT_DIR.relative_to(ROOT)}"))
        return 0
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
