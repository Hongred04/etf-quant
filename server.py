#!/usr/bin/env python
"""
etf-quant Web 控制台（零额外依赖，仅用标准库 + pandas）。

用法
----
    python server.py            # 启动并自动打开浏览器
    python server.py --port 9000 --no-browser

设计说明
--------
* 后端只做三件事：读既有 CSV 产物 → 输出 JSON、按需执行 scripts/ 下的任务、静态托管 web/
* 任务执行放在后台线程，前端轮询日志，避免 110 秒的扫描任务把请求阻塞
* 所有接口返回 UTF-8 JSON，前端不依赖任何 CDN，离线可用
"""

from __future__ import annotations

import argparse
import json
import math
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

try:
    import numpy as np
    import pandas as pd
except ModuleNotFoundError as exc:  # noqa: PERF203
    # 直接抛 traceback 的话，双击 bat 的窗口会一闪而过，用户只知道"打不开"。
    # 这里把原因和两种修复方式说清楚，然后正常退出（退出码 2）。
    _missing = getattr(exc, "name", "") or "numpy / pandas"
    sys.stderr.write(
        "\n[缺少依赖] 当前 Python 环境没有安装 %s\n"
        "  解释器：%s\n\n"
        "修复方式（任选其一）：\n"
        "  1) 双击项目里的「打开界面.bat」，它会自动补齐依赖并启动\n"
        "  2) 手动执行：\n"
        "     \"%s\" -m pip install -r requirements.txt\n\n"
        % (_missing, sys.executable, sys.executable)
    )
    sys.exit(2)

from etfquant import holdings as H
from etfquant import metrics as M

if getattr(sys, "frozen", False):
    # PyInstaller 打包版（dist/etf-quant.exe）：
    # - 只读资源（web/ 前端）在解压临时目录（_MEIPASS）
    # - 数据目录优先取 exe 旁边（把 exe 放进项目根目录 = 接管全套数据）；
    #   exe 单独运行时回退到内置的 data/output 只读快照，保证开箱有东西可看。
    ROOT = Path(sys.executable).resolve().parent
    _MP = Path(getattr(sys, "_MEIPASS", ROOT))
    WEB = _MP / "web"
    OUT = (ROOT / "output") if (ROOT / "output").exists() else (_MP / "output")
    DATA = (ROOT / "data") if (ROOT / "data").exists() else (_MP / "data")
    FROZEN = True
else:
    ROOT = Path(__file__).resolve().parent
    WEB = ROOT / "web"
    OUT = ROOT / "output"
    DATA = ROOT / "data"
    FROZEN = False
SCRIPTS = ROOT / "scripts"

SELF = "S4-TGAR(自研)"
BENCH = "基准-沪深300"
MAX_POINTS = 460
CURRENT_PORT = 8800                 # 与量价选股（8777）错开，两个可以同时开

# --------------------------------------------------------------------------- #
# 任务管理
# --------------------------------------------------------------------------- #
ACTIONS: dict[str, dict] = {
    "universe": {"script": "build_universe.py", "args": [], "label": "扫描全市场 ETF",
                 "seconds": 110, "timeout": 900},
    "download": {"script": "download_history.py", "args": ["--start", "2018-01-01", "--top", "260"],
                 "label": "更新历史行情", "seconds": 70, "timeout": 1800},
    "backtest": {"script": "run_backtest.py", "args": ["--tag", "main"], "label": "运行四策略回测",
                 "seconds": 60, "timeout": 900},
    "signal": {"script": "run_signal.py", "args": [], "label": "生成选股信号",
               "seconds": 15, "timeout": 300},
    # 组合任务：行情 → 回测 → 信号。顺序不能换：回测依赖新行情，信号依赖新回测。
    "refresh": {"steps": ["download", "backtest", "signal"], "label": "更新行情并重算",
                "seconds": 150, "timeout": 2400},
    "report": {"script": "make_report.py", "args": [], "label": "生成研究报告",
               "seconds": 12, "timeout": 300},
    "test": {"script": "test_runner.py", "args": [], "label": "运行单元测试",
             "seconds": 5, "timeout": 300},
}

# 同一时刻只允许一个任务在跑。
# 不同脚本读写的是同一批 CSV（nav_main.csv / weights_*.csv / perf_main.csv），
# 并发执行会让"生成信号"读到"运行回测"写了一半的中间态。
_RUN_LOCK = threading.Lock()
MAX_TASKS_KEPT = 50

TASKS: dict[str, dict] = {}
TASK_LOCK = threading.Lock()
CACHE: dict[str, tuple[float, object]] = {}
CACHE_LOCK = threading.Lock()
# 实盘跟踪的落盘缓存 + 构建锁（冷启动约 7 秒，必须避免重复计算）
TRACK_CACHE = OUT / "track_cache.json"
TRACK_LOCK = threading.Lock()


def _clean_json(obj):
    """
    把 NaN / ±Inf 换成 None。

    ``json.dumps`` 默认 allow_nan=True，会把它们写成字面量 NaN / Infinity，
    这不是合法 JSON，浏览器 ``JSON.parse`` 直接抛 SyntaxError —— 整个页面白屏，
    而且报错信息与真实原因（某个指标缺失）完全对不上。
    """
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
        return {k: _clean_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean_json(v) for v in obj]
    return obj


def _cached(key: str, ttl: float, fn):
    now = time.time()
    with CACHE_LOCK:
        hit = CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with CACHE_LOCK:
        CACHE[key] = (now, val)
    return val


def invalidate_cache() -> None:
    with CACHE_LOCK:
        CACHE.clear()
    # 数据已变，实盘跟踪的落盘缓存也失效了（指纹对不上）。
    # 在后台重算一次：等用户点开那一页时已经是热的，而不是当场等 7 秒。
    threading.Thread(target=_warm_caches, daemon=True, name="warm-after-update").start()


def start_task(action: str) -> str:
    spec = ACTIONS[action]
    # 组合任务：按顺序跑多个脚本（更新行情 → 重跑回测 → 重算信号）。
    # 单步任务统一看成只有一步的列表，后面只有一条执行路径。
    steps = spec.get("steps") or [action]
    if not _RUN_LOCK.acquire(blocking=False):
        raise RuntimeError("已有任务正在执行，请等它结束再发起新任务")
    tid = uuid.uuid4().hex[:12]
    with TASK_LOCK:
        TASKS[tid] = {
            "id": tid,
            "action": action,
            "label": spec["label"],
            "status": "running",
            "log": [],
            "started": time.time(),
            "duration": None,
            "code": None,
            "step": 0,
            "total_steps": len(steps),
        }

    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}

    def run_one(step_spec: dict, tag: str) -> int:
        """跑一个脚本，把输出逐行写进任务日志。返回退出码。"""
        if FROZEN:
            # exe 版没有内嵌 Python 解释器可供子进程调用，也不带 scripts/ 源码；
            # 数据管道请用完整源码版（README「快速开始」）。
            with TASK_LOCK:
                TASKS[tid]["log"].append(
                    f"{tag}[exe 版] 这是便携只读控制台，不含数据管道。"
                    "刷新数据 / 回测请下载完整源码版后运行 python scripts/ 下对应脚本。")
            TASKS[tid]["code"] = 2
            TASKS[tid]["status"] = "error"
            TASKS[tid]["duration"] = round(time.time() - TASKS[tid]["started"], 1)
            return 2
        cmd = [sys.executable, str(SCRIPTS / step_spec["script"]), *step_spec["args"]]
        timed_out = threading.Event()

        def _kill() -> None:
            timed_out.set()
            try:
                if proc is not None:
                    proc.kill()
            except Exception:  # noqa: BLE001
                pass

        proc = None
        try:
            # Windows 下子进程 stdout 默认按 GBK 编码，若父进程按 UTF-8 解码会变乱码，
            # 因此显式指定 PYTHONIOENCODING，并关闭缓冲以便日志实时滚动。
            proc = subprocess.Popen(
                cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
            )
            # 看护线程：子进程若无输出（网络卡死、等待 stdin），主循环会一直阻塞在
            # for line in proc.stdout，只能靠外部 kill 才能退出。
            watchdog = threading.Timer(step_spec.get("timeout", 900), _kill)
            watchdog.daemon = True
            watchdog.start()
            assert proc.stdout is not None
            try:
                for line in proc.stdout:
                    with TASK_LOCK:
                        TASKS[tid]["log"].append(f"{tag}{line.rstrip()}")
                proc.wait()
            finally:
                watchdog.cancel()
            if timed_out.is_set():
                with TASK_LOCK:
                    TASKS[tid]["log"].append(
                        f"[ERROR] {tag}超过 {step_spec.get('timeout', 900)} 秒未完成，已强制终止"
                    )
                return 124
            return proc.returncode
        except Exception as exc:  # noqa: BLE001
            with TASK_LOCK:
                TASKS[tid]["log"].append(f"[ERROR] {tag}{exc}")
            return 1

    def worker() -> None:
        code = 0
        try:
            for i, step in enumerate(steps, 1):
                step_spec = ACTIONS[step]
                tag = f"[{i}/{len(steps)}] " if len(steps) > 1 else ""
                with TASK_LOCK:
                    TASKS[tid]["step"] = i
                    if tag:
                        TASKS[tid]["log"].append(f"{tag}开始：{step_spec['label']}")
                code = run_one(step_spec, tag)
                if code != 0:
                    with TASK_LOCK:
                        TASKS[tid]["log"].append(f"{tag}失败（退出码 {code}），后续步骤已跳过")
                    break
        finally:
            with TASK_LOCK:
                TASKS[tid]["code"] = code
                TASKS[tid]["status"] = "done" if code == 0 else "error"
                TASKS[tid]["duration"] = round(time.time() - TASKS[tid]["started"], 1)
                _prune_tasks()
            invalidate_cache()
            _RUN_LOCK.release()

    threading.Thread(target=worker, daemon=True).start()
    return tid


