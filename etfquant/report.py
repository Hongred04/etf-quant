"""
HTML 研究报告生成器。

不依赖 matplotlib（规避 Windows 中文字体问题），图表全部用内联 SVG 绘制，
输出为单文件、可离线打开、可直接打印的 HTML。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# 配色（深色终端风格）
# --------------------------------------------------------------------------- #
C = {
    "bg": "#0f1218",
    "panel": "#161b24",
    "panel2": "#1b2130",
    "line": "#2a3244",
    "text": "#e6ecf5",
    "muted": "#8b98ad",
    "red": "#f6465d",
    "green": "#2ebd85",
    "blue": "#4c9aff",
    "amber": "#f0b90b",
    "purple": "#a78bfa",
    "cyan": "#22d3ee",
}
SERIES_COLORS = [C["blue"], C["amber"], C["purple"], C["cyan"], C["red"], C["green"]]


def _fmt_pct(x: float, digits: int = 2) -> str:
    if x is None or not np.isfinite(x):
        return "-"
    return f"{x * 100:.{digits}f}%"


def _fmt_num(x: float, digits: int = 2) -> str:
    if x is None or not np.isfinite(x):
        return "-"
    return f"{x:.{digits}f}"


# --------------------------------------------------------------------------- #
# 图表
# --------------------------------------------------------------------------- #
def svg_line_chart(
    df: pd.DataFrame,
    width: int = 980,
    height: int = 400,
    log_scale: bool = True,
    title: str = "",
    y_label: str = "净值",
) -> str:
    """多序列折线图。df: index=日期, columns=序列名。"""
    if df.empty:
        return "<p>无数据</p>"
    pad_l, pad_r, pad_t, pad_b = 62, 120, 34, 40
    iw, ih = width - pad_l - pad_r, height - pad_t - pad_b

    data = df.dropna(how="all").ffill()
    if log_scale:
        data = data.where(data > 0)
        y = np.log10(data)
    else:
        y = data

    ymin, ymax = np.nanmin(y.values), np.nanmax(y.values)
    if ymax - ymin < 1e-9:
        ymax = ymin + 0.01
    span = ymax - ymin
    ymin -= span * 0.06
    ymax += span * 0.06

    n = len(data)
    xs = np.linspace(0, iw, n)

    def xpos(i: int) -> float:
        return pad_l + xs[i]

    def ypos(v: float) -> float:
        return pad_t + ih - (v - ymin) / (ymax - ymin) * ih

    # 网格与 Y 轴刻度
    grid = []
    yticks = np.linspace(ymin, ymax, 6)
    for v in yticks:
        yy = ypos(v)
        label = f"{10 ** v:.2f}" if log_scale else f"{v:.2f}"
        grid.append(
            f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{pad_l + iw}" y2="{yy:.1f}" '
            f'stroke="{C["line"]}" stroke-width="1"/>'
        )
        grid.append(
            f'<text x="{pad_l - 10}" y="{yy + 4:.1f}" fill="{C["muted"]}" font-size="11" '
            f'text-anchor="end">{label}</text>'
        )

    # X 轴日期标签
    n_ticks = 7
    xlab = []
    for k in range(n_ticks):
        i = int(round(k * (n - 1) / (n_ticks - 1)))
        d = data.index[i]
        xlab.append(
            f'<text x="{xpos(i):.1f}" y="{pad_t + ih + 20}" fill="{C["muted"]}" font-size="11" '
            f'text-anchor="middle">{d.strftime("%Y-%m")}</text>'
        )

    # 曲线
    paths, legend = [], []
    for idx, col in enumerate(data.columns):
        s = y[col].to_numpy(dtype=float)
        color = SERIES_COLORS[idx % len(SERIES_COLORS)]
        pts = [(xpos(i), ypos(s[i])) for i in range(n) if np.isfinite(s[i])]
        if len(pts) < 2:
            continue
        d = "M " + " L ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
        paths.append(
            f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.9" '
            f'stroke-linejoin="round" opacity="0.95"/>'
        )
        ly = pad_t + 12 + idx * 20
        legend.append(
            f'<line x1="{width - pad_r + 10}" y1="{ly}" x2="{width - pad_r + 32}" y2="{ly}" '
            f'stroke="{color}" stroke-width="2.5"/>'
            f'<text x="{width - pad_r + 38}" y="{ly + 4}" fill="{C["text"]}" font-size="12">'
            f'{col}</text>'
        )

    return f"""<svg viewBox="0 0 {width} {height}" width="100%" xmlns="http://www.w3.org/2000/svg"
  style="background:{C['panel']};border-radius:10px">
  <text x="{pad_l}" y="20" fill="{C['text']}" font-size="13" font-weight="600">{title}</text>
  {''.join(grid)}
  <line x1="{pad_l}" y1="{pad_t + ih}" x2="{pad_l + iw}" y2="{pad_t + ih}" stroke="{C['line']}"/>
  {''.join(paths)}
  {''.join(xlab)}
  {''.join(legend)}
