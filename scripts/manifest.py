"""
复现清单（manifest）：把输入与输出的 SHA256 写进 output/manifest.json。

为什么需要它
------------
7.15 的结论之一是"别人无法用手上的东西验证 17.32% 是这份数据算出来的"。
这个脚本把这句话变成一条命令：任何人拿到同一份 `data/` 与 `output/`，
就能核对 hash 是否一致——如果一致，说明数字不是改出来的。

注意它**只做校验，不做证明**：hash 一致只能说明"没改过文件"，
不能说明策略本身有效。后一件事由 DSR / PBO / 前向验证负责。

用法
----
python -m etfquant manifest
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from etfquant.io_utils import atomic_write_text  # noqa: E402

INPUT_FILES = ["data/etf_pool.csv"]
OUTPUT_FILES = ["output/nav_main.csv", "output/perf_main.csv",
                "output/diagnostics.json", "output/overfit_summary.json",
                "output/dsr.json"]


def sha256(fp: Path) -> str:
    return hashlib.sha256(fp.read_bytes()).hexdigest()


def build_manifest(root: Path) -> dict:
    cache = sorted((root / "data" / "cache" / "kline_qfq").glob("*.csv"))
    inputs = {p: sha256(root / p) for p in INPUT_FILES}
    inputs.update({f"data/cache/kline_qfq/{p.name}": sha256(p) for p in cache})
    outputs = {p: sha256(root / p) for p in OUTPUT_FILES if (root / p).exists()}

    # 一个可复制的总指纹：把 (path, hash) 按 path 排序后拼接再 hash
    joined = "\n".join(f"{k} {v}" for k, v in sorted({**inputs, **outputs}.items()))
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "n_cache_files": len(cache),
        "inputs_sha": hashlib.sha256(joined.encode("utf-8")).hexdigest(),
        "inputs": inputs,
        "outputs": outputs,
    }


def main() -> int:
    m = build_manifest(ROOT)
    atomic_write_text(ROOT / "output" / "manifest.json",
                      json.dumps(m, ensure_ascii=False, indent=2))
    print(f"[OK] 已写 output/manifest.json：{m['n_cache_files']} 个缓存文件 + "
          f"{len(m['inputs'])} 个输入 + {len(m['outputs'])} 个输出")
    print(f"     总指纹 {m['inputs_sha'][:16]}…（任何人用同一份数据能复现同一指纹）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
