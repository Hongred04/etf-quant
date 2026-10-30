"""
文件读写小工具。

存在的理由
----------
所有 ``to_csv`` / ``write_text`` 直接覆盖原文件时，一旦进程中途被杀（Ctrl+C、
任务超时、磁盘满），会留下**半个文件**。下一个读取方（回测、报告、Web 接口）
拿到的就是损坏数据，而且报错位置与真正原因相隔很远。

``atomic_*`` 先写同目录的临时文件再 ``os.replace`` 原子替换，
读取方要么看到旧文件、要么看到完整新文件，不存在中间态。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pandas as pd


def atomic_write_text(fp: str | Path, text: str, encoding: str = "utf-8") -> Path:
    """原子写文本文件。"""
    fp = Path(fp)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(fp.parent), prefix=f".{fp.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            f.write(text)
        os.replace(tmp, fp)
    except BaseException:
        # 失败时清掉临时文件，避免目录里堆积 .tmp
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return fp


def atomic_to_csv(df: pd.DataFrame, fp: str | Path, **kw) -> Path:
    """原子写 CSV。``kw`` 透传给 ``DataFrame.to_csv``。"""
    fp = Path(fp)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(fp.parent), prefix=f".{fp.name}.", suffix=".tmp")
    os.close(fd)  # 让 pandas 自己按路径打开，避免 Windows 上句柄冲突
    try:
        df.to_csv(tmp, **kw)
        os.replace(tmp, fp)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return fp