</svg>"""


def svg_drawdown(nav: pd.Series, bench: pd.Series | None = None, width: int = 980, height: int = 210) -> str:
    """回撤填充图。"""
    pad_l, pad_r, pad_t, pad_b = 62, 120, 26, 32
    iw, ih = width - pad_l - pad_r, height - pad_t - pad_b
    dd = nav / nav.cummax() - 1.0

    series = {"策略回撤": dd}
    if bench is not None and len(bench) > 2:
        b = bench.reindex(nav.index).ffill()
        series["基准回撤"] = b / b.cummax() - 1.0

    ymin = min(s.min() for s in series.values())
    ymin = min(ymin, -0.02) * 1.1
    ymax = 0.02
    n = len(dd)
    xs = np.linspace(0, iw, n)

    def ypos(v):
        return pad_t + ih - (v - ymin) / (ymax - ymin) * ih

    parts, legend = [], []
    for idx, (name, s) in enumerate(series.items()):
        color = C["red"] if idx == 0 else C["muted"]
        vals = s.to_numpy(dtype=float)
        pts = [(pad_l + xs[i], ypos(vals[i])) for i in range(n) if np.isfinite(vals[i])]
        if not pts:
            continue
        line = "M " + " L ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
        if idx == 0:
            area = line + f" L {pts[-1][0]:.1f},{ypos(0):.1f} L {pts[0][0]:.1f},{ypos(0):.1f} Z"
            parts.append(f'<path d="{area}" fill="{color}" opacity="0.22"/>')
        parts.append(f'<path d="{line}" fill="none" stroke="{color}" stroke-width="1.4"/>')
        ly = pad_t + 12 + idx * 18
        legend.append(
            f'<text x="{width - pad_r + 12}" y="{ly}" fill="{color}" font-size="11">{name} '
            f'{s.min() * 100:.1f}%</text>'
        )

    grid = []
    for v in np.linspace(ymin, 0, 4):
        yy = ypos(v)
        grid.append(
            f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{pad_l + iw}" y2="{yy:.1f}" stroke="{C["line"]}"/>'
            f'<text x="{pad_l - 10}" y="{yy + 4:.1f}" fill="{C["muted"]}" font-size="11" '
            f'text-anchor="end">{v * 100:.0f}%</text>'
        )

    return f"""<svg viewBox="0 0 {width} {height}" width="100%" xmlns="http://www.w3.org/2000/svg"
  style="background:{C['panel']};border-radius:10px">
  <text x="{pad_l}" y="18" fill="{C['text']}" font-size="12" font-weight="600">回撤曲线</text>
  {''.join(grid)}{''.join(parts)}{''.join(legend)}
