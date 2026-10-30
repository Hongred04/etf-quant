"""
把回测产物导出为离线 JavaScript 数据文件，供 Android WebView 直接加载。

为什么不是 JSON + fetch
------------------------
APK 内页面通过 ``file:///android_asset/...`` 加载。现代 WebView 出于同源策略，
会拒绝 ``file://`` 下的 ``fetch()`` 读本地文件。写成 ``window.__DATA__ = {...}``
的 .js 文件可以直接 <script> 引入，零跨域问题，也不需要任何网络权限。

用法
----
python scripts/export_mobile_data.py
python scripts/export_mobile_data.py --out mobile/app/assets/www/data.js --points 200
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server  # noqa: E402  （复用 Web 端接口，保证口径完全一致）


def _clean(obj):
    """NaN / Inf -> None；numpy 标量 -> Python 标量。"""
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, np.floating):
        v = float(obj)
        return v if math.isfinite(v) else None
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "mobile" / "app" / "assets" / "www" / "data.js"))
    ap.add_argument("--points", type=int, default=200)
    args = ap.parse_args()

    # 组装逻辑放在 server.build_mobile_payload 里，导出与在线更新共用同一份代码，
    # 避免"打的包"和"App 拉到的"变成两套口径。
    print("[1/2] 汇总数据（概览 / 信号 / 持仓 / 实验 / 标的池）…")
    payload = server.build_mobile_payload(points=args.points)
    payload = _clean(payload)

    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".js.tmp")
    tmp.write_text(
        "/* 由 scripts/export_mobile_data.py 生成，请勿手改 */\n"
        f"window.__DATA__ = {body};\n",
        encoding="utf-8",
    )
    tmp.replace(out)

    size_kb = out.stat().st_size / 1024
    meta = payload["meta"]
    print(f"[2/2] 已写入 {out}  ({size_kb:.1f} KB)")
    print(f"      数据版本 {meta['data_version']}")
    print(f"      调仓日 {meta['signal_date']} · 执行日 {meta['exec_date']} · "
          f"下次换仓 {meta.get('next_reb')} · "
          f"净值点数 {len(payload['overview']['series'][0]['data'])}")


if __name__ == "__main__":
    main()
