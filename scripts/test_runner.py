"""
单元测试运行器（供启动器调用）。

优先使用 pytest；若未安装则先尝试自动安装，失败时退回 unittest 直接运行。
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"


def _run_pytest() -> int | None:
    try:
        import pytest
    except ImportError:
        print("[INFO] 未检测到 pytest，正在安装...")
        code = subprocess.call(
            [sys.executable, "-m", "pip", "install", "pytest", "-q", "--disable-pip-version-check"]
        )
        if code != 0:
            return None
        try:
            import pytest  # noqa: F401
        except ImportError:
            return None
    print(f"[INFO] 运行 pytest：{TESTS}")
    return int(pytest.main(["-q", str(TESTS)]))


def _run_unittest() -> int:
    print("[INFO] 退回 unittest 运行")
    loader = unittest.TestLoader()
    suite = loader.discover(str(TESTS), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def main() -> int:
    sys.path.insert(0, str(ROOT))
    if not TESTS.exists():
        print(f"[ERR] 未找到测试目录：{TESTS}")
        return 1
    code = _run_pytest()
    if code is None:
        code = _run_unittest()
    print()
    print("[通过] 全部测试通过" if code == 0 else f"[失败] 存在未通过的用例（退出码 {code}）")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