def _prune_tasks() -> None:
    """只保留最近 MAX_TASKS_KEPT 条已完成任务，防止常驻服务内存无限增长。"""
    if len(TASKS) <= MAX_TASKS_KEPT:
        return
    done = sorted(
        (t for t in TASKS.items() if t[1]["status"] != "running"),
        key=lambda kv: kv[1]["started"],
    )
    for tid, _ in done[: max(0, len(TASKS) - MAX_TASKS_KEPT)]:
        TASKS.pop(tid, None)


# --------------------------------------------------------------------------- #
# 数据接口
# --------------------------------------------------------------------------- #
def _csv(fp: Path, **kw):
    if not fp.exists():
        return None
    try:
        return pd.read_csv(fp, **kw)
    except Exception:  # noqa: BLE001
        return None


def _downsample(n: int, target: int = MAX_POINTS) -> int:
    return max(1, n // target) if n > target else 1


def api_debug_paths() -> dict:
    """诊断端点：exe 排查"数据为什么没显示"用。只暴露路径与存在性，不含数据内容。"""
    probe = {}
    for name, fp in (("nav", OUT / "nav_main.csv"), ("perf", OUT / "perf_main.csv")):
        try:
            df = pd.read_csv(fp, index_col=0, parse_dates=True)
            probe[name] = f"read OK, {len(df)} rows"
        except FileNotFoundError:
            probe[name] = "file missing"
        except Exception as exc:  # noqa: BLE001
            probe[name] = f"read FAILED: {type(exc).__name__}: {exc}"
    return {
        "frozen": FROZEN,
        "root": str(ROOT),
        "web": str(WEB),
        "out": str(OUT),
        "data": str(DATA),
        "probe": probe,
    }


def api_overview() -> dict:
    nav = _csv(OUT / "nav_main.csv", index_col=0, parse_dates=True)
    perf = _csv(OUT / "perf_main.csv", index_col=0)
    if nav is None or perf is None:
        return {"error": "缺少回测产物，请先运行一次回测"}

    step = _downsample(len(nav))
    sub = nav.iloc[::step]
    if sub.index[-1] != nav.index[-1]:
        sub = pd.concat([sub, nav.iloc[[-1]]])

    series = []
    for col in nav.columns:
        s = sub[col].dropna()
        series.append({
            "name": col,
            "data": [[d.strftime("%Y-%m-%d"), round(float(v), 4)] for d, v in s.items()],
        })

    # 回撤
    def dd(s: pd.Series) -> list:
        peak = s.cummax()
        d = (s / peak - 1.0) * 100
        return [[k.strftime("%Y-%m-%d"), round(float(v), 2)] for k, v in d.items()]

    nav_self = nav[SELF].dropna()
    nav_bench = nav[BENCH].dropna() if BENCH in nav.columns else None

    dd_self = dd(nav_self.iloc[::step] if step > 1 else nav_self)
    dd_bench = dd(nav_bench.iloc[::step]) if nav_bench is not None and step > 1 else (
        dd(nav_bench) if nav_bench is not None else [])

    # 年度收益
    def annual(s: pd.Series) -> dict:
        r = s.pct_change(fill_method=None).dropna()
        a = (1 + r).resample("YE").prod() - 1
        return {str(i.year): round(float(v) * 100, 2) for i, v in a.items()}

    annuals = {c: annual(nav[c].dropna()) for c in nav.columns}

    perf_rows = []
    for _, row in perf.iterrows():
        perf_rows.append({
            "name": row.get("策略", ""),
            "cls": row.name,
            "cum": float(row["累计收益"]),
            "cagr": float(row["年化收益(CAGR)"]),
            "vol": float(row["年化波动"]),
            "sharpe": float(row["夏普比率"]),
            "mdd": float(row["最大回撤"]),
            "calmar": float(row["卡玛比率"]),
            "excess": float(row.get("超额收益", 0)),
            "beta": float(row.get("Beta", 0)),
            "ir": float(row.get("信息比率", 0)),
            "turnover": float(row.get("年均换手(单边)", 0)),
        })

    # 基准单独算一行：前端 KPI 需要用它做「基准 X%」的对照，缺失会显示成 0
    if nav_bench is not None and len(nav_bench) > 20:
        bs = M.summarize(nav_bench.reindex(nav.index).ffill())
        perf_rows.append({
            "name": BENCH,
            "cls": "Benchmark",
            "cum": float(bs["累计收益"]),
            "cagr": float(bs["年化收益(CAGR)"]),
            "vol": float(bs["年化波动"]),
            "sharpe": float(bs["夏普比率"]),
            "mdd": float(bs["最大回撤"]),
            "calmar": float(bs["卡玛比率"]),
            "excess": 0.0,
            "beta": 1.0,
            "ir": 0.0,
            "turnover": 0.0,
        })

    pool = _csv(DATA / "etf_pool.csv")
    cache_dir = DATA / "cache" / "kline_qfq"
    n_cache = len(list(cache_dir.glob("*.csv"))) if cache_dir.exists() else 0

    self_row = next((r for r in perf_rows if r["name"] == SELF), None)
    expect = _expectancy(float(self_row["cagr"]) if self_row else None)

    return {
        "meta": {
            "start": nav.index[0].strftime("%Y-%m-%d"),
            "end": nav.index[-1].strftime("%Y-%m-%d"),
            "days": int(len(nav)),
            "years": round(M.years_elapsed(nav), 1),
            "pool": int(len(pool)) if pool is not None else 0,
            "cache": n_cache,
            "cost": 0.001,
            "self": SELF,
            "bench": BENCH,
        },
        "series": series,
        "drawdown": {"self": dd_self, "bench": dd_bench},
        "annual": annuals,
        "perf": perf_rows,
        "expect": expect,
        # 水下时长：回答"多久没回本"，与 drawdown（跌多深）互补。
        # 回撤 20% 拖三年比回撤 30% 六个月恢复更难受，但 MDD 看不出这件事。
        "underwater": {
            "self": M.underwater_stats(nav_self),
            "bench": M.underwater_stats(nav_bench) if nav_bench is not None else None,
        },
    }


def _perf_row(name: str = SELF) -> pd.Series | None:
    """按「策略」列取绩效行。

    注意不能写 perf.loc[SELF]——perf_main.csv 的索引是策略类名
    （TrendGatedAdaptive 等），中文策略名在「策略」列里。早期在这里查错过，
    结果条件永远为假、分支被静默跳过、成本反推从未生效。
    """
    perf = _csv(OUT / "perf_main.csv", index_col=0)
    if perf is None or "策略" not in perf.columns:
        return None
    hit = perf[perf["策略"] == name]
    return None if hit.empty else hit.iloc[0]


def _next_trading_day(d: pd.Timestamp) -> pd.Timestamp:
    """下一个工作日（未考虑法定节假日，仅跳过周末）。"""
    nxt = d + pd.Timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += pd.Timedelta(days=1)
    return nxt


def _position_split(rows: list[dict]) -> dict:
    """
    把信号行拆成「权益 / 债券 / 现金」三块，合计 100%。

    口径说明（这是最容易引起误解的地方）：
    策略里的「半仓」指的是**权益占一半**，另一半买入债券 ETF，账户始终满仓。
    用户看到下单表 100% 全买满，会以为"说好的半仓呢"，所以这个拆分必须由后端
    统一给出，前端只做展示——两端各算一次必然有一天对不上。
    """
    eq_w = sum(float(r.get("weight") or 0) for r in rows if r.get("category") != "债券")
    bond_w = sum(float(r.get("weight") or 0) for r in rows if r.get("category") == "债券")
    eq_r, bd_r = round(eq_w, 4), round(bond_w, 4)
    # 现金用**已舍入**的两块去减，保证三者相加正好 1；
    # 信号 CSV 的权重本身保留 4 位（1/6 存成 0.1667），所以权益常常是 50.01%，
    # 三块合计可能略微超过 100%，这是源数据的舍入，不是算错。
    cash_r = round(max(0.0, 1.0 - eq_r - bd_r), 4)
    return {
        "equity": eq_r,
        "bond": bd_r,
        "cash": cash_r,
        "basis": "权益 + 债券 + 现金 = 100%；「半仓」指权益 50%，账户仍是满仓，不留现金",
    }


def _signal_status(today, exec_date, next_reb) -> str:
    """
    清单当前处于哪种状态。抽成纯函数是为了能单测——这段判定直接决定
    "今天该不该动手"，写错了用户要么白跑一趟，要么错过换仓。

    pending     还没到买入日（今天收盘后才换，明天开盘买）
    exec_today  今天就是买入日
    holding     买入之后、下一个调仓日之前：不需要操作
    today       今天就是调仓日：收盘后更新数据，明天开盘换
    due         调仓日已过还没重算：清单作废，必须更新
    """
    if today < exec_date:
        return "pending"
    if today == exec_date:
        return "exec_today"
    if next_reb is None:
        return "holding"
    if today > next_reb:
        return "due"
    if today == next_reb:
        return "today"
    return "holding"


def _remind_of(status: str, reb_date: pd.Timestamp, exec_date: pd.Timestamp,
               next_reb: pd.Timestamp | None) -> dict:
    """调仓日提醒的内容：只在这两天提醒，其余时间返回空，避免变成骚扰。"""
    wd = "一二三四五六日"
    if status == "exec_today":
        return {
            "should": True, "kind": "exec",
            "title": "今天开盘要换仓",
            "body": f"按 {reb_date.date()} 收盘定的清单执行（{exec_date.date()} 周{wd[exec_date.weekday()]} 开盘）。",
        }
    if status == "today":
        # 决策日当天不写具体"明天几号"：新一期的执行日 = 下一个交易日，
        # 用旧的 exec_date 会指到本期，用日历推算又会被节假日带偏。
        return {
            "should": True, "kind": "decision",
            "title": "今天是调仓日",
            "body": "收盘后更新行情重算，明天开盘按新清单换仓。",
        }
    return {"should": False, "kind": "", "title": "", "body": ""}


def api_signal() -> dict:
    meta_fp = OUT / "signal_meta.json"
    meta = {}
    if meta_fp.exists():
        try:
            meta = json.loads(meta_fp.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            meta = {}

    sigs = sorted(OUT.glob("signal_*.csv"))
    if not sigs:
        return {"error": "尚未生成信号，请先执行一次「生成信号」"}
    # 优先按 meta 记的调仓日取文件：目录里可能残留旧口径的按日文件
    # （signal_2026-10-28.csv 之类），按文件名排序会把它们当成最新一期。
    fp = OUT / f"signal_{meta.get('rebalance_date')}.csv"
    df = _csv(fp if fp.exists() else sigs[-1], dtype={"代码": str})
    rows = []
    if df is not None:
        for _, r in df.iterrows():
            rows.append({
                "code": str(r["代码"]),
                "name": str(r["名称"]),
                "category": str(r["类别"]),
                "weight": float(r["目标权重"]),
                "price": float(r["最新价"]),
                "r20": float(r["R20%"]) if pd.notna(r["R20%"]) else None,
                "r60": float(r["R60%"]) if pd.notna(r["R60%"]) else None,
                "vol": float(r["年化波动%"]) if pd.notna(r["年化波动%"]) else None,
                "above60": bool(r["MA60之上"]),
                "price_date": str(r.get("价格日期", "")) if "价格日期" in r else "",
                "price_stale": bool(r.get("价格陈旧", False)) if "价格陈旧" in r else False,
            })
    # 「调仓日」「数据截止日」是两个日期，不能混用：
    # 策略每 N 个交易日才换一次仓，行情却每天都有。旧口径把数据日当信号日，
    # 于是连续三天运行给出三份不同持仓（10-26 地产 / 10-27 红利低波 / 10-28 纳指），
    # 照做等于每天换仓。现在清单钉在最近一个调仓日，中间几天内容不变。
    reb_date = pd.Timestamp(meta.get("rebalance_date") or sigs[-1].stem.replace("signal_", ""))
    data_date = pd.Timestamp(meta.get("data_date") or reb_date)
    today = pd.Timestamp.now().normalize()
    exec_date = pd.Timestamp(meta["exec_date"]) if meta.get("exec_date") else _next_trading_day(reb_date)
    next_reb = pd.Timestamp(meta["next_rebalance"]) if meta.get("next_rebalance") else None
    status = _signal_status(today, exec_date, next_reb)
    remind = _remind_of(status, reb_date, exec_date, next_reb)
    exec_past = today > exec_date
    age_days = int((today - data_date).days)

    # 给每只标的补齐下单所需信息
    for r in rows:
        r["market"] = "沪市" if r["code"].startswith("sh") else "深市"
        r["lot_cost"] = round(r["price"] * 100, 2)      # 一手 = 100 份
        r["share"] = round(r["weight"] / sum(x["weight"] for x in rows), 4) if rows else 0.0

    # 单次调仓成本：用回测里的真实换手反推，不写死
    per_reb_cost, avg_turnover = 0.0, 0.0
    _row = _perf_row(SELF)
    if _row is not None:
        n_reb = float(_row.get("调仓次数", 0) or 0)
        avg_turnover = float(_row.get("年均换手(单边)", 0) or 0)
        # 每年调仓次数 = 调仓次数 / 回测年数，年数按净值文件的实际交易日 / 252。
        # 这里原来写死 1892（当时的主回测行数），一旦回测区间变化，
        # per_reb_cost 会静默算错且没有任何报错——必须从数据反推。
        _nav = _csv(OUT / "nav_main.csv", index_col=0, parse_dates=True)
        _n_days = len(_nav) if _nav is not None else 0
        _years = _n_days / 252.0 if _n_days > 0 else 0.0
        if n_reb > 0 and avg_turnover > 0 and _years > 0:
            per_reb_cost = (avg_turnover / (n_reb / _years)) * 2 * 0.001

    # 仓位结构：权益 / 债券 / 现金三块，合计 100%。
    # 策略里的「半仓」指的是**权益占一半**，另一半买债券，账户始终满仓、不留现金。
    # 这个数字以前只在前端用一句"风险资产仓位 50%"表达，用户看到资金表 100% 全买满，
    # 就会以为"说好半仓怎么买满了"。所以后端把三块拆开返回，前端只负责展示。
    pos = _position_split(rows)

    return {
        "date": data_date.strftime("%Y-%m-%d"),
        "today": today.strftime("%Y-%m-%d"),
        "position": pos,
        "per_reb_cost": round(per_reb_cost, 6),
        "avg_turnover": round(avg_turnover, 2),
        "stale_prices": meta.get("stale_price_names", []),
        "exec_date": exec_date.strftime("%Y-%m-%d"),
        "exec_weekday": "一二三四五六日"[exec_date.weekday()],
        "age_days": age_days,
        "exec_past": bool(exec_past),
        "is_weekend": today.weekday() >= 5,
        # 过期判定改为「是否已过下一个调仓日」：清单在两次调仓之间本来就该保持不变，
        # 用"数据日距今几天"当过期标准会把正常的持有期误判成过期。
        "stale": bool(status == "due"),
        "reb_date": reb_date.strftime("%Y-%m-%d"),
        "reb_weekday": "一二三四五六日"[reb_date.weekday()],
        "next_reb": next_reb.strftime("%Y-%m-%d") if next_reb is not None else None,
        "next_weekday": "一二三四五六日"[next_reb.weekday()] if next_reb is not None else None,
        "next_est": bool(meta.get("next_estimated", False)),
        "reb_freq": int(meta.get("rebalance_freq") or 5),
        "days_to_next": meta.get("days_to_next"),
        "held_days": meta.get("held_days"),
        "status": status,
        "remind": remind,
        "rows": rows,
        "regime": meta.get("regime"),
        "exposure": meta.get("exposure"),
        "equity_weight": meta.get("equity_weight", sum(r["weight"] for r in rows
                                                        if r["category"] not in ("债券",))),
        "bond_weight": meta.get("bond_weight", sum(r["weight"] for r in rows
                                                   if r["category"] == "债券")),
        "note": meta.get("note", ""),
    }


def api_research() -> dict:
    def table(fp: Path) -> dict | None:
        df = _csv(fp, index_col=0)
        if df is None:
            return None
        return {
            "index": [str(i) for i in df.index],
            "columns": [str(c) for c in df.columns],
            "rows": [[None if pd.isna(v) else (float(v) if isinstance(v, (int, float)) else str(v))
                      for v in df.loc[i].tolist()] for i in df.index],
        }

    return {
        "ladder": table(OUT / "ladder.csv"),
        "ablation": table(OUT / "ablation.csv"),
        "subsample": table(OUT / "subsample2.csv"),
        "overfit": _overfit_payload(),
    }


def _expectancy(sample_cagr: float | None = None) -> dict | None:
    """
    对外口径（预期区间）。样本内年化由调用方给，锚点来自 overfit_summary.json。

    为什么由服务端统一算：口径一旦在四个地方各写一遍（README / 桌面 / 手机 / 报告），
    改一次要改四处，漏一处就是"页面 17.4%、报告 12.6%"式的自相矛盾。
    """
    from etfquant.expectation import load

    exp = load(OUT / "overfit_summary.json", sample_cagr)
    if not exp:
        return exp

    # 把 7.10 的归因结果挂上去：光有"预期 11%~13%"是不够的——
    # 那个区间建立在 alpha 之上，而 alpha 的 t 值只有 1.38。
    # 界面不写这一句，就等于让页面比 README 乐观。
    fp = OUT / "diagnostics.json"
    if fp.exists():
        try:
            doc = json.loads(fp.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError):
            doc = {}
        reg = doc.get("regression") or {}
        if reg.get("alpha_ann") is not None:
            exp["alpha_ann"] = float(reg["alpha_ann"])
            if reg.get("alpha_t") is not None:
                exp["alpha_t"] = float(reg["alpha_t"])
            exp["frozen_at"] = (doc.get("meta") or {}).get("frozen_at")
    return exp


def _overfit_payload() -> dict | None:
    """过拟合体检结果（scripts/overfit_check.py 生成）。缺文件就返回 None，前端不显示该卡。"""
    fp = OUT / "overfit_summary.json"
    if not fp.exists():
        return None
    try:
        summary = json.loads(fp.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    row = _perf_row()
    sample = float(row["年化收益(CAGR)"]) if row is not None else None
    expect = _expectancy(sample)
    if expect:
        summary["expectation"] = expect
    walk = _csv(OUT / "overfit_walk.csv")
    rows = []
    if walk is not None and not walk.empty:
        for _, r in walk.head(12).iterrows():
            rows.append({
                "oos_start": str(r.get("oos_start", ""))[:10],
                "picked": str(r.get("picked", "")),
                "is_sharpe": float(r["is_sharpe"]) if pd.notna(r.get("is_sharpe")) else None,
                "oos_sharpe": float(r["oos_sharpe"]) if pd.notna(r.get("oos_sharpe")) else None,
                "oos_ret": float(r["oos_ret"]) if pd.notna(r.get("oos_ret")) else None,
            })
    summary["walk"] = rows
    return summary


# --------------------------------------------------------------------------- #
# 实盘持仓
# --------------------------------------------------------------------------- #
HOLD_FILE = DATA / "my_holdings.json"


def _pool_meta() -> dict[str, dict]:
    """代码 → {name, category}，用于给录入的持仓补名称与分类。"""
    pool = _csv(DATA / "etf_pool.csv", dtype={"ticker": str})
    if pool is None or pool.empty:
        return {}
    out = {}
    for _, r in pool.iterrows():
        out[str(r["ticker"])] = {
            "name": str(r.get("name") or r["ticker"]),
            "category": str(r.get("category") or "主题"),
        }
    return out


def _last_bar(code: str) -> tuple[float, float, str]:
    """缓存日线末尾两根：返回 (最后收盘, 倒数第二收盘, 日期)。

    注意列顺序是 date,open,close,high,low,volume —— 收盘价是第 3 列（下标 2）。
    早期这里取 parts[1]，拿到的是**开盘价**，对池外标的估值会整体偏低或偏高，
    而且完全不会报错。
    """
    fp = DATA / "cache" / "kline_qfq" / f"{code}.csv"
    if not fp.exists():
        return 0.0, 0.0, ""
    try:
        # 只读末尾 512 字节，避免为了两个价格把整张表读进来
        with open(fp, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 512))
            tail = f.read().decode("utf-8", "ignore").strip().splitlines()
    except OSError:
        return 0.0, 0.0, ""
    rows = []
    for line in reversed(tail):
        parts = line.split(",")
        if len(parts) >= 3 and len(parts[0]) == 10 and parts[0][4] == "-":
            try:
                rows.append((float(parts[2]), parts[0]))
            except ValueError:
                continue
        if len(rows) >= 2:
            break
    if not rows:
        return 0.0, 0.0, ""
    px, d = rows[0]
    prev = rows[1][0] if len(rows) > 1 else 0.0
    return px, prev, d


_LIVE_LOCK = threading.Lock()
_LIVE_CACHE: dict[str, tuple[float, dict]] = {}
_LIVE_TTL = 20.0


def _live_quotes(codes: list[str]) -> dict[str, dict]:
    """
    实时快照（腾讯），失败返回空字典由调用方回落到收盘价。

    为什么持仓估值要单开一条实时通道：data/etf_pool.csv 是「上次跑数据更新」
    时的快照，可能已经是几天前的收盘价。持仓页要做的是「我现在赚了多少」，
    拿上周的收盘价算会让人以为程序坏了。

    超时收紧到 4 秒 × 1 次：这是页面同步接口，不能像后台任务那样重试 45 秒。
    """
    if not codes:
        return {}
    now = time.time()
    with _LIVE_LOCK:
        hit = _LIVE_CACHE.get("q")
        if hit and now - hit[0] < _LIVE_TTL:
            cached = hit[1]
            return {c: cached[c] for c in codes if c in cached}
    try:
        from etfquant import datafeed as DF
        df = DF.fetch_snapshot(list(codes), timeout=4, retries=1)
    except Exception:  # noqa: BLE001 —— 断网/接口变更都不该让持仓页报错
        df = None

    out: dict[str, dict] = {}
    if df is not None and not df.empty:
        today = date.today().isoformat()
        for _, r in df.iterrows():
            try:
                px = float(r["price"])
                prev = float(r["prev_close"])
            except (TypeError, ValueError):
                continue
            if px <= 0:
                continue
            qt = str(r.get("quote_time") or "")
            qdate = f"{qt[0:4]}-{qt[4:6]}-{qt[6:8]}" if len(qt) >= 8 else ""
            out[str(r["ticker"])] = {
                "name": str(r["name"]),
                "price": px,
                "prev": prev if prev > 0 else 0.0,
                "date": qdate,
                "live": qdate == today,
            }
    with _LIVE_LOCK:
        _LIVE_CACHE["q"] = (now, out)
    return {c: out[c] for c in codes if c in out}


def _quotes(codes: list[str]) -> dict[str, dict]:
    """
    构造持仓估值所需的行情。三级回落，谁新用谁：
    * 实时快照（腾讯，20 秒缓存）—— 盘中能看到当日盈亏
    * data/etf_pool.csv —— 上次数据更新时的收盘快照，含昨收
    * data/cache/kline_qfq —— 历史日线，给池外/新上市标的兜底

    不能只用池子：用户可能持有池外的 ETF（成交额不够没进初筛池）；
    也不能只用缓存：缓存是收盘数据，盘中看不到当日盈亏。
    """
    pool = _csv(DATA / "etf_pool.csv", dtype={"ticker": str})
    snap: dict[str, dict] = {}
    if pool is not None and not pool.empty:
        for _, r in pool.iterrows():
            code = str(r["ticker"])
            qt = str(r.get("quote_time") or "")
            qdate = f"{qt[0:4]}-{qt[4:6]}-{qt[6:8]}" if len(qt) >= 8 else ""
            try:
                px = float(r.get("price"))
                prev = float(r.get("prev_close"))
            except (TypeError, ValueError):
                continue
            if px <= 0:
                continue
            snap[code] = {
                "name": str(r.get("name") or code),
                "price": px,
                "prev": prev if prev > 0 else 0.0,
                "date": qdate,
                "live": qdate == date.today().isoformat(),
            }

    live = _live_quotes(codes)
    today = date.today().isoformat()
    out: dict[str, dict] = {}
    for code in codes:
        s = snap.get(code)
        px, prev, d = _last_bar(code)
        if code in live:
            out[code] = live[code]
        elif s and (not d or (s["date"] and s["date"] >= d)):
            out[code] = s
        elif px > 0:
            out[code] = {"name": s["name"] if s else code, "price": px, "prev": prev,
                         "date": d, "live": d == today}
        elif s:
            out[code] = s
    return out


def api_holdings() -> dict:
    """我的持仓：估值 + 与今日信号对比。"""
    data = H.load(HOLD_FILE)
    meta = _pool_meta()
    codes = [i["code"] for i in data["items"]]
    quotes = _quotes(codes)
    for c, q in quotes.items():
        if meta.get(c):
            q["name"] = meta[c]["name"]
    valued = H.value(data, quotes)

    sig = _cached("signal", 5, api_signal)
    sig_rows = (sig or {}).get("rows") or []
    plan = H.advise(valued, sig_rows)

    # 池子里有哪些代码/名称，前端做输入联想用（只给代码与名称，不带行情）
    known = {c: v["name"] for c, v in meta.items()}
    return _clean_json({
        **valued,
        "cash": data.get("cash", 0.0),
        "updated_at": data.get("updated_at"),
        "advice": plan,
        # 用调仓日：这是"目标仓位取自哪一期"，不是数据截止日
        "signal_date": (sig or {}).get("reb_date") or (sig or {}).get("date"),
        "signal_exec_date": (sig or {}).get("exec_date"),
        "known": known,
    })


def api_holdings_write(payload: dict) -> tuple[dict, int]:
    """持仓写操作。返回 (响应体, HTTP 状态码)。"""
    op = str(payload.get("op") or "").strip()
    meta = _pool_meta()
    known = {c: v["name"] for c, v in meta.items()}
    data = H.load(HOLD_FILE)
    build_notes: list[dict] = []

    try:
        if op in ("add", "update"):
            item = H.validate_item(payload.get("item") or {}, known)
            data = H.upsert(data, item)
        elif op == "remove":
            code = str(payload.get("code") or "")
            if not code:
                raise H.HoldingError("缺少要删除的代码")
            data = H.remove(data, code)
        elif op == "cash":
            try:
                cash = float(payload.get("cash"))
            except (TypeError, ValueError) as exc:
                raise H.HoldingError("现金金额必须是数字") from exc
            if cash < 0 or cash > 1e12:
                raise H.HoldingError("现金金额不合理")
            data = dict(data)
            data["cash"] = round(cash, 2)
        elif op == "build":
            sig = _cached("signal", 5, api_signal)
            items, leftover, notes = H.build_from_weights(
                (sig or {}).get("rows") or [], payload.get("amount")
            )
            if not items:
                raise H.HoldingError(
                    "按这个金额买不到任何一手，请加大资金或换更低价位的标的"
                    + (f"（{notes[0]['message']}）" if notes else ""))
            data = {"items": items, "cash": leftover}
            # 整手约束造成的缺口交给界面显式提示，别让用户误以为已按信号满仓
            build_notes = notes
        elif op == "replace":
            raw_items = payload.get("items")
            if not isinstance(raw_items, list):
                raise H.HoldingError("items 必须是数组")
            items = [H.validate_item(it, known) for it in raw_items]
            data = {"items": items, "cash": data.get("cash", 0.0)}
        elif op == "clear":
            data = {"items": [], "cash": 0.0}
        else:
            return {"error": f"未知操作：{op or '(空)'}"}, 400
    except H.HoldingError as exc:
        return {"error": str(exc)}, 400

    H.save(HOLD_FILE, data)
    invalidate_cache()
    resp = dict(api_holdings())
    if build_notes:
        resp["build_notes"] = build_notes
    return resp, 200


# --------------------------------------------------------------------------- #
# 数据新鲜度
# --------------------------------------------------------------------------- #
_AUTO_LOCK = threading.Lock()
# 自动更新去重：按「今天 | 目标交易日」记，而不是只按日期。
# 盘中首次打开会把数据更到昨日；收盘后目标日推进到今天，key 变化，
# 允许再自动跑一次——否则下午收盘后必须手动点。
_AUTO_ATTEMPT: dict[str, str] = {}
_SESSION_CACHE: dict[str, tuple[float, dict]] = {}


def _data_date() -> str:
    """缓存里最新的交易日。

    取各标的 `end` 的**众数**而不是最大值：单只标的停牌、退市、或某次下载失败
    会留下更晚/更早的日期，最大最小值都会被一只异常数据带偏。
    """
    inv = _csv(DATA / "etf_history_inventory.csv", dtype=str)
    if inv is not None and "end" in inv.columns:
        vals = [v for v in inv["end"].dropna().astype(str).tolist() if len(v) == 10 and v[4] == "-"]
        if vals:
            return max(set(vals), key=vals.count)
    sigs = sorted(OUT.glob("signal_*.csv"))
    if sigs:
        return sigs[-1].stem.replace("signal_", "")
    return ""


def _bt_date() -> str:
    """回测产物的最后一天（权重表末尾）。

    它和数据末日是两个概念：只跑「更新行情」不跑「重跑回测」时，
    行情到了 10-27、权重表还停在 10-23，页面上的日期就会比侧栏旧一天，
    而数据本身是新的——所以必须分开判断，否则用户只会看到"日期不对"。
    """
    fp = OUT / "weights_s4_tgar_main.csv"
    if not fp.exists():
        return ""
    try:
        with open(fp, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 2048))
            lines = f.read().decode("utf-8", "ignore").strip().splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        d = line.split(",")[0].strip()
        if len(d) == 10 and d[4] == "-":
            return d
    return ""


def _prev_weekday(d: date) -> str:
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def _market_session() -> dict:
    """
    用沪深300ETF 的快照判断「今天是不是交易日、收盘了没有」。

    这是判断数据该更新到哪一天的唯一可靠依据：靠日历推算会被法定节假日骗过去
    （春节后打开会误报"落后 7 个交易日"），而行情时间戳在假期会停在上一个交易日。
    离线时返回空字典，由调用方退化为工作日推算。
    """
    now = time.time()
    hit = _SESSION_CACHE.get("s")
    if hit and now - hit[0] < 60:
        return hit[1]
    out: dict = {}
    try:
        from etfquant import datafeed as DF
        df = DF.fetch_snapshot(["sh510300"], timeout=3, retries=1)
        if df is not None and not df.empty:
            qt = str(df.iloc[0].get("quote_time") or "")
            if len(qt) >= 14:
                out = {
                    "date": f"{qt[0:4]}-{qt[4:6]}-{qt[6:8]}",
                    "time": f"{qt[8:10]}:{qt[10:12]}",
                }
    except Exception:  # noqa: BLE001 —— 断网时退化，不要让状态接口整体失败
        out = {}
    _SESSION_CACHE["s"] = (now, out)
    return out


def _expected_session() -> tuple[str, bool]:
    """返回 (数据应到哪天, 此刻是否盘中)。"""
    today = date.today()
    sess = _market_session()
    if sess.get("date"):
        d = sess["date"]
        if d == today.isoformat():
            # 今天确实在交易：盘中数据只能到上一个交易日，收盘后（≥15:00）到今天就齐了
            return (today.isoformat() if sess["time"] >= "15:00" else _prev_weekday(today),
                    sess["time"] < "15:00")
        return d, False          # 今天没成交（周末/节假日），应该到上一个交易日
    return _prev_weekday(today) if today.weekday() < 5 else _prev_weekday(today), (today.weekday() < 5)


def _weekdays_between(start: str, end: str) -> int:
    """(start, end] 之间的工作日数。start/end 都是 YYYY-MM-DD。"""
    try:
        a = date.fromisoformat(start)
        b = date.fromisoformat(end)
    except ValueError:
        return 0
    if b <= a:
        return 0
    n = 0
    d = a
    while d < b:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def api_status() -> dict:
    """给前端判断"要不要更新"的轻量接口。"""
    data_date = _data_date()
    expected, intraday = _expected_session()
    behind = _weekdays_between(data_date, expected) if data_date else 0
    running = None
    with TASK_LOCK:
        for tid, t in TASKS.items():
            if t["status"] == "running":
                running = {
                    "id": tid, "action": t["action"], "label": t["label"],
                    "step": t.get("step") or 0, "total_steps": t.get("total_steps") or 1,
                }
                break
    with _AUTO_LOCK:
        attempted = _AUTO_ATTEMPT.get("key") == _auto_key(expected)
    bt_date = _bt_date()
    return {
        "data_date": data_date,
        "bt_date": bt_date,
        # 行情已更新但回测没重跑：日期会"看起来不对"，必须单独提示
        "bt_stale": bool(bt_date and data_date and bt_date < data_date),
        "expected_date": expected,
        "behind_sessions": behind,
        "intraday": intraday,
        "stale": behind > 0,
        "running": running,
        "auto_attempted": attempted,
        "market_open_hint": "盘中：今日行情需收盘后并入" if intraday else "",
    }


def _auto_key(expected: str) -> str:
    return f"{date.today().isoformat()}|{expected}"


def mark_auto_attempt() -> None:
    with _AUTO_LOCK:
        _AUTO_ATTEMPT["key"] = _auto_key(_expected_session()[0])


# --------------------------------------------------------------------------- #
# 实盘跟踪（信号归档 → 纸上净值）
# --------------------------------------------------------------------------- #
def _track_panel():
    """加载面板供纸上净值使用。归档只有几期，但定价要用完整的开/收盘序列。"""
    from etfquant.pipeline import load_panel
    return _cached("track_panel", 300, lambda: load_panel(
        DATA / "etf_pool.csv", DATA / "cache", "2018-01-02", "2026-12-31"
    ))


def _strategy_targets(panel) -> dict:
    """
    同期回测的对照线：策略在**调仓日**给出的目标权重。

    不能用 output/weights_*.csv —— 那张表是**每日持仓快照**（每个交易日一行），
    当调仓信号喂给回测器会变成"每天全量再平衡"，换手与收益全错（实测年化能跑到 102%）。
    """
    from etfquant.strategy import TrendGatedAdaptive
    return _cached("track_targets", 300, lambda: TrendGatedAdaptive(panel).generate())


def _track_fp() -> str:
    """实盘跟踪缓存的输入指纹：归档文件 mtime + 行情末日。

    任一项变化即失效。用 mtime 而不是内容 hash，是为了省掉每次读几万行 CSV 的开销。
    """
    parts = []
    arch = DATA / "signal_archive.csv"
    if arch.exists():
        parts.append(str(arch.stat().st_mtime_ns))
    inv = _csv(DATA / "etf_history_inventory.csv", dtype=str)
    if inv is not None and "end" in inv.columns:
        vals = [str(v) for v in inv["end"].dropna().tolist() if len(str(v)) == 10]
        if vals:
            parts.append(max(set(vals), key=vals.count))
    return "|".join(parts) or "empty"


def _track_from_disk() -> dict | None:
    if not TRACK_CACHE.exists():
        return None
    try:
        blob = json.loads(TRACK_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return blob.get("data") if blob.get("fp") == _track_fp() else None


def _track_to_disk(data: dict) -> None:
    try:
        TRACK_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = TRACK_CACHE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"fp": _track_fp(), "data": data}, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(TRACK_CACHE)
    except OSError:
        pass


def api_track() -> dict:
    """实盘跟踪：归档多久、纸上净值多少、与同期回测差多少。

    冷启动要跑一遍完整策略（约 7 秒），这是本服务最慢的接口。因此结果落盘缓存，
    并在 start 时后台预热：用户点开页面时直接命中，而不是当场等。
    指纹变了（归档更新 / 行情推进）才重算。
    """
    disk = _track_from_disk()
    if disk is not None:
        return disk

    # 预热线程与用户请求可能同时到达：加锁做双检，避免重复算两遍 7 秒
    with TRACK_LOCK:
        disk = _track_from_disk()
        if disk is not None:
            return disk

        from etfquant import track as T

        arch = T.load_archive(T.archive_path(DATA))
        if arch.empty:
            return {"count": 0, "periods": [], "series": [], "paper": None, "backtest": None,
                    "live_count": 0, "backfill_count": 0,
                    "hint": "还没有归档信号。运行一次「生成选股信号」后开始记录。"}

        panel = _track_panel()
        res = T.build_track(panel, _strategy_targets(panel), arch)
        res["hint"] = (
            "backfill 期是事后用当前策略回放出来的，只用于垫底展示，不等于当时真实给出的建议。"
            if res.get("backfill_count") else ""
        )
        res = _clean_json(res)
        _track_to_disk(res)
        return res


def api_universe() -> dict:
    pool = _csv(DATA / "etf_pool.csv")
    if pool is None:
        return {"error": "缺少标的池文件"}
    g = pool.groupby("category").agg(
        count=("ticker", "size"), amount=("amount_yuan", "sum")
    ).sort_values("count", ascending=False)
    cats = [{"name": i, "count": int(r["count"]), "amount": round(float(r["amount"]) / 1e8, 1)}
            for i, r in g.iterrows()]

    top = pool.sort_values("amount_yuan", ascending=False).head(30)
    rows = [{
        "code": r["ticker"], "name": r["name"], "category": r["category"],
        "price": round(float(r["price"]), 3),
        "pct": round(float(r["pct_chg"]), 2),
        "amount": round(float(r["amount_yuan"]) / 1e8, 2),
        "turnover": round(float(r["turnover_pct"]), 2) if pd.notna(r["turnover_pct"]) else None,
    } for _, r in top.iterrows()]

    cache_dir = DATA / "cache" / "kline_qfq"
    n_cache = len(list(cache_dir.glob("*.csv"))) if cache_dir.exists() else 0
    latest = ""
    inv = _csv(DATA / "etf_history_inventory.csv")
    if inv is not None and len(inv):
        latest = str(inv["end"].max())[:10]

    return {"categories": cats, "top": rows, "total": int(len(pool)), "cache": n_cache, "latest": latest}


def api_portfolio() -> dict:
    """
    持仓与调仓分析。

    数据来源
    --------
    * output/weights_s4_tgar_main.csv —— 逐日逐标的权重（收盘后状态）
    * output/trades_s4_tgar_main.csv  —— 回测引擎记录的真实调仓明细
    * 价格面板 —— 计算持有期间涨幅与收益贡献

    贡献用「滞后一期权重 × 当日对数收益」逐日累加后归一化为占比。
    用对数收益而非算术收益，是因为各标的贡献之和可直接对应组合总收益，
    避免"贡献加总 134% 但净值涨了 422%"这类口径错位。
    """
    w = _csv(OUT / "weights_s4_tgar_main.csv", index_col=0, parse_dates=True)
    if w is None or w.empty:
        return {"error": "缺少权重文件，请先运行一次回测"}
    tr = _csv(OUT / "trades_s4_tgar_main.csv", parse_dates=["date"])

    pool = _csv(DATA / "etf_pool.csv", dtype={"ticker": str})
    meta = {}
    if pool is not None:
        meta = {r["ticker"]: (r["name"], r["category"]) for _, r in pool.iterrows()}

    from etfquant.pipeline import load_panel

    panel = load_panel(
        DATA / "etf_pool.csv", DATA / "cache",
        str(w.index[0].date()), str(w.index[-1].date()),
    )
    px = panel.close.reindex(columns=w.columns).ffill()
    ret = px.pct_change(fill_method=None).fillna(0.0)
    logret = np.log1p(ret.clip(lower=-0.999))
    daily_log = w.shift(1).fillna(0.0) * logret
    contrib_log = daily_log.sum()
    total_log = float(contrib_log.sum())
    hold_days = (w > 1e-6).sum()

    # ---------- 当前持仓 ----------
    held_mask = w > 1e-6
    last = w.iloc[-1]
    current = []
    for code in last[last > 1e-6].index:
        col = held_mask[code].to_numpy()
        i = len(col) - 1
        while i > 0 and col[i - 1]:
            i -= 1
        since = w.index[i]
        try:
            p0 = float(px[code].loc[since])
            p1 = float(px[code].iloc[-1])
            since_ret = p1 / p0 - 1 if p0 > 0 else 0.0
        except (KeyError, ZeroDivisionError):
            since_ret = 0.0
        name, cat = meta.get(code, (code, ""))
        cur_log = float(contrib_log.get(code, 0.0))
        current.append({
            "code": code, "name": name, "category": cat,
            "weight": round(float(last[code]), 4),
            "since": since.strftime("%Y-%m-%d"),
            # 交易日 = 持仓跨越的 K 线根数（含首日）；日历天 = 自然日差。
            # 早先只返回前者却标成"天"，09-21 买入显示 21 天，用户按日历一算差 16 天，
            # 看起来就像程序算错了。两个都给，界面按"个交易日"标注。
            "days": int(len(col) - i),
            "days_trade": int(len(col) - i),
            "days_calendar": int((w.index[-1] - since).days),
            "ret_since": round(since_ret, 4),
            "contrib": round(cur_log * 100, 2),
            "share": round(cur_log / total_log, 4) if total_log else 0.0,
        })
    current.sort(key=lambda r: -r["weight"])

    # ---------- 收益贡献（全历史） ----------
    rows = []
    for code in contrib_log.index:
        v = float(contrib_log[code])
        if abs(v) < 1e-6:
            continue
        name, cat = meta.get(code, (code, ""))
        rows.append({
            "code": code, "name": name, "category": cat,
            "contrib": round(v * 100, 2),
            "share": round(v / total_log, 4) if total_log else 0.0,
            "days": int(hold_days.get(code, 0)),
            "now": bool(last.get(code, 0) > 1e-6),
        })
    rows.sort(key=lambda r: -r["contrib"])

    # ---------- 调仓记录 ----------
    trades: list[dict] = []
    if tr is not None and not tr.empty:
        turn_by_day = tr.groupby("date")["delta"].apply(lambda x: float(x.abs().sum()) / 2.0)
        for dt in list(turn_by_day.index)[-16:][::-1]:
            g = tr[tr["date"] == dt]
            buys = g[g["delta"] > 1e-4].sort_values("delta", ascending=False)
            sells = g[g["delta"] < -1e-4].sort_values("delta")
            trades.append({
                "date": dt.strftime("%Y-%m-%d"),
                "turnover": round(float(turn_by_day.loc[dt]), 4),
                "buys": [{"code": r["code"], "name": meta.get(r["code"], (r["code"], ""))[0],
                          "from": round(float(r["w_from"]), 4), "to": round(float(r["w_to"]), 4)}
                         for _, r in buys.iterrows()],
                "sells": [{"code": r["code"], "name": meta.get(r["code"], (r["code"], ""))[0],
                           "from": round(float(r["w_from"]), 4), "to": round(float(r["w_to"]), 4)}
                          for _, r in sells.iterrows()],
            })
        n_reb = int(turn_by_day.shape[0])
        total_turn = float(turn_by_day.sum())
    else:
        n_reb = 0
        total_turn = 0.0

    years = max(len(w) / 252, 1e-9)

    # 成本取回测引擎实算值：换手率 × 2（双边）× 单边费率。
    # 单边费率从 perf 表反解，避免硬编码与回测参数脱节。
    fee_side = 0.001
    _row = _perf_row(SELF)
    if _row is not None:
        real_turn = float(_row.get("年均换手(单边)", 0) or 0)
        real_cost = float(_row.get("累计交易成本", 0) or 0)
        if real_turn > 0 and real_cost > 0:
            fee_side = real_cost / (real_turn * years * 2)

    per_reb_turn = total_turn / n_reb if n_reb else 0.0
    stats = {
        "rebalances": n_reb,
        "avg_holdings": round(float(held_mask.sum(axis=1).mean()), 2),
        "avg_turnover": round(total_turn / years, 2),
        "symbols": int((w > 1e-6).any().sum()),
        "max_weight": round(float(last.max()), 4),
        "per_reb_turn": round(per_reb_turn, 4),
        "fee_side": round(fee_side, 5),
        "cost_total": round(total_turn * 2 * fee_side, 4),
        "cost_annual": round(total_turn * 2 * fee_side / years, 4),
    }

    return {
        "as_of": w.index[-1].strftime("%Y-%m-%d"),
        "current": current,
        "contrib": rows,
        "trades": trades,
        "stats": stats,
    }


def api_actions() -> dict:
    running = {}
    with TASK_LOCK:
        for tid, t in TASKS.items():
            if t["status"] == "running":
                running[t["action"]] = tid
    return {
        "actions": [{"id": k, **{kk: vv for kk, vv in v.items() if kk != "script"}} for k, v in ACTIONS.items()],
        "running": running,
    }


def _lan_ip() -> str:
    """本机在局域网中的地址，用于告诉手机该连哪里。"""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # 不会真的发包，只是让内核选一张出网网卡
        sock.connect(("223.5.5.5", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        if sock is not None:
            sock.close()


def _history_payload(days: int = 300) -> dict:
    """
    导出最近 N 个交易日的日线，供手机端**本地计算**信号。

    为什么必须内置历史
    ------------------
    手机端要独立运行，就得自己算 regime、MA200、R20、量比这些指标。
    算这些至少需要 250 根日线（min_hist），300 天留出安全余量。

    为什么不导出成交量与成交额两个字段
    --------------------------------
    桌面端 ``Panel.amount = close × volume × 100``，这里只导出 close 与 volume，
    由手机端用同一公式现算 amount —— 口径天然一致，且体积少三分之一。

    对齐方式
    --------
    以全池交易日的并集为统一时间轴（各 ETF 交易日必然一致），
    未上市/停牌的位置填 null，手机端按 null 跳过，与桌面端「该日无行情」等价。
    """
    pool = pd.read_csv(DATA / "etf_pool.csv", dtype={"ticker": str})
    codes = [c for c in pool["ticker"].tolist()]
    frames: dict[str, pd.DataFrame] = {}
    for tk in codes:
        fp = DATA / "cache" / "kline_qfq" / f"{tk}.csv"
        if not fp.exists():
            continue
        try:
            df = pd.read_csv(fp, index_col=0, parse_dates=True)
        except (OSError, ValueError):
            continue
        if df.empty or "close" not in df.columns:
            continue
        frames[tk] = df

    if not frames:
        return {"dates": [], "codes": [], "close": [], "vol": []}

    all_dates = sorted(set().union(*(df.index for df in frames.values())))[-days:]
    axis = pd.DatetimeIndex(all_dates)
    used = [c for c in codes if c in frames]

    closes: list[list] = []
    vols: list[list] = []
    for tk in used:
        df = frames[tk].reindex(axis)
        c = df["close"].to_numpy()
        v = df["volume"].to_numpy() if "volume" in df.columns else np.full(len(axis), np.nan)
        closes.append([None if pd.isna(x) else round(float(x), 3) for x in c])
        vols.append([None if pd.isna(x) else int(x) for x in v])

    # 名称与类别随历史一起下发：手机端要靠 category 把债券排除出打分，
    # 早先只把版本信息放进 payload.meta，导致手机端拿不到类别、债券被当成
    # 权益品种选进去（实测信号完全错）。
    pool_meta = {}
    for _, r in pool.iterrows():
        tk = r["ticker"]
        if tk in frames:
            pool_meta[tk] = {"name": r["name"], "category": r["category"]}

    return {
        "dates": [d.strftime("%Y-%m-%d") for d in axis],
        "codes": used,
        "close": closes,
        "vol": vols,
        "meta": pool_meta,
    }


def _data_version() -> tuple[str, str]:
    """
    数据版本 = 信号日期 + 产物文件的最新修改时间戳。

    必须**确定性**：不能用"当前时间"，否则每次探测都会算出一个新版本，
    App 会陷入"永远有新数据"的循环更新。用文件 mtime 既便宜又只在数据真的变了才变。
    """
    sigs = sorted(OUT.glob("signal_*.csv"))
    sig_date = sigs[-1].stem.replace("signal_", "") if sigs else ""
    stamps = []
    for name in ("nav_main.csv", "perf_main.csv", "weights_s4_tgar_main.csv",
                 "holdings_s4_tgar_main.csv"):
        fp = OUT / name
        if fp.exists():
            stamps.append(int(fp.stat().st_mtime))
    if sigs:
        stamps.append(int(sigs[-1].stat().st_mtime))
    token = max(stamps) if stamps else 0
    return f"{sig_date}@{token}", sig_date


def _app_version() -> str:
    """App 版本号以 AndroidManifest.xml 为唯一来源。

    之前这里有第二处硬编码 "1.3.0"（手机「关于」页也有一份 fallback），
    发版时忘了改，界面上就会显示旧版本号。读清单文件，改一处即可。
    """
    fp = ROOT / "android" / "AndroidManifest.xml"
    try:
        m = re.search(r'android:versionName="([^"]+)"', fp.read_text(encoding="utf-8"))
    except OSError:
        return ""
    return m.group(1) if m else ""


def build_mobile_payload(points: int = 200) -> dict:
    """
    生成手机端所需的完整数据包（与 scripts/export_mobile_data.py 共用一套逻辑）。

    抽成函数而不是只写在导出脚本里，是为了让 App 能通过 HTTP 直接拉到同一份内容，
    避免"打包用的数据"与"在线更新的数据"出现两套口径。
    """
    overview = api_overview()
    if "error" in overview:
        raise RuntimeError(overview["error"])

    # 手机上 5 条线会糊成一团，只保留基准 / 主策略 / 基线三条，并抽稀
    keep = [BENCH, SELF, "S1-动量基线"]
    series = []
    for item in overview["series"]:
        if item["name"] in keep:
            series.append({"name": item["name"], "data": _thin(item["data"], points)})
    overview["series"] = series
    overview["drawdown"] = {
        "self": _thin(overview["drawdown"]["self"], points),
        "bench": _thin(overview["drawdown"]["bench"], points),
    }

    signal = api_signal()
    portfolio = api_portfolio()
    if "error" in portfolio:
        raise RuntimeError(portfolio["error"])
    portfolio["trades"] = portfolio["trades"][:10]

    research = api_research()
    universe = api_universe()
    universe["top"] = universe["top"][:12]

    version, _ = _data_version()
    # signal_date 用调仓日（不是数据截止日）：App 用它对齐"这一期是哪天定的"，
    # 若填数据日，同一次调仓会被当成不同的期次。
    signal_date = str(signal.get("reb_date") or signal.get("date") or "")
    return {
        "history": _history_payload(),
        "meta": {
            "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
            # App 用它判断是否需要覆盖本地数据；只随数据变化，不随运行时间变化
            "data_version": version,
            "signal_date": signal_date,
            "exec_date": signal.get("exec_date"),
            "next_reb": signal.get("next_reb"),
            "version": _app_version(),
        },
        "overview": overview,
        "signal": signal,
        "portfolio": portfolio,
        "research": research,
        "universe": universe,
    }


def _thin(points: list, target: int) -> list:
    """等距抽稀折线点，保留首尾。"""
    n = len(points)
    if n <= target:
        return points
    step = max(1, n // target)
    out = points[::step]
    if out[-1] != points[-1]:
        out.append(points[-1])
    return out


def api_mobile_data() -> str:
    """返回可直接被 <script> 引入的数据脚本。"""
    payload = build_mobile_payload()
    return "/* 由 server.py 动态生成 */\nwindow.__DATA__ = " + json.dumps(
        _clean_json(payload), ensure_ascii=False, separators=(",", ":")
    ) + ";\n"


def api_mobile_info() -> dict:
    """
    轻量探针：手机启动时先问一句"你那边是什么版本"。

    只读文件 mtime 与信号 CSV，不加载价格面板、不算持仓，实测 <10ms；
    对比完整包（1.8s / 60KB），让"每次打开都自动检查"变得没有代价。
    """
    version, sig_date = _data_version()
    exec_date = None
    sigs = sorted(OUT.glob("signal_*.csv"))
    if sigs:
        try:
            d = pd.Timestamp(sig_date)
            exec_date = _next_trading_day(d).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            exec_date = None
    return {
        "data_version": version,
        "signal_date": sig_date,
        "exec_date": exec_date,
        # 注意：服务只监听 127.0.0.1，手机连不上这个地址。
        # 这里原样给出的是「如果改成监听 0.0.0.0 时可用」的地址，
        # 仅供 PC 端自测/预览引用；APK 自己直连腾讯行情，不依赖本机。
        "endpoint": None,
        "endpoint_if_lan": f"http://{_lan_ip()}:{CURRENT_PORT}/api/mobile/data",
    }


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "etfquant/1.0"

    def log_message(self, fmt, *args):  # 静默，避免控制台刷屏
        pass

    # ---------- 响应工具 ---------- #
    def _json(self, obj, code: int = 200, cors: bool = False) -> None:
        # allow_nan=False 让"漏清洗"当场抛错，而不是把非法 JSON 发给浏览器
        body = json.dumps(_clean_json(obj), ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cors:
            # 手机端在浏览器里调试时是跨源请求；Android 版走原生桥不经过这里
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, fp: Path, ctype: str | None = None) -> None:
        if not fp.exists() or not fp.is_file():
            self._json({"error": "not found"}, 404)
            return
        data = fp.read_bytes()
        if ctype is None:
            ctype = mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
        if fp.suffix in (".html", ".css", ".js"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # ---------- 路由 ---------- #
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        qs = parse_qs(urlparse(self.path).query)

        try:
            if path == "/api/overview":
                return self._json(_cached("overview", 5, api_overview))
            if path == "/api/_debug_paths":
                return self._json(api_debug_paths())
            if path == "/api/signal":
                return self._json(_cached("signal", 5, api_signal))
            if path == "/api/research":
                return self._json(_cached("research", 30, api_research))
            if path == "/api/universe":
                return self._json(_cached("universe", 30, api_universe))
            if path == "/api/portfolio":
                return self._json(_cached("portfolio", 120, api_portfolio))
            if path == "/api/holdings":
                # 不缓存：写完立刻要看到结果，且读一次只有 1 个 json + 261 行快照
                return self._json(api_holdings())
            if path == "/api/actions":
                return self._json(api_actions())
            if path == "/api/track":
                return self._json(_cached("track", 60, api_track))
            if path == "/api/status":
                # 不缓存：判断"要不要更新"必须是当下的事实
                return self._json(api_status())
            if path == "/api/reveal-apk":
                target = ROOT / "dist" / "etf-quant.apk"
                if not target.exists():
                    return self._json({"error": "APK 尚未构建，请先运行 bash android/build.sh"}, 404)
                try:
                    if sys.platform == "win32":
                        os.startfile(str(target.parent))  # noqa: S606
                    return self._json({"opened": str(target.parent)})
                except OSError as exc:
                    return self._json({"error": str(exc)}, 500)
            if path == "/api/mobile/info":
                return self._json(_cached("mobile_info", 20, api_mobile_info), cors=True)
            if path == "/api/mobile/data":
                body = _cached("mobile_data", 20, api_mobile_data)
                raw = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(raw)
                return None
            if path == "/api/task":
                tid = (qs.get("id") or [""])[0]
                with TASK_LOCK:
                    t = TASKS.get(tid)
                    if not t:
                        return self._json({"error": "task not found"}, 404)
                    # log 是列表，dict(t) 只是浅拷贝，worker 并发 append 时
                    # 序列化可能读到半更新的内容，这里做一次快照
                    snapshot = {**t, "log": list(t["log"])}
                return self._json(snapshot)
            if path == "/report":
                return self._file(OUT / "ETF量化策略研究报告.html", "text/html")
            if path.startswith("/static/"):
                rel = unquote(path[len("/static/"):])
                target = (WEB / rel).resolve()
                # 字符串前缀匹配会放行 ../web_evil/ 这类同前缀兄弟目录，
                # 必须用路径层级判断
                try:
                    target.relative_to(WEB.resolve())
                except ValueError:
                    return self._json({"error": "forbidden"}, 403)
                return self._file(target)
            if path in ("/", "/index.html"):
                return self._file(WEB / "index.html", "text/html")
            return self._json({"error": "not found", "path": path}, 404)
        except Exception as exc:  # noqa: BLE001
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        length = max(0, min(length, 1 << 20))  # 本地工具，1MB 上限足够，避免超大 body
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            payload = {}

        if path == "/api/holdings":
            body, code = api_holdings_write(payload)
            return self._json(body, code)

        if path == "/api/run":
            action = payload.get("action", "")
            if action not in ACTIONS:
                return self._json({"error": f"未知任务：{action}"}, 400)
            with TASK_LOCK:
                for tid, t in TASKS.items():
                    if t["action"] == action and t["status"] == "running":
                        return self._json({"task_id": tid, "reused": True})
            try:
                tid = start_task(action)
                if action == "refresh":
                    mark_auto_attempt()
                return self._json({"task_id": tid})
            except RuntimeError as exc:
                return self._json({"error": str(exc)}, 409)
        return self._json({"error": "not found"}, 404)


class _Server(ThreadingHTTPServer):
    # Windows 上 SO_REUSEADDR 允许两个进程同时绑同一个端口、请求随机分发，
    # 表现就是"打开同一地址有时是别的程序"。只在非 Windows 上开启。
    allow_reuse_address = sys.platform != "win32"


def find_port(start: int = 8800, tries: int = 40) -> int:
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            # 探测时**不要**设 SO_REUSEADDR：Windows 上它的语义是"允许抢占已被监听
            # 的端口"，设了之后连别人正在用的端口都会 bind 成功，等于把占用当成空闲。
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError("找不到可用端口")


def _warm_caches() -> None:
    """启动后在后台预热最慢的接口。

    实盘跟踪冷启动要跑一遍完整策略（约 7 秒）——不预热的话，用户点开那一页
    才开始算，会以为界面卡住了。结果会落盘，所以下次启动通常直接命中缓存。
    """
    try:
        api_track()
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    if not WEB.exists():
        print(f"[ERR] 缺少前端目录：{WEB}")
        return 1

    global CURRENT_PORT
    port = args.port or find_port()
    CURRENT_PORT = port
    url = f"http://127.0.0.1:{port}/"
    srv = _Server(("127.0.0.1", port), Handler)
    srv.daemon_threads = True

    print("=" * 62)
    print("  ETF 量化选股工具 · Web 控制台")
    print("=" * 62)
    print(f"  地址   {url}")
    print(f"  项目   {ROOT}")
    print("  退出   按 Ctrl+C 或直接关闭此窗口")
    print("=" * 62)

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    # 预热最慢的接口（实盘跟踪），放后台线程：不挡首屏，也不挡端口监听
    threading.Thread(target=_warm_caches, daemon=True, name="warm").start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[已停止]")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
