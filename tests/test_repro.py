"""复现清单与试验登记簿的测试（离线）。"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import manifest as M  # noqa: E402


def test_manifest_is_deterministic_and_sensitive(tmp_path):
    (tmp_path / "data" / "cache" / "kline_qfq").mkdir(parents=True)
    f = tmp_path / "data" / "cache" / "kline_qfq" / "a.csv"
    f.write_text("date,close\n2020-01-02,1.0\n", encoding="utf-8")
    (tmp_path / "data" / "etf_pool.csv").write_text("ticker\nsh510300\n", encoding="utf-8")
    # output 文件缺失时不应报错
    (tmp_path / "output").mkdir()

    m1 = M.build_manifest(tmp_path)
    m2 = M.build_manifest(tmp_path)
    assert m1["inputs_sha"] == m2["inputs_sha"], "同一份数据必须给同一指纹"

    f.write_text("date,close\n2020-01-02,1.1\n", encoding="utf-8")
    m3 = M.build_manifest(tmp_path)
    assert m3["inputs_sha"] != m1["inputs_sha"], "改一个字节指纹必须变"


def test_trials_register_is_consistent():
    reg = json.loads((ROOT / "data" / "trials.json").read_text(encoding="utf-8"))
    assert reg["grid_variants"] == 29
    assert reg["total"] == sum(int(t["n"]) for t in reg["trials"])
    ids = [t["id"] for t in reg["trials"]]
    assert len(ids) == len(set(ids)), "试验编号不允许重复"


def test_pre_registration_hash_is_frozen():
    doc = ROOT / "docs" / "pre_registration.md"
    fp = ROOT / "data" / "pre_registration.sha256"
    assert doc.exists() and fp.exists(), "预注册文档与其 hash 文件必须成对存在"
    cur = hashlib.sha256(doc.read_bytes()).hexdigest()
    assert fp.read_text(encoding="utf-8").strip() == cur, \
        "预注册文档被改动过，且 hash 文件没有同步——这正是 audit 要拦的事"
