"""
历史压力测试：把回测推到 2015-2016（杠杆牛 → 股灾 → 熔断），看闸门的表现。

为什么做这个
------------
主回测从 2019 起，样本里最极端的熊是 2022（缓慢阴跌）。2015-06~2016-01 是另一种
完全不同的环境：连续跌停、流动性消失、熔断——"追最强"的动量策略在这种环境里
最容易出现「减仓永远慢一步」的连环损失。这段历史能回答一个问题：
**闸门（而不是运气）有没有把组合保住。**

口径与限制（先说清，避免误读结果）
----------------------------------
* 池子 260 只里只有 **36 只**在 2018 前上市（当前池 = 活下来的，2015-2018 清盘潮
  的那批不在），所以 2015-2016 的横截面只有 20~30 只可选——这段是**压力测试**，
  不是"更多样本"，不能拿年化和 2019-2026 主回测直接比；
* 数据来自腾讯前复权日线（实测 510300 自 2012-05-28 上市首日）；
* 引擎、成本、预热与主回测完全一致：T+1 开盘、双边 0.10%、min_hist 250；
* 对照组：无闸门版（regime_mid=0，永远满仓）与沪深300——闸门的价值 = 有闸门 − 无闸门。

用法：python scripts/research/ab_early_era.py
产物：output/experiments/ab_early_era.csv（含 2015-04~2016-03 的闸门状态轨迹）
"""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from etfquant import metrics  # noqa: E402
from etfquant.backtest import Backtester  # noqa: E402
from etfquant.datafeed import fetch_kline_window, market_last_trade_date  # noqa: E402
from etfquant.io_utils import atomic_to_csv, atomic_write_text  # noqa: E402
from etfquant.pipeline import load_panel  # noqa: E402
from etfquant.strategy import TrendGatedAdaptive  # noqa: E402

PANEL_START, OLD_END = "2013-01-02", "2018-01-02"
BT_START = "2014-02-10"          # 预热(260 日)之后的统一起点
CRASH_TOP, CRASH_BOT = "2015-06-12", "2016-01-28"   # 5178 峰值 → 熔断底
POOL = ROOT / "data" / "etf_pool.csv"
CACHE = ROOT / "data" / "cache"
# 早期缓存单独放，**不并进生产缓存**：
# 生产缓存从 2018-01-02 起，恰好 243 个交易日 < min_hist(250)，所以 2019-01-02 时
# 没有任何标的是"合格"的；把 2013-2017 补进来会让 36 只当天就满 250 日，
# 2019 年净值因此最多偏离 0.79%（实测），等于换了一套机会集。
# 研究要看早期可以，但**不能顺手改掉主回测的起点语义**。
CACHE_EARLY = ROOT / "data" / "cache_early"
LIST_FP = ROOT / "output" / "experiments" / "_old_etfs.json"