</svg>"""


def svg_annual_bars(tbl: pd.DataFrame, width: int = 980, height: int = 300) -> str:
    """年度收益分组柱状图。tbl: index=年份, columns=策略。"""
    pad_l, pad_r, pad_t, pad_b = 56, 120, 30, 34
    iw, ih = width - pad_l - pad_r, height - pad_t - pad_b
    vals = tbl.to_numpy(dtype=float)
    vmax = np.nanmax(np.abs(vals)) * 1.15 or 0.1
    n_years, n_s = tbl.shape
    group_w = iw / max(n_years, 1)
    bar_w = group_w * 0.78 / max(n_s, 1)

    def ypos(v):
        return pad_t + ih / 2 - v / vmax * (ih / 2)

    parts = []
    for yi in range(n_years):
        for si in range(n_s):
            v = vals[yi, si]
            if not np.isfinite(v):
                continue
            x = pad_l + yi * group_w + group_w * 0.11 + si * bar_w
            y0, y1 = ypos(0), ypos(v)
            top, h = min(y0, y1), abs(y1 - y0)
            color = SERIES_COLORS[si % len(SERIES_COLORS)]
            parts.append(
                f'<rect x="{x:.1f}" y="{top:.1f}" width="{bar_w * 0.86:.1f}" height="{max(h, 1):.1f}" '
                f'fill="{color}" opacity="0.88" rx="1.5"/>'
            )
        parts.append(
            f'<text x="{pad_l + yi * group_w + group_w / 2:.1f}" y="{pad_t + ih + 20}" '
            f'fill="{C["muted"]}" font-size="11" text-anchor="middle">{tbl.index[yi]}</text>'
        )

    grid = [
        f'<line x1="{pad_l}" y1="{ypos(0):.1f}" x2="{pad_l + iw}" y2="{ypos(0):.1f}" '
        f'stroke="{C["line"]}" stroke-width="1.2"/>'
    ]
    for v in (-vmax, vmax):
        grid.append(
            f'<text x="{pad_l - 8}" y="{ypos(v) + 4:.1f}" fill="{C["muted"]}" font-size="10" '
            f'text-anchor="end">{v * 100:.0f}%</text>'
        )
    legend = "".join(
        f'<text x="{width - pad_r + 12}" y="{pad_t + 12 + i * 18}" fill="{SERIES_COLORS[i % len(SERIES_COLORS)]}" '
        f'font-size="11">{c}</text>'
        for i, c in enumerate(tbl.columns)
    )

    return f"""<svg viewBox="0 0 {width} {height}" width="100%" xmlns="http://www.w3.org/2000/svg"
  style="background:{C['panel']};border-radius:10px">
  <text x="{pad_l}" y="20" fill="{C['text']}" font-size="12" font-weight="600">年度收益对比</text>
  {''.join(grid)}{''.join(parts)}{legend}
</svg>"""


def svg_heatmap(tbl: pd.DataFrame, width: int = 980, height: int = 240) -> str:
    """月度收益热力图。tbl: index=年份, columns=月份。"""
    rows, cols = tbl.shape
    pad_l = 52
    pad_t = 34
    cw = (width - 60) / max(cols, 1)
    ch = (height - pad_t - 20) / max(rows, 1)
    cells, labels = [], []
    vmax = np.nanmax(np.abs(tbl.to_numpy(dtype=float))) or 0.1
    for i in range(rows):
        labels.append(
            f'<text x="{pad_l - 10}" y="{pad_t + i * ch + ch / 2 + 4:.1f}" fill="{C["muted"]}" '
            f'font-size="11" text-anchor="end">{tbl.index[i]}</text>'
        )
        for j in range(cols):
            v = tbl.iloc[i, j]
            if not np.isfinite(v):
                continue
            a = min(abs(v) / vmax, 1.0) * 0.85 + 0.08
            color = C["red"] if v >= 0 else C["green"]
            cells.append(
                f'<rect x="{pad_l + j * cw:.1f}" y="{pad_t + i * ch:.1f}" width="{cw - 2:.1f}" '
                f'height="{ch - 2:.1f}" fill="{color}" opacity="{a:.2f}" rx="2"/>'
                f'<text x="{pad_l + j * cw + cw / 2 - 1:.1f}" y="{pad_t + i * ch + ch / 2 + 4:.1f}" '
                f'fill="{C["text"]}" font-size="9" text-anchor="middle">{v * 100:.0f}</text>'
            )
    head = "".join(
        f'<text x="{pad_l + j * cw + cw / 2 - 1:.1f}" y="{pad_t - 10}" fill="{C["muted"]}" '
        f'font-size="10" text-anchor="middle">{m}月</text>'
        for j, m in enumerate(tbl.columns)
    )
    return f"""<svg viewBox="0 0 {width} {height}" width="100%" xmlns="http://www.w3.org/2000/svg"
  style="background:{C['panel']};border-radius:10px">
  <text x="{pad_l}" y="18" fill="{C['text']}" font-size="12" font-weight="600">TGAR 月度收益热力图（%）</text>
  {head}{''.join(labels)}{''.join(cells)}
