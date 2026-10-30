#!/usr/bin/env python
"""
etf-quant 启动器（交互式控制台）。

由 `启动.bat` 调用，也可直接运行：python launcher.py

设计要点
--------
1. 不假设系统 python 可用：本机 PATH 上的 python 是 Microsoft Store 占位符，
   真正可用的是 .venv 或 WorkBuddy 托管的解释器，由 .bat 探测后传入；
2. 依赖缺失时引导安装，而不是抛 ImportError 崩溃；
3. 所有子流程用当前解释器执行，保证依赖一致；
4. 任何异常都就地捕获并提示，避免双击后窗口闪退。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT / "scripts"
OUT = ROOT / "output"

VERSION = "0.1.0"
RULE = "=" * 68
THIN = "-" * 68


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _init_console() -> None:
    """在 cmd 中启用 UTF-8 输出，避免中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def clear() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def hr(char: str = "-") -> None:
    print(char * 68)


def pause(msg: str = "按回车返回主菜单...") -> None:
    try:
        input(f"\n{msg}")
    except (EOFError, KeyboardInterrupt):
        pass


# --------------------------------------------------------------------------- #
# 环境与状态
# --------------------------------------------------------------------------- #
def check_deps() -> tuple[bool, str]:
    """检查第三方依赖是否就绪。"""
    try:
        import numpy
        import pandas
    except ImportError as exc:
        return False, f"缺少依赖：{exc.name}"
    return True, f"pandas {pandas.__version__} | numpy {numpy.__version__}"


def ensure_deps(interactive: bool = True) -> bool:
    ok, info = check_deps()
    if ok:
        return True
    print(f"\n[!] {info}")
    req = ROOT / "requirements.txt"
    if not req.exists():
        print("[ERR] 未找到 requirements.txt，无法自动安装。")
        return False
    if interactive:
        try:
            ans = input("是否立即安装依赖？(Y/n) ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if ans not in ("", "y", "yes"):
            return False
    print("\n正在安装依赖，请稍候...")
    code = subprocess.call(
        [sys.executable, "-m", "pip", "install", "-r", str(req), "--disable-pip-version-check"],
        cwd=str(ROOT),
    )
    if code != 0:
        print("[ERR] 依赖安装失败，请手动执行：")
        print(f"      {sys.executable} -m pip install -r requirements.txt")
        return False
    ok, info = check_deps()
    print(f"[OK] 依赖就绪：{info}")
    return ok


def collect_status() -> dict:
    """收集项目当前状态，用于首屏展示。"""
    st = {
        "pool": 0,
        "cache": 0,
        "latest": "—",
        "signal_date": "—",
        "signal_n": 0,
        "report": False,
    }
    pool_fp = ROOT / "data" / "etf_pool.csv"
    if pool_fp.exists():
        try:
            with open(pool_fp, encoding="utf-8-sig") as f:
                st["pool"] = max(0, sum(1 for _ in f) - 1)
        except OSError:
            pass

    cache_dir = ROOT / "data" / "cache" / "kline_qfq"
    if cache_dir.exists():
        files = sorted(cache_dir.glob("*.csv"))
        st["cache"] = len(files)

        # 最新交易日直接从缓存内容推断，只读每个文件的末尾 200 字节。
        # 不要用 data/etf_history_inventory.csv —— 那是下载时写的一次性快照，
        # 缓存被清理或回滚后它不会跟着变，会显示成一个已经不存在的"最新日"。
        latest = ""
        for fp in files:
            try:
                with open(fp, "rb") as f:
                    f.seek(0, 2)
                    size = f.tell()
                    f.seek(max(0, size - 200))
                    tail = f.read().decode("utf-8", "ignore").strip().splitlines()
            except OSError:
                continue
            for line in reversed(tail):
                d = line.split(",")[0].strip()
                if len(d) == 10 and d[4] == "-":
                    if d > latest:
                        latest = d
                    break
        if latest:
            st["latest"] = latest

    if OUT.exists():
        sigs = sorted(OUT.glob("signal_*.csv"))
        if sigs:
            st["signal_date"] = sigs[-1].stem.replace("signal_", "")
            try:
                with open(sigs[-1], encoding="utf-8-sig") as f:
                    st["signal_n"] = max(0, sum(1 for _ in f) - 1)
            except OSError:
                pass
        st["report"] = (OUT / "ETF量化策略研究报告.html").exists()
    return st


# --------------------------------------------------------------------------- #
# 执行封装
# --------------------------------------------------------------------------- #
def run_script(script: str, args: list[str] | None = None, title: str = "") -> int:
    """用当前解释器运行 scripts/ 下的脚本，实时输出。"""
    fp = SCRIPTS / script
    if not fp.exists():
        print(f"[ERR] 未找到脚本：{fp}")
        return 1
    if title:
        print(f"\n>>> {title}")
        hr()
        sys.stdout.flush()
    cmd = [sys.executable, str(fp), *(args or [])]
    t0 = time.time()
    try:
        code = subprocess.call(cmd, cwd=str(ROOT))
    except KeyboardInterrupt:
        print("\n[中断] 已取消")
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"[ERR] 执行失败：{exc}")
        return 1
    dur = time.time() - t0
    sys.stdout.flush()
    hr()
    if code == 0:
        print(f"[完成] 耗时 {dur:.1f} 秒")
    else:
        print(f"[失败] 退出码 {code}，耗时 {dur:.1f} 秒")
    return code


