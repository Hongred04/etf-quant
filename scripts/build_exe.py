"""把 Web 控制台打包成单文件 exe（dist/etf-quant.exe）。

定位
----
exe 是**便携只读控制台**：双击即用，无需安装 Python。
- 内置：Python 运行时、pandas/numpy、etfquant 包、web/ 前端
- 内置：data/（只读展示用）——打包时把当前 data 快照一起塞进去，别人才有东西可看
- 不含：scripts/ 数据管道（刷新数据、回测请用完整源码版）

产物去向：dist/etf-quant.exe，与 dist/etf-quant.apk 并排，两者都入库供下载。

用法
----
    .venv/Scripts/python.exe scripts/build_exe.py
    # 前置：pip install pyinstaller（自动检测并提示）
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build_exe"
DIST = ROOT / "dist"
EXE = DIST / "etf-quant.exe"

# 隐私文件：不进 exe（exe 可能被分发，持仓只留在本机 data/）
EXCLUDE_FROM_DATA = {"my_holdings.json"}


def stage_data() -> Path:
    """把 data/ 复制到打包暂存目录，剔除隐私文件。"""
    stage = BUILD / "data_stage"
    if stage.exists():
        shutil.rmtree(stage)
    shutil.copytree(ROOT / "data", stage,
                    ignore=shutil.ignore_patterns("cache", "cache_early",
                                                  *EXCLUDE_FROM_DATA))
    return stage


def sh(cmd: list[str]) -> None:
    print("[cmd]", " ".join(cmd))
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        raise SystemExit(f"[FAIL] 退出码 {r.returncode}: {' '.join(cmd)}")


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ModuleNotFoundError:
        print("[..] 安装 PyInstaller（仅构建期依赖，不进 requirements.txt）")
        sh([sys.executable, "-m", "pip", "install", "pyinstaller", "-q"])

    if not (ROOT / "web" / "index.html").exists():
        raise SystemExit("[FAIL] 缺少 web/index.html")

    if BUILD.exists():
        sh(["cmd", "/c", f"rmdir /s /q {BUILD}"])
    BUILD.mkdir(parents=True)
    data_stage = stage_data()

    sh([
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--name", "etf-quant",
        "--console",                      # 保留控制台：用户能看到地址、Ctrl+C 可停
        "--add-data", f"{ROOT / 'web'};web",
        "--add-data", f"{data_stage};data",   # 只读快照（已剔除持仓等隐私文件）
        "--add-data", f"{ROOT / 'output'};output",
        # pandas 读 CSV（parse_dates）需要这些可选依赖；PyInstaller 静态分析常漏，
        # 漏了会在运行时 ImportError 且被 _csv() 吞掉，表现为"缺少回测产物"。
        "--collect-all", "pandas",
        "--collect-all", "numpy",
        "--hidden-import", "python-dateutil",
        "--hidden-import", "pytz",
        "--hidden-import", "tzdata",
        "--distpath", str(DIST),
        "--workpath", str(BUILD / "work"),
        "--specpath", str(BUILD),
        str(ROOT / "server.py"),
    ])

    size_mb = EXE.stat().st_size / 1048576
    print(f"\n[OK] {EXE}  ({size_mb:.1f} MB)")
    print("     双击启动 Web 控制台；放进项目根目录可接管 data/ 与 output/。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