def _windows(start: str, end: str, years: int = 2) -> list[tuple[str, str]]:
    """把区间切成 ≤2 年的块。腾讯单次最多 640 根，2 年约 490 根，一次够用。"""
    a, b = pd.Timestamp(start), pd.Timestamp(end)
    out = []
    while a < b:
        nxt = min(a + pd.DateOffset(years=years), b)
        out.append((a.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d")))
        a = nxt + pd.Timedelta(days=1)
    return out


def fetch_range(tk: str, start: str, end: str, pause: float = 0.3) -> pd.DataFrame:
    """
    按 2 年分块取数并拼接。

    为什么不用 `fetch_kline`：它按**每月**一个窗口翻页，260 只标的就要发近 5000 次
    请求——第一次跑正是因此被腾讯整段 501 限流，把"被限流"误读成"没有数据"，
    最后写出一个空清单（`for/else` 里 `res` 每轮被覆盖，231 个成功结果全丢）。
    2 年一块能把请求数压到 1/12。
    """
    frames = []
    for s0, e0 in _windows(start, end):
        part = fetch_kline_window(tk, s0, e0)
        if part is not None and not part.empty:
            frames.append(part)
        time.sleep(pause)
    if not frames:
        return pd.DataFrame()
    return (pd.concat(frames, ignore_index=True)
            .drop_duplicates(subset="date", keep="last")
            .sort_values("date").reset_index(drop=True))


def probe_old_etfs() -> dict[str, str]:
    """
    找出 2016 年下半年已有数据的 ETF（= 2018 前上市）。

    三条纪律（都是被上一版坑出来的）：
    1. **结果逐轮累积**，不能每轮覆盖——否则最后一轮只剩失败项，成功全丢；
    2. **只重试真正报错的**，且最多 3 轮；"无数据"是正常结果，不算失败；
    3. 每轮结束就落盘，进程被杀也不至于从头再来。
    """
    if LIST_FP.exists():
        try:
            cached = json.loads(LIST_FP.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = {}
        if cached:                      # 只接受非空缓存：空数组是上一版的 bug 产物
            print(f"[probe] 复用缓存清单 {len(cached)} 只")
            return cached

    pool = pd.read_csv(POOL, dtype={"ticker": str})
    pending = pool["ticker"].tolist()
    ok: dict[str, str] = {}

    for cycle in range(3):
        print(f"[probe] 第 {cycle+1} 轮（{len(pending)} 只，单次 ≤2 年窗口）…")
        errs = []
        streak = 0
        for i, tk in enumerate(pending, 1):
            try:
                df = fetch_range(tk, "2016-06-01", "2017-12-31", pause=0.15)
                if df.empty:
                    pass                        # 未上市，正常
                else:
                    ok[tk] = str(df["date"].iloc[0])
                streak = 0
            except Exception:                   # noqa: BLE001
                errs.append(tk)
                streak += 1
                # 连续失败 = 整段被封，不是某只标的的问题。再跑 200 次也只是白等。
                if streak >= 15:
                    print(f"[probe] 连续 {streak} 只请求失败，判定接口被限流，停止本轮。")
                    break
            if i % 60 == 0:
                print(f"       {i}/{len(pending)}… 已确认 {len(ok)} 只", flush=True)
        if streak >= 15 and not ok:
            # 整段被封：重试也不会变，直接交给 main() 的"空清单"闸门退出。
            print("[probe] 接口被限流且一只都没确认，放弃本次压力测试（不写空清单）。")
            return {}
        atomic_write_text(LIST_FP, json.dumps(ok, ensure_ascii=False, indent=1))
        print(f"[probe] 第 {cycle+1} 轮结束：累计 {len(ok)} 只，报错 {len(errs)} 只")
        if not errs:
            break
        pending = errs
        if cycle < 2:
            time.sleep(60)

    print(f"[probe] 2018 前上市 {len(ok)} 只（清单已缓存）")
    return ok


def extend_cache(old: dict[str, str]) -> list[str]:
    """给老 ETF 把缓存从 2018-01-02 向前补到 2013-01-02。"""
    done, empty, fail = [], [], []
    for i, tk in enumerate(sorted(old), 1):
        fp = CACHE_EARLY / "kline_qfq" / f"{tk}.csv"
        try:
            if fp.exists():
                cur = pd.read_csv(fp, parse_dates=["date"])
                if cur["date"].min() <= pd.Timestamp("2013-02-01"):
                    done.append(tk)
                    continue
            else:
                cur = pd.DataFrame()
            old_df = fetch_range(tk, PANEL_START, OLD_END)
            if old_df.empty:
                empty.append(tk)
                print(f"  [{i:>2}/{len(old)}] {tk}  腾讯无 2018 前数据")
                continue
            old_df = old_df[old_df["date"] < pd.Timestamp(OLD_END)]
            merged = (pd.concat([old_df, cur], ignore_index=True)
                      .drop_duplicates(subset="date", keep="last")
                      .sort_values("date").reset_index(drop=True)
                      if not cur.empty else old_df)
            # 拼接处的前复权基准必须连续：跳空 >5% 说明两段用了不同的复权因子
            j = merged["date"].searchsorted(pd.Timestamp(OLD_END))
            gap = float(merged["close"].iloc[j] / merged["close"].iloc[j - 1] - 1) if 0 < j < len(merged) else 0.0
            atomic_to_csv(merged, fp, index=False)
            done.append(tk)
            flag = "" if abs(gap) < 0.05 else f"  [!] 拼接处跳空 {gap*100:+.1f}%，复权基准可能不一致"
            print(f"  [{i:>2}/{len(old)}] {tk}  前补 {len(old_df)} 根 → 起 {merged['date'].iloc[0].date()}{flag}")
        except Exception as exc:  # noqa: BLE001
            fail.append(tk)
            print(f"  [{i:>2}/{len(old)}] {tk} 失败 {type(exc).__name__}: {str(exc)[:60]}")
    print(f"[extend] 成功 {len(done)} / 无数据 {len(empty)} / 失败 {len(fail)}")
    return done


def fmt(v, d=1) -> str:
    return "—" if v is None or v != v else f"{v * 100:.{d}f}%"


def window_stats(nav: pd.Series, bench: pd.Series, start: str, end: str) -> dict:
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    n = nav.loc[s:e].dropna()
    b = bench.loc[s:e].dropna()
    if len(n) < 3:
        return {}
    peak_i = n.idxmax()
    seg = n.loc[peak_i:e]
    mdd, dur, _, _ = metrics.max_dd_detail(n)
    return {
        "区间收益": float(n.iloc[-1] / n.iloc[0] - 1),
        "最大回撤": float(mdd),
        "峰值日": str(peak_i.date()), "谷底日": str(seg.idxmin().date()),
        "基准区间收益": float(b.iloc[-1] / b.iloc[0] - 1) if len(b) > 3 else None,
    }


def main() -> int:
    print("[1/4] 找 2018 前上市的 ETF…")
    old = probe_old_etfs()
    print(f"      {len(old)} 只（清单缓存于 {LIST_FP.name}）")
    if not old:
        # 2026-10-30 就是这样空跑了 33 分钟：接口 501 被当成"没有数据"，
        # 清单写成空数组，后面补缓存 0 次、面板仍是 2018 起，最后输出了
        # 一张"股灾段全是 —"的表。宁可在这里停住。
        print("[FAIL] 没有任何 2018 前上市的标的——极可能是接口限流被误读成「无数据」。"
              "缓存与产物均未改动，稍后重试。")
        return 3

    print("[2/4] 把缓存向前补到 2013-01…")
    done = extend_cache(old)
    print(f"      就绪 {len(done)}/{len(old)}")

    print("[3/4] 加载面板并回测…")
    panel = load_panel(POOL, CACHE_EARLY, PANEL_START, "2026-12-31")
    bt = Backtester(panel.close, panel.open_)
    bench = panel.close["sh510300"].dropna()

    variants = [
        ("TGAR（有闸门）", TrendGatedAdaptive(panel)),
        ("无闸门（永远满仓）", TrendGatedAdaptive(panel, regime_mid=0.0)),
    ]
    sigs = {}
    for label, strat in variants:
        t0 = time.time()
        sig = strat.generate()
        sigs[label] = sig
        print(f"      {label}: {len(sig)} 期信号（{time.time()-t0:.0f}s）")

    rows = []
    for label, sig in sigs.items():
        r = bt.run(sig, start=BT_START, end="2019-12-31")
        nav = r.nav
        full = window_stats(nav, bench, BT_START, "2019-12-31")
        crash = window_stats(nav, bench, CRASH_TOP, CRASH_BOT)
        ext = window_stats(nav, bench, "2016-02-01", "2019-12-31")
        rows.append({
            "策略": label, "起点": BT_START,
            "全程收益": full.get("区间收益"), "全程回撤": full.get("最大回撤"),
            "股灾段收益": crash.get("区间收益"), "股灾段回撤": crash.get("最大回撤"),
            "股灾基准收益": crash.get("基准区间收益"),
            "2016-02 起收益": ext.get("区间收益"), "2016-02 起回撤": ext.get("最大回撤"),
            "年换手": float(r.stats.get("年均换手(单边)", float("nan"))),
        })

    bs = window_stats(bench, bench, BT_START, "2019-12-31")
    cs = window_stats(bench, bench, CRASH_TOP, CRASH_BOT)
    rows.append({
        "策略": "沪深300（基准）", "起点": BT_START,
        "全程收益": bs.get("区间收益"), "全程回撤": bs.get("最大回撤"),
        "股灾段收益": cs.get("区间收益"), "股灾段回撤": cs.get("最大回撤"),
        "股灾基准收益": None, "2016-02 起收益": None, "2016-02 起回撤": None, "年换手": 0.0,
    })
    df = pd.DataFrame(rows)
    atomic_to_csv(df, ROOT / "output" / "experiments" / "ab_early_era.csv", index=False, encoding="utf-8-sig")

    print(f"\n=== 压力测试 {BT_START} ~ 2019-12（横截面仅 20~30 只，勿与主回测直接比） ===")
    print(f"{'策略':<20}{'全程':>9}{'全程回撤':>9}{'股灾段':>9}{'股灾回撤':>9}{'基准段':>9}{'换手':>7}")
    for r in df.to_dict("records"):
        print(f"{r['策略']:<20}{fmt(r['全程收益']):>9}{fmt(r['全程回撤']):>9}"
              f"{fmt(r['股灾段收益']):>9}{fmt(r['股灾段回撤']):>9}{fmt(r['股灾基准收益']):>9}"
              f"{r['年换手']:>7.1f}")

    # ---------- 闸门在股灾里的状态轨迹 ----------
    tgar = TrendGatedAdaptive(panel)
    print("\n=== 闸门状态轨迹（2015-04 ~ 2016-03，每月末） ===")
    print(f"{'日期':<12}{'regime':>7}{'敞口':>6}{'本策略':>9}{'沪深300':>9}")
    n_t = sigs["TGAR（有闸门）"] and bt.run(sigs["TGAR（有闸门）"], start=BT_START, end="2019-12-31").nav
    b_n = bench.loc[n_t.index[0]:n_t.index[-1]]
    b_n = b_n / b_n.iloc[0]
    trace = []
    for d in pd.date_range("2015-04-30", "2016-03-31", freq="ME"):
        pos = panel.close.index[panel.close.index <= d]
        if len(pos) == 0:
            continue
        dt = pos[-1]
        reg = tgar.regime_at(dt)
        expo = tgar._exposure(reg)
        nn = n_t.loc[:dt]
        bb = b_n.loc[:dt]
        trace.append({
            "日期": str(dt.date()), "regime": round(reg, 2), "敞口": expo,
            "本策略": round(float(nn.iloc[-1] / nn.iloc[0] - 1) * 100, 1),
            "沪深300": round(float(bb.iloc[-1] - 1) * 100, 1),
        })
    tr = pd.DataFrame(trace)
    atomic_to_csv(tr, ROOT / "output" / "experiments" / "ab_early_era_gate.csv", index=False, encoding="utf-8-sig")
    for r in tr.to_dict("records"):
        print(f"{r['日期']:<12}{r['regime']:>7.2f}{r['敞口']:>6.1f}"
              f"{r['本策略']:>8}%{r['沪深300']:>8}%")

    print(f"\n[OK] 已写 output/experiments/ab_early_era.csv + ab_early_era_gate.csv")
    print("[注] 横截面早期只有 20~30 只（且为幸存者），这段结论用于「闸门是否有效」，"
          "不能外推成年化预期。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