def open_path(target: Path) -> None:
    if not target.exists():
        print(f"[ERR] 路径不存在：{target}")
        return
    try:
        os.startfile(str(target))  # type: ignore[attr-defined]
        print(f"[OK] 已打开：{target.name}")
    except Exception as exc:  # noqa: BLE001
        print(f"[ERR] 打开失败：{exc}")


def _disp_width(s: str) -> int:
    """估算终端显示宽度（中日韩字符按 2 计）。"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in s)


def _pad(s: str, width: int, align: str = "left") -> str:
    gap = max(0, width - _disp_width(s))
    if align == "right":
        return " " * gap + s
    return s + " " * gap


def preview_signal(max_rows: int = 8) -> None:
    """读取最新信号文件，格式化为对齐表格。"""
    sigs = sorted(OUT.glob("signal_*.csv")) if OUT.exists() else []
    if not sigs:
        print("\n尚无信号文件，请先执行 [4] 生成今日选股信号。")
        return
    import csv

    print(f"\n>>> 当前信号：{sigs[-1].name}")
    hr()
    try:
        with open(sigs[-1], encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
    except OSError as exc:
        print(f"[ERR] 读取失败：{exc}")
        return
    if not rows:
        return

    head, body = rows[0], rows[1 : 1 + max_rows]
    widths = []
    for i, h in enumerate(head):
        w = max([_disp_width(h)] + [_disp_width(r[i]) for r in body if i < len(r)])
        widths.append(w)

    print("  " + "  ".join(_pad(h, widths[i]) for i, h in enumerate(head)).rstrip())
    print("  " + "-" * min(66, sum(widths) + 2 * (len(widths) - 1)))
    for r in body:
        cells = [
            _pad(c, widths[i], "left" if i <= 2 else "right") for i, c in enumerate(r) if i < len(widths)
        ]
        print("  " + "  ".join(cells).rstrip())


# --------------------------------------------------------------------------- #
# 界面
# --------------------------------------------------------------------------- #
def banner() -> None:
    st = collect_status()
    print(RULE)
    print(f"  ETF 量化选股工具  v{VERSION}")
    print("  趋势闸门自适应轮动 (TGAR) · A 股 ETF 轮动回测")
    print(RULE)
    print(f"  解释器  {sys.version.split()[0]}  ({Path(sys.executable).name})")
    ok, info = check_deps()
    print(f"  依赖    {info if ok else '未安装（选择任意功能将提示安装）'}")
    print(f"  标的池  {st['pool']} 只    日线缓存 {st['cache']} 只    最新交易日 {st['latest']}")
    sig = (
        f"{st['signal_date']}  {st['signal_n']} 只"
        if st["signal_date"] != "—"
        else "尚无"
    )
    print(f"  信号    {sig}")
    print(f"  报告    {'已生成' if st['report'] else '未生成'}")
    print(RULE)


def menu() -> None:
    print()
    print("   1  更新 ETF 全市场列表          约 110 秒 · 需联网")
    print("   2  更新历史行情数据             约 65 秒 · 需联网（有缓存则秒级）")
    print("   3  运行回测（4 个策略）          约 60 秒")
    print("   4  生成今日选股信号             约 15 秒")
    print("   5  重新生成研究报告             约 10 秒")
    print("   6  一键全流程  (1 → 2 → 3 → 4 → 5)")
    print(THIN)
    print("   7  运行单元测试")
    print("   8  打开研究报告")
    print("   9  打开项目文件夹")
    print("   w  打开 Web 可视化控制台   ← 推荐")
    print("   0  退出")
    print(THIN)


def one_click() -> None:
    steps = [
        ("build_universe.py", None, "步骤 1/5  扫描全市场 ETF"),
        ("download_history.py", ["--start", "2018-01-01", "--top", "260"], "步骤 2/5  更新历史行情"),
        ("run_backtest.py", ["--tag", "main"], "步骤 3/5  运行回测"),
        ("run_signal.py", None, "步骤 4/5  生成选股信号"),
        ("make_report.py", None, "步骤 5/5  生成研究报告"),
    ]
    t0 = time.time()
    for script, args, title in steps:
        code = run_script(script, args, title)
        if code != 0:
            print(f"\n[中止] {title} 失败，后续步骤未执行。")
            return
    print(f"\n[全部完成] 总耗时 {time.time() - t0:.1f} 秒")
    print(f"研究报告：{OUT / 'ETF量化策略研究报告.html'}")


def open_web() -> None:
    """在新控制台窗口启动 Web 服务并自动打开浏览器。"""
    print("\n正在启动 Web 可视化控制台...")
    flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
    subprocess.Popen(
        [sys.executable, str(ROOT / "server.py")],
        cwd=str(ROOT), creationflags=flags,
    )
    print("[OK] 服务已在新窗口启动，浏览器将自动打开。")
    print("     关闭那个窗口即可停止服务。")
    time.sleep(1.2)


ACTIONS = {
    "1": lambda: run_script("build_universe.py", None, "扫描全市场 ETF"),
    "2": lambda: run_script(
        "download_history.py", ["--start", "2018-01-01", "--top", "260"], "更新历史行情"
    ),
    "3": lambda: run_script("run_backtest.py", ["--tag", "main"], "运行四策略回测"),
    "4": lambda: run_script("run_signal.py", None, "生成今日选股信号"),
    "5": lambda: run_script("make_report.py", None, "生成研究报告"),
    "6": one_click,
    "7": lambda: run_script("test_runner.py", None, "运行单元测试"),
    "8": lambda: open_path(OUT / "ETF量化策略研究报告.html"),
    "9": lambda: open_path(ROOT),
    "w": open_web,
    "W": open_web,
}


def main() -> int:
    _init_console()
    clear()
    banner()

    while True:
        menu()
        try:
            choice = input("  请选择 [0-9]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if choice in ("0", "q", "Q", "exit"):
            break
        if choice.lower() == "w":
            choice = "w"
        elif choice not in ACTIONS:
            print("  [!] 无效选项，请输入 0-9 或 w")
            continue

        if not ensure_deps():
            pause()
            clear()
            banner()
            continue

        try:
            ACTIONS[choice]()
        except KeyboardInterrupt:
            print("\n[中断]")
        except Exception as exc:  # noqa: BLE001
            print(f"[ERR] {exc}")

        if choice in ("3", "4", "5", "6"):
            preview_signal()

        pause()
        clear()
        banner()

    print("\n再见。\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"\n[启动失败] {exc}")
        input("按回车退出...")
        raise SystemExit(1)
