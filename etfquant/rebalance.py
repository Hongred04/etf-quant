"""
调仓日历：把「每天都能跑的信号」钉回「每 N 个交易日调一次仓」。

为什么需要这个模块
------------------
策略的回测口径是**每 5 个交易日调一次仓**（`rebalance_days()` = 索引第 260 根起、
步长 `rebalance_freq` 取样）。但 `run_signal.py` 原先取「数据最后一个交易日」重算目标，
于是连续三天运行会得到三份不同的持仓（2026-10-26 地产 / 10-27 红利低波 / 10-28 纳指），
照做等于每天换仓——换手是回测的 3~5 倍，成本按比例放大，而且「实盘跟踪」会如实记下
一笔策略里并不存在的再平衡。

这个模块只做三件事，全部是纯函数，不读文件、不碰行情：

* `last_rebalance`    —— 给定日期，找它之前（含当日）最近一个调仓日；
* `next_rebalance`    —— 下一个调仓日；数据没覆盖到时按交易日估算并标记 `estimated`；
* `trading_days_between` —— 两个日期之间隔了几个交易日。

约定：`index` 是交易日索引（`panel.close.index`），`sig_date` 必须落在 `index` 上。
调仓网格是等步长的，所以下一个调仓日 = `index[pos + freq]`，不必知道 warmup 是多少。
"""
from __future__ import annotations

import pandas as pd


def last_rebalance(rebalance_days: list[pd.Timestamp], asof) -> pd.Timestamp | None:
    """`asof` 之前（含当日）最近一个调仓日；没有则返回 None。

    显式要求 `<= asof`：用 `bisect` 之类找"最近"很容易写成"之后"，那样会用到未来信息。
    """
    if not rebalance_days:
        return None
    asof = pd.Timestamp(asof)
    cands = [d for d in rebalance_days if d <= asof]
    return cands[-1] if cands else None


def next_rebalance(index: pd.DatetimeIndex, sig_date, freq: int) -> tuple[pd.Timestamp, bool]:
    """
    下一个调仓日，返回 `(日期, 是否为估算值)`。

    调仓网格等步长，所以下一个调仓日就是 `sig_date` 往后数 `freq` 个交易日。
    数据覆盖不到时，用工作日历向前推（`estimated=True`）——只用于界面提示
    "下次大约在几号"，不参与任何计算，因此不必精确到交易所节假日。
    """
    freq = max(1, int(freq))
    sig_date = pd.Timestamp(sig_date)
    pos = index.get_indexer([sig_date], method="pad")[0]
    if pos < 0:
        return sig_date + pd.offsets.BDay(freq), True

    target = pos + freq
    if target < len(index):
        return index[target], False

    # 数据到此为止：从最后一根往后补足剩余步数
    tail = pd.bdate_range(index[-1], periods=(target - len(index) + 2))
    return tail[target - len(index) + 1], True


def next_trading_day(index: pd.DatetimeIndex, d) -> tuple[pd.Timestamp, bool]:
    """`d` 之后的第一个交易日（T 日收盘出信号、T+1 开盘执行）。"""
    d = pd.Timestamp(d)
    pos = index.get_indexer([d], method="pad")[0]
    if pos < 0 or pos + 1 >= len(index):
        return pd.bdate_range(d, periods=2)[1], True
    return index[pos + 1], False


def trading_days_between(index: pd.DatetimeIndex, a, b) -> int:
    """`a` 之后到 `b`（含）之间有几个交易日；`b` 超出数据范围时按工作日补足。"""
    a, b = pd.Timestamp(a), pd.Timestamp(b)
    if b <= a or len(index) == 0:
        return 0
    last = index[-1]
    if b <= last:
        return int(((index > a) & (index <= b)).sum())
    n = int((index > a).sum())
    # 超出数据的部分按工作日估算；bdate_range 含起点，所以减 1
    return n + max(0, len(pd.bdate_range(last, b)) - 1)


def schedule(index: pd.DatetimeIndex, sig_date, freq: int) -> dict:
    """一次性给出界面需要的全部日期，避免各处各算一遍。"""
    sig_date = pd.Timestamp(sig_date)
    nxt, nxt_est = next_rebalance(index, sig_date, freq)
    ex, ex_est = next_trading_day(index, sig_date)
    return {
        "rebalance_date": sig_date,
        "exec_date": ex,
        "exec_estimated": ex_est,
        "next_rebalance": nxt,
        "next_estimated": nxt_est,
    }