</svg>"""


# --------------------------------------------------------------------------- #
# 表格与页面
# --------------------------------------------------------------------------- #
def df_to_html(df: pd.DataFrame, pct_cols: tuple[str, ...] = (), num_cols: tuple[str, ...] = (),
               show_index: bool = True) -> str:
    """把 DataFrame 渲染为带样式的表格。"""
    head = "".join(f"<th>{c}</th>" for c in df.columns)
    body = []
    for ridx, row in df.iterrows():
        tds = []
        for c in df.columns:
            v = row[c]
            if c in pct_cols and isinstance(v, (int, float, np.floating)):
                txt = _fmt_pct(float(v))
                cls = "pos" if v >= 0 else "neg"
            elif c in num_cols and isinstance(v, (int, float, np.floating)):
                txt = _fmt_num(float(v))
                cls = ""
            else:
                txt = str(v)
                cls = ""
            tds.append(f'<td class="{cls}">{txt}</td>')
        rh = f"<th class='rh'>{ridx}</th>" if show_index else ""
        body.append(f"<tr>{rh}{''.join(tds)}</tr>")
    empty = "<th></th>" if show_index else ""
    return f"<table><thead><tr>{empty}{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


CSS = f"""
* {{ box-sizing: border-box; }}
body {{
  margin: 0; background: {C['bg']}; color: {C['text']};
  font-family: -apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
  font-size: 14px; line-height: 1.75; -webkit-font-smoothing: antialiased;
}}
.wrap {{ max-width: 1080px; margin: 0 auto; padding: 40px 28px 80px; }}
h1 {{ font-size: 27px; margin: 0 0 6px; letter-spacing: -0.3px; }}
h2 {{ font-size: 19px; margin: 44px 0 14px; padding-left: 11px;
      border-left: 3px solid {C['blue']}; }}
h3 {{ font-size: 15px; margin: 26px 0 10px; color: {C['cyan']}; }}
p, li {{ color: #c3ccda; }}
.sub {{ color: {C['muted']}; font-size: 13px; margin-bottom: 26px; }}
.kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin: 22px 0; }}
.kpi {{ background: {C['panel']}; border: 1px solid {C['line']}; border-radius: 10px; padding: 14px 16px; }}
.kpi .v {{ font-size: 22px; font-weight: 650; letter-spacing: -0.5px; }}
.kpi .l {{ font-size: 12px; color: {C['muted']}; margin-top: 3px; }}
.pos {{ color: {C['red']}; }}
.neg {{ color: {C['green']}; }}
.badge {{ display: inline-block; padding: 2px 9px; border-radius: 20px; font-size: 12px;
          background: rgba(76,154,255,.14); color: {C['blue']}; border: 1px solid rgba(76,154,255,.3); }}
table {{ width: 100%; border-collapse: collapse; margin: 14px 0 8px; font-size: 13px;
         background: {C['panel']}; border-radius: 10px; overflow: hidden; }}
th, td {{ padding: 9px 12px; text-align: right; border-bottom: 1px solid {C['line']}; }}
th {{ background: {C['panel2']}; color: {C['muted']}; font-weight: 600; font-size: 12px; }}
td {{ text-align: right; font-variant-numeric: tabular-nums; }}
td:first-child, th:first-child {{ text-align: left; white-space: nowrap; padding-right: 18px; min-width: 76px; }}
th.rh {{ text-align: left; color: {C['text']}; font-weight: 600; }}
tbody tr:last-child td {{ border-bottom: none; }}
tbody tr:hover td {{ background: rgba(255,255,255,.025); }}
.note {{ background: {C['panel']}; border-left: 3px solid {C['amber']}; padding: 12px 16px;
         border-radius: 0 8px 8px 0; margin: 16px 0; font-size: 13px; color: #c3ccda; }}
.grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
.card {{ background: {C['panel']}; border: 1px solid {C['line']}; border-radius: 10px; padding: 16px 18px; }}
.card h4 {{ margin: 0 0 8px; font-size: 13px; color: {C['amber']}; }}
ol, ul {{ padding-left: 22px; }}
code {{ background: {C['panel2']}; padding: 1.5px 6px; border-radius: 4px; font-size: 12.5px;
        color: {C['cyan']}; font-family: Consolas, Monaco, monospace; }}
.toc a {{ color: {C['blue']}; text-decoration: none; }}
.foot {{ margin-top: 60px; padding-top: 18px; border-top: 1px solid {C['line']};
         color: {C['muted']}; font-size: 12px; }}
"""


def render_page(title: str, subtitle: str, body: str, footer: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{CSS}</style></head>
<body><div class="wrap">
<h1>{title}</h1><div class="sub">{subtitle}</div>
{body}
<div class="foot">{footer}</div>
</div></body></html>"""
