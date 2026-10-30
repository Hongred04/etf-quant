"""
ETF 标的池构建（Universe Construction）。

三条流水线：
  1) discover_etfs()  —— 枚举沪深两市 ETF 代码段，批量快照确认真实存在；
  2) classify_etf()   —— 依据名称关键词做资产类别映射（宽基/行业/主题/债券/商品/跨境/货币）；
  3) build_universe() —— 施加流动性、规模、历史长度约束，输出可交易池。

代码段依据交易所编码规则：
  沪市：510-519（股票/债券/跨境/商品 ETF）、560-564（行业主题 ETF）、588-589（科创板 ETF）
  深市：159xxx
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from .datafeed import fetch_snapshot

# (交易所, 起始代码, 结束代码(不含))
CODE_SEGMENTS: list[tuple[str, int, int]] = [
    ("sh", 510000, 520000),
    ("sh", 560000, 565000),
    ("sh", 588000, 590000),
    ("sz", 159000, 160000),
]

# 关键词 -> 资产大类（顺序敏感，先匹配先生效）
CATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("货币", ["货币", "现金", "保证金", "日利", "添益", "短融", "快线", "理财", "添利",
              "财富宝", "收益宝", "现金宝", "日增利", "保证金"]),
    ("转债", ["转债"]),
    ("债券", ["债", "国债", "政金", "信用债", "城投"]),
    ("商品", ["黄金", "白银", "豆粕", "原油", "能源化工", "有色期货", "商品"]),
    (
        "跨境",
        ["纳指", "纳斯达克", "标普", "道琼斯", "恒生", "港股", "H股", "中概", "日经",
         "德国", "法国", "东南亚", "美国", "海外", "亚太", "越南", "沙特", "英国", "印度"],
    ),
    (
        "宽基",
        ["沪深300", "中证500", "中证1000", "中证2000", "上证50", "上证180", "科创50",
         "科创100", "科创200", "创业板", "深证100", "中证100", "A50", "A500", "A100",
         "上证综指", "深证成指", "北证50", "MSCI", "全指", "综指", "龙头"],
    ),
    (
        "行业",
        ["证券", "银行", "保险", "非银", "医药", "医疗", "生物", "白酒", "食品", "饮料",
         "消费", "家电", "汽车", "新能源", "光伏", "电池", "储能", "半导体", "芯片",
         "军工", "国防", "航天", "煤炭", "钢铁", "有色", "化工", "建材", "地产", "房地产",
         "基建", "电力", "公用", "环保", "农业", "养殖", "传媒", "游戏", "动漫", "影视",
         "计算机", "软件", "通信", "电子", "机械", "机器人", "旅游", "酒店", "交运",
         "物流", "港口", "稀土", "锂电", "氢能", "电网", "石油", "油气"],
    ),
]

# 防守型资产（用于 RISK_OFF 状态下的避风港）
DEFENSIVE_CATEGORIES = {"债券", "货币"}


def iter_candidate_tickers() -> list[str]:
    """生成全部候选 ETF 代码（约 1.8 万个）。"""
    out: list[str] = []
    for market, lo, hi in CODE_SEGMENTS:
        out.extend(f"{market}{i:06d}" for i in range(lo, hi))
    return out


def dedup_key(name: str) -> str:
    """
    同质化去重键：截取名称中 "ETF" 及其之前的部分。

    "沪深300ETF华泰柏瑞" 与 "沪深300ETF华夏" 视为同一标的（跟踪同一指数），
    仅保留成交额最大的那只，避免回测中同指数重复暴露虚增分散度。
    """
    pos = name.find("ETF")
    if pos < 0:
        return name
    return name[: pos + 3]


def dedup_same_index(df: pd.DataFrame, amount_col: str = "amount_yuan") -> pd.DataFrame:
    """同一指数/主题只保留成交额最大的一只 ETF。"""
    if df.empty:
        return df
    d = df.copy()
    d["_key"] = d["name"].map(dedup_key)
    d = d.sort_values(amount_col, ascending=False)
    d = d.drop_duplicates(subset="_key", keep="first")
    return d.drop(columns="_key")


def discover_etfs(min_price: float = 0.01, min_amount_yuan: float = 1e7) -> pd.DataFrame:
    """
    扫描全市场 ETF。

    判定条件：名称含 ETF 字样、价格有效、成交额达标。
    返回列：ticker/name/price/pct_chg/amount_yuan/turnover_pct/float_mv_yi/category
    """
    tickers = iter_candidate_tickers()
    snap = fetch_snapshot(tickers, batch=60)
    if snap.empty:
        raise RuntimeError("快照抓取失败，请检查网络连通性")

    snap = snap[snap["name"].str.contains("ETF", na=False)]
    snap = snap[snap["price"] > min_price]
    snap = snap[snap["amount_yuan"].fillna(0) >= min_amount_yuan]
    snap = snap.drop_duplicates(subset="ticker").sort_values("amount_yuan", ascending=False)
    snap["category"] = snap["name"].map(classify_etf)
    return snap.reset_index(drop=True)


def classify_etf(name: str) -> str:
    """按名称关键词做资产类别映射。"""
    for label, kws in CATEGORY_RULES:
        if any(kw in name for kw in kws):
            return label
    return "主题"


def is_defensive(category: str) -> bool:
    return category in DEFENSIVE_CATEGORIES


def build_universe(
    snap: pd.DataFrame,
    price_panel: pd.DataFrame,
    min_hist_days: int = 250,
    min_avg_amount: float = 2e7,
    lookback: int = 60,
    exclude_categories: tuple[str, ...] = ("货币",),
    max_size: int | None = 160,
) -> pd.DataFrame:
    """
    在快照基础上叠加历史数据约束，产出最终可交易池。

    参数
    ----
    min_hist_days : 最少历史交易日（保证 200 日均线等长周期因子可计算）
    min_avg_amount: 近 `lookback` 日平均成交额下限（元）
    max_size      : 按日均成交额截取前 N 只，控制回测计算量
    """
    if price_panel.empty:
        raise ValueError("price_panel 为空")

    hist_days = price_panel.notna().sum()
    recent = price_panel.tail(lookback)

    # 用成交额代理：以价格矩阵无法得到成交额，改由快照的成交额做二次校验
    amount_col = "amount_yuan"
    uni = snap.copy()
    uni["hist_days"] = uni["ticker"].map(hist_days).fillna(0).astype(int)
    uni = uni[uni["ticker"].isin(price_panel.columns)]
    uni = uni[uni["hist_days"] >= min_hist_days]
    uni = uni[~uni["category"].isin(exclude_categories)]
    uni = uni[uni[amount_col] >= min_avg_amount]
    uni = uni.sort_values(amount_col, ascending=False)
    if max_size:
        uni = uni.head(max_size)
    return uni.reset_index(drop=True)


def universe_summary(uni: pd.DataFrame) -> pd.DataFrame:
    """按类别汇总标的数量与成交额。"""
    g = uni.groupby("category").agg(
        count=("ticker", "size"),
        amount_yi=("amount_yuan", lambda s: s.sum() / 1e8),
    )
    return g.sort_values("amount_yi", ascending=False).round(2)


# --------------------------------------------------------------------------- #
# 运行时池子口径：按"是不是纯大盘 beta"切
# --------------------------------------------------------------------------- #
# 背景：TGAR 的 alpha 来自横截面主题动量，而沪深300/上证50/中证500 本质是同一个
# 大盘 beta —— 它已经由市场状态闸门（regime）管着了。把它们放进打分池，等于对
# 同一份大盘暴露收两次费。
#
# ⚠️ 这里记录一次**自我纠错**，避免后来者重走弯路：
# 最初的对照实验显示"剔除大盘宽基"能把 CAGR 从 17.27% 提到 20.46%，但那个实验
# 是**把宽基列整列删掉**做的，而沪深300 恰好也在被删之列；`_bench_series()` 在
# 基准缺失时会退化为"宽基等权代理"，宽基全被删后更退化为"全池等权"——
# 于是那次实验实际测的是"换了一个闸门判据"，不是"换池子"。
#
# 用本模块的 tradable 标记（基准列保留、只是不参与选股）重做后：
#     全池 260 只            CAGR 17.27%  MaxDD -28.86%  Sharpe 0.69
#     剔除大盘宽基 23 只      CAGR 16.74%  MaxDD -29.47%  Sharpe 0.67
# 结论：**剔除宽基没有效果，略差**。所以 POOL_MODES["core"] 不作为推荐默认值，
# 仅保留为可复现的实验开关。闸门判据那条线索见 strategy.gate_mode。
#
BROAD_KEYWORDS = (
    "沪深300", "上证50", "上证180", "上证综指",
    "中证500", "中证1000", "中证2000",
    "中证A500", "中证A50", "A500", "A50", "MSCIA股",
    "深证100", "深证成指", "XDA500",
)

POOL_MODES = {
    "full": "全部标的（历史口径）",
    "core": "剔除纯大盘宽基（实验：实测无改善）",
}


def is_broad_beta(name: str, category: str) -> bool:
    """是否属于"纯大盘 beta"。创业板 / 科创 不算。"""
    if category != "宽基":
        return False
    return any(k in str(name) for k in BROAD_KEYWORDS)


def apply_pool(panel, mode: str = "full"):
    """
    按池子口径裁剪 Panel：**不删列，只给 meta 打 ``tradable=False``**。

    为什么不直接删列：市场状态闸门（regime）要用沪深300 的收盘价算趋势，
    把 510300 从 close 里删掉，``self.benchmark`` 会变成 None，闸门静默退回满仓——
    回测照样出净值，只是策略变成了另一个东西。这个坑很难从结果上看出来，
    所以改成"留在表里、不参与选股"。
    """
    from .strategy import Panel

    if mode == "full":
        return panel
    if mode not in POOL_MODES:
        raise ValueError(f"未知池子口径：{mode}（可选 {list(POOL_MODES)}）")
    if "name" not in panel.meta.columns or "category" not in panel.meta.columns:
        return panel

    meta = panel.meta.copy()
    meta["tradable"] = [
        not is_broad_beta(meta.loc[c, "name"], meta.loc[c, "category"])
        for c in meta.index
    ]
    return Panel(
        close=panel.close, open_=panel.open_, high=panel.high, low=panel.low,
        volume=panel.volume, meta=meta,
    )
