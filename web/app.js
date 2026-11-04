/* ============================================================
   ETF Quant · Web Console 前端
   纯原生 JS，无构建、无外部依赖
   ============================================================ */

(() => {
  "use strict";

  /* ------------------------------------------------------- 常量 */
  const SELF = "S4-TGAR(自研)";
  const BENCH = "基准-沪深300";

  /* 内部代号 → 人话。数据 key 不能动（图表配色、CSV 列都靠它），
     但用户没必要知道"S4-TGAR 自研"是什么——他只需要知道"这是现在这套"、
     "这是只看涨幅"、"这是直接买指数"。 */
  const PRETTY = {
    "S1-动量基线": "只看涨幅",
    "S2-双动量": "加趋势过滤",
    "S3-多因子": "多指标合成",
    "S4-TGAR(自研)": "现在这套",
    "基准-沪深300": "直接买沪深300",
  };
  const pretty = (n) => PRETTY[n] || n;

  /* 颜色一律从 CSS 变量取，切换主题后重绘即可换肤 */
  const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

  const SERIES_VAR = {
    "S1-动量基线": "--chart-s1",
    "S2-双动量": "--chart-s2",
    "S3-多因子": "--chart-s3",
    "S4-TGAR(自研)": "--chart-s4",
    "基准-沪深300": "--chart-bench",
  };
  const seriesColor = (name) => cssVar(SERIES_VAR[name] || "--chart-bench");

  const CAT_VAR = {
    宽基: "--cat-broad", 行业: "--cat-industry", 主题: "--cat-theme",
    跨境: "--cat-overseas", 商品: "--cat-commodity", 债券: "--cat-bond", 转债: "--cat-convert",
  };
  const catColor = (n) => cssVar(CAT_VAR[n] || "--chart-s1");

  /* 页面标题即导航文案：用"用户在问什么"命名，而不是用内部模块名。
     hash 不变（#signal/#track…），所以外部书签与冒烟用例都不受影响。 */
  const PAGES = {
    overview: { title: "总览", sub: "" },
    signal: { title: "买什么", sub: "" },
    track: { title: "跟单记录", sub: "每期信号都记着，按它买会怎样" },
    myholdings: { title: "我的持仓", sub: "" },
    portfolio: { title: "模拟账户", sub: "一路按信号买下来，账户会变成什么样" },
    research: { title: "靠不靠谱", sub: "" },
    data: { title: "数据", sub: "" },
  };

  const state = {
    page: "overview",
    overview: null,
    signal: null,
    myholdings: null,
    track: null,
    research: null,
    universe: null,
    portfolio: null,
    hidden: new Set(),
    charts: [],
    task: null,
    lastTask: null,        // 最近一次结束的任务：失败后点顶栏胶囊看日志用
    status: null,
    pollTimer: null,
    pendingAction: null,
  };

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const view = $("#view");

  /* ------------------------------------------------------- 工具 */
  const pct = (v, d = 2) => (v == null || Number.isNaN(v) ? "—" : `${(v * 100).toFixed(d)}%`);
  const pctRaw = (v, d = 2) => (v == null || Number.isNaN(v) ? "—" : `${v.toFixed(d)}%`);
  const num = (v, d = 2) => (v == null || Number.isNaN(v) ? "—" : v.toFixed(d));
  const cls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
  /* 本地日期 YYYY-MM-DD。不能用 toISOString——那是 UTC，早上 8 点前会差一天。 */
  const todayLocal = () => {
    const d = new Date();
    const p2 = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}`;
  };
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));

  function el(html) {
    const t = document.createElement("template");
    t.innerHTML = html.trim();
    return t.content.firstElementChild;
  }

  function toast(msg, kind = "ok", ms = 3200) {
    const node = el(`<div class="toast ${kind}">${esc(msg)}</div>`);
    $("#toasts").appendChild(node);
    setTimeout(() => {
      node.style.transition = "opacity .3s, transform .3s";
      node.style.opacity = "0";
      node.style.transform = "translateX(16px)";
      setTimeout(() => node.remove(), 320);
    }, Math.max(1200, ms));
  }

  function countUp(node, target, fmt) {
    const dur = 620;
    const t0 = performance.now();
    const from = 0;
    function tick(t) {
      const k = Math.min(1, (t - t0) / dur);
      const e = 1 - Math.pow(1 - k, 3);
      node.textContent = fmt(from + (target - from) * e);
      if (k < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  }

  async function api(path, opts = {}) {
    // 加超时：服务端某个接口要现算（portfolio 约 1.6s），但也可能因故卡住，
    // 没有超时会让页面永远停在骨架屏上。
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 30000);
    let res;
    try {
      res = await fetch(path, { ...opts, signal: ctrl.signal });
    } catch (e) {
      clearTimeout(timer);
      throw new Error(e.name === "AbortError" ? "请求超时（30 秒）" : e.message);
    }
    clearTimeout(timer);
    if (!res.ok) {
      // 服务端会在 body 里给出中文原因（如"已有任务正在执行"），优先展示它
      let msg = `${res.status} ${res.statusText}`;
      try {
        const body = await res.json();
        if (body && body.error) msg = body.error;
      } catch (e) { /* 非 JSON 错误页，保留状态码 */ }
      throw new Error(msg);
    }
    return res.json();
  }

  /* ------------------------------------------------------- 图表引擎 */
  function setupCanvas(canvas, cssH) {
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.parentElement.clientWidth;
    canvas.style.height = cssH + "px";
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(cssH * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, w, h: cssH };
  }

  function niceTicks(min, max, count = 5) {
    if (min === max) return [min];
    const span = max - min;
    const step0 = span / count;
    const mag = Math.pow(10, Math.floor(Math.log10(step0)));
    const norm = step0 / mag;
    const step = (norm >= 7.5 ? 10 : norm >= 3.5 ? 5 : norm >= 1.5 ? 2 : 1) * mag;
    const start = Math.ceil(min / step) * step;
    const out = [];
    for (let v = start; v <= max + step * 0.001; v += step) out.push(v);
    return out;
  }

  /**
   * 多序列折线图
   * cfg: { series:[{name,color,data:[[date,val]]}], log, yFmt, area, height }
   */
  function lineChart(canvas, cfg) {
    const H = cfg.height || 320;
    const PAD = { l: 54, r: 14, t: 14, b: 26 };

    function render(hover) {
      const { ctx, w, h } = setupCanvas(canvas, H);
      ctx.clearRect(0, 0, w, h);

      const visible = cfg.series.filter((s) => !state.hidden.has(s.name) && s.data.length);
      if (!visible.length) {
        ctx.fillStyle = cssVar("--chart-label");
        ctx.font = "12px Segoe UI, sans-serif";
        ctx.textAlign = "center";
        ctx.fillText("全部序列已隐藏", w / 2, h / 2);
        return;
      }

      const n = visible[0].data.length;
      const iw = w - PAD.l - PAD.r;
      const ih = h - PAD.t - PAD.b;

      // Y 值域
      let lo = Infinity, hi = -Infinity;
      for (const s of visible) {
        for (const [, v] of s.data) {
          const y = cfg.log ? Math.log10(Math.max(v, 1e-6)) : v;
          if (y < lo) lo = y;
          if (y > hi) hi = y;
        }
      }
      if (!isFinite(lo)) { lo = 0; hi = 1; }
      const padY = (hi - lo) * 0.08 || 0.02;
      lo -= padY; hi += padY;
      if (cfg.zeroBase) lo = Math.min(lo, 0);

      const xOf = (i) => PAD.l + (n === 1 ? iw / 2 : (i / (n - 1)) * iw);
      const yOf = (v) => {
        const y = cfg.log ? Math.log10(Math.max(v, 1e-6)) : v;
        return PAD.t + ih - ((y - lo) / (hi - lo)) * ih;
      };

      // 网格 + Y 轴
      ctx.font = "10px Consolas, monospace";
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      let yt;
      if (cfg.log) {
        // 用"整数净值"序列，读起来更自然（1.0 / 1.5 / 2 / 3 / 4 / 6 ...）
        const cands = [0.3, 0.5, 0.8, 1, 1.5, 2, 3, 4, 5, 6, 8, 10, 15, 20, 30, 50];
        const loV = Math.pow(10, lo), hiV = Math.pow(10, hi);
        yt = cands.filter((v) => v >= loV * 0.999 && v <= hiV * 1.001).map((v) => Math.log10(v));
        if (yt.length < 2) yt = niceTicks(lo, hi, 4);
      } else {
        yt = niceTicks(lo, hi, 5);
      }
      for (const v of yt) {
        const y = PAD.t + ih - ((v - lo) / (hi - lo)) * ih;
        ctx.strokeStyle = cssVar("--chart-grid");
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(PAD.l, Math.round(y) + 0.5);
        ctx.lineTo(w - PAD.r, Math.round(y) + 0.5);
        ctx.stroke();
        ctx.fillStyle = cssVar("--chart-label");
        const label = cfg.log ? Math.pow(10, v).toFixed(2) : cfg.yFmt ? cfg.yFmt(v) : v.toFixed(1);
        ctx.fillText(label, PAD.l - 9, y);
      }

      // X 轴标签
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      const nx = Math.min(7, n);
      for (let k = 0; k < nx; k++) {
        const i = Math.round((k * (n - 1)) / Math.max(1, nx - 1));
        ctx.fillStyle = cssVar("--chart-label");
        const d = visible[0].data[i][0];
        // 首末标签贴边对齐，否则居中绘制会被画布裁掉半个（最后一个月份只剩 "2026-1"）
        ctx.textAlign = k === 0 ? "left" : k === nx - 1 ? "right" : "center";
        ctx.fillText(d.slice(0, 7), xOf(i), PAD.t + ih + 8);
      }
      ctx.textAlign = "center";

      // 序列
      for (const s of visible) {
        const pts = s.data.map(([, v], i) => [xOf(i), yOf(v)]);

        if (cfg.area) {
          const grad = ctx.createLinearGradient(0, PAD.t, 0, PAD.t + ih);
          grad.addColorStop(0, hexA(s.color, 0.26));
          grad.addColorStop(1, hexA(s.color, 0.01));
          ctx.beginPath();
          ctx.moveTo(pts[0][0], PAD.t + ih);
          for (const [x, y] of pts) ctx.lineTo(x, y);
          ctx.lineTo(pts[pts.length - 1][0], PAD.t + ih);
          ctx.closePath();
          ctx.fillStyle = grad;
          ctx.fill();
        }

        ctx.beginPath();
        pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
        ctx.strokeStyle = s.color;
        ctx.lineWidth = s.width || 1.8;
        ctx.lineJoin = "round";
        ctx.lineCap = "round";
        if (s.dash) ctx.setLineDash(s.dash); else ctx.setLineDash([]);
        ctx.stroke();
        ctx.setLineDash([]);
      }

      // hover 十字线
      if (hover != null && visible[0].data[hover]) {
        const x = xOf(hover);
        ctx.strokeStyle = cssVar("--txt-3");
        ctx.lineWidth = 1;
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(x, PAD.t);
        ctx.lineTo(x, PAD.t + ih);
        ctx.stroke();
        ctx.setLineDash([]);
        for (const s of visible) {
          const v = s.data[hover]?.[1];
          if (v == null) continue;
          const y = yOf(v);
          ctx.beginPath();
          ctx.arc(x, y, 3.6, 0, Math.PI * 2);
          ctx.fillStyle = s.color;
          ctx.fill();
          ctx.beginPath();
          ctx.arc(x, y, 6.5, 0, Math.PI * 2);
          ctx.strokeStyle = hexA(s.color, 0.35);
          ctx.lineWidth = 1.4;
          ctx.stroke();
        }
      }
    }

    canvas._render = render;
    render(null);

    // 交互
    const tip = $("#tooltip");
    const onMove = (ev) => {
      const rect = canvas.getBoundingClientRect();
      const x = ev.clientX - rect.left;
      const visible = cfg.series.filter((s) => !state.hidden.has(s.name) && s.data.length);
      if (!visible.length) return;
      const n = visible[0].data.length;
      const iw = rect.width - PAD.l - PAD.r;
      let i = Math.round(((x - PAD.l) / iw) * (n - 1));
      i = Math.max(0, Math.min(n - 1, i));
      render(i);

      const rows = visible.map((s) => {
        const v = s.data[i]?.[1];
        return `<div class="tt-row"><span class="tt-swatch" style="background:${s.color}"></span>
          <span class="tt-name">${esc(s.name)}</span>
          <span class="tt-val">${v == null ? "—" : cfg.tipFmt ? cfg.tipFmt(v) : v.toFixed(2)}</span></div>`;
      }).join("");
      tip.innerHTML = `<div class="tt-date">${visible[0].data[i][0]}</div>${rows}`;
      tip.hidden = false;
      const tw = tip.offsetWidth;
      let left = ev.clientX + 16;
      if (left + tw > window.innerWidth - 12) left = ev.clientX - tw - 16;
      tip.style.left = left + "px";
      tip.style.top = Math.max(12, ev.clientY - 20) + "px";
    };
    const onLeave = () => {
      tip.hidden = true;
      render(null);
    };
    canvas.addEventListener("mousemove", onMove);
    canvas.addEventListener("mouseleave", onLeave);

    state.charts.push({ canvas, render });
  }

  function hexA(hex, a) {
    const h = hex.replace("#", "");
    const r = parseInt(h.slice(0, 2), 16);
    const g = parseInt(h.slice(2, 4), 16);
    const b = parseInt(h.slice(4, 6), 16);
    return `rgba(${r},${g},${b},${a})`;
  }

  function redrawAll() {
    for (const c of state.charts) {
      try { c.render(null); } catch (e) { /* noop */ }
    }
  }

  window.addEventListener("resize", () => {
    clearTimeout(window.__rz);
    window.__rz = setTimeout(redrawAll, 140);
  });

  /* ------------------------------------------------------- 主题 */
  function currentTheme() {
    return document.documentElement.dataset.theme || "dark";
  }
  function applyTheme(t) {
    document.documentElement.dataset.theme = t;
    try { localStorage.setItem("etfquant-theme", t); } catch (e) { /* noop */ }
    setTimeout(redrawAll, 20); // DOM 由 CSS 换肤，canvas 需重绘
  }
  $("#btnTheme")?.addEventListener("click", () => {
    applyTheme(currentTheme() === "dark" ? "light" : "dark");
  });

  /* ------------------------------------------------------- 术语提示 */
  const termTip = $("#termTip");
  function showTermTip(target) {
    const html = target.dataset.tip;
    if (!html) return;
    termTip.innerHTML = html;
    termTip.hidden = false;
    const r = target.getBoundingClientRect();
    const tw = termTip.offsetWidth;
    const th = termTip.offsetHeight;
    let left = r.left + r.width / 2 - tw / 2;
    left = Math.max(10, Math.min(left, window.innerWidth - tw - 10));
    let top = r.top - th - 9;
    if (top < 8) top = r.bottom + 9;
    termTip.style.left = left + "px";
    termTip.style.top = top + "px";
  }
  function hideTermTip() { if (termTip) termTip.hidden = true; }
  document.addEventListener("mouseover", (e) => {
    const t = e.target.closest ? e.target.closest("[data-tip]") : null;
    if (t) showTermTip(t); else hideTermTip();
  });
  document.addEventListener("mouseout", (e) => {
    if (e.target.closest && e.target.closest("[data-tip]")) hideTermTip();
  });
  window.addEventListener("scroll", hideTermTip, { passive: true });

  /* ------------------------------------------------------- 通用组件 */
  function kpiCard(label, value, foot, tone = "", tip = "") {
    const lbl = tip
      ? `<span class="term" data-tip="${tip}">${esc(label)}</span>`
      : esc(label);
    return `<div class="kpi reveal">
      <div class="kpi-label">${lbl}</div>
      <div class="kpi-value ${tone}">${value}</div>
      <div class="kpi-foot">${foot}</div>
    </div>`;
  }

  function tableCard(title, sub, cols, rows, opts = {}) {
    const head = cols.map((c) => {
      const inner = c.tip ? `<span class="term" data-tip="${c.tip}">${esc(c.label)}</span>` : esc(c.label);
      return `<th>${inner}</th>`;
    }).join("");
    const body = rows.map((r) => {
      const tds = cols.map((c) => {
        const v = r[c.key];
        let text = v;
        let klass = c.align === "left" ? "" : "";
        if (typeof c.fmt === "function") {
          const out = c.fmt(v, r);
          text = out.text != null ? out.text : out;
          if (out.cls) klass = out.cls;
        }
        if (v == null || v === "") text = "—";
        return `<td class="${klass}">${text}</td>`;
      }).join("");
      return `<tr class="${opts.rowClass ? opts.rowClass(r) : ""}">${tds}</tr>`;
    }).join("");

    return `<div class="card reveal ${opts.cardClass || ""}">
      <div class="card-head">
        <div><div class="card-title">${esc(title)}</div>
        ${sub ? `<div class="card-sub">${sub}</div>` : ""}</div>
      </div>
      <div class="table-wrap"><table>
        <thead><tr>${head}</tr></thead>
        <tbody>${body || `<tr><td colspan="${cols.length}" class="empty">暂无数据</td></tr>`}</tbody>
      </table></div>
    </div>`;
  }

  /* ------------------------------------------------------- 页面：概览 */
  function renderOverview() {
    const d = state.overview;
    if (!d || d.error) {
      view.innerHTML = `<div class="empty">${esc(d?.error || "加载中…")}</div>`;
      return;
    }
    const self = d.perf.find((p) => p.name === SELF) || d.perf[0];
    const bench = d.perf.find((p) => p.name === BENCH) || {};
    const m = d.meta;
    const exp = d.expect;
    // 17.4% 是样本内结果：29 个变体在同一段历史上比过大小。可对外引用的区间
    // 由服务端按"滚动前向锚点"算好，这里只负责显示，不在前端写第二套换算。
    // 脚注只放"预期"一项：基准年化挪进提示里，六个 KPI 并排时脚注超过一行会换行。
    const cagrFoot = exp && exp.label ? `对外口径 ${exp.label}` : `沪深300 ${pct(bench.cagr ?? 0)}`;
    // 基准收益回撤比：用同一口径现算，缺数据时退回公式说明（不要写死 0.16）
    const benchCalmar = (bench.cagr != null && bench.mdd) ? bench.cagr / Math.abs(bench.mdd) : null;

    // 首屏第一件事回答「今天要不要操作」——历史战绩放第二屏。
    // 数据来自 /api/signal（boot 时已预取），取不到就不显示，不阻塞页面。
    const sig = state.signal;
    let strip = "";
    if (sig && !sig.error && Array.isArray(sig.rows) && sig.rows.length) {
      const tw = sig.rows.reduce((s, r) => s + (+r.share || 0), 0) || 1;
      const bondShare = sig.rows
        .filter((r) => r.category === "债券")
        .reduce((s, r) => s + (+r.share || 0), 0) / tw;
      // 组合口径下，"权益/债券"不足以描述仓位：底仓是不择时的红利低波，
      // 进攻仓才受大盘状态控制。这里按 leg 汇总，用户才知道哪部分会被换掉。
      const coreShare = sig.rows
        .filter((r) => r.leg === "core")
        .reduce((s, r) => s + (+r.share || 0), 0) / tw;
      const STATE = {
        holding: ["ok", "持有中 · 今天不用操作", `下次换仓 ${sig.next_reb || "—"} 收盘后再来`],
        exec_today: ["act", "今天开盘执行调仓", "先买债券，再按占比从大到小"],
        pending: ["wait", `${sig.exec_date || "—"} 开盘执行`, "清单已生成，等开盘"],
        today: ["due", "今天收盘后更新数据", "明天开盘按新清单换仓"],
        due: ["due", "清单已过期", "先更新行情并重算，否则不要按旧表下单"],
      };
      const [tone, title, sub] = STATE[sig.status] || ["wait", "清单待更新", "点右侧「更新行情并重算」"];
      strip = `<div class="status-strip is-${tone}">
        <span class="ss-dot"></span>
        <b>${esc(title)}</b>
        <span class="ss-sub">${esc(sub)}</span>
        <span class="ss-pos">${coreShare > 0.01
          ? `底仓 ${pct(coreShare, 0)} · 进攻 ${pct(1 - coreShare, 0)}（其中债券 ${pct(bondShare, 0)}）`
          : `权益 ${pct(1 - bondShare, 0)} · 债券 ${pct(bondShare, 0)}`}</span>
        <a class="ss-link" href="#signal">查看清单 →</a>
      </div>`;
    }

    // 当前回撤必须用**全量**净值算：drawdown 序列是降采样的，cummax 会漏掉
    // 被跳过的峰值，导致回撤被低估（实测差 7pp：-13.8% vs 真实的 -21.0%）。
    const uw = d.underwater && d.underwater.self;
    const ddSeries = (d.drawdown && d.drawdown.self) || [];
    const ddNow = uw ? uw.current_dd : (ddSeries.length ? ddSeries[ddSeries.length - 1][1] / 100 : null);
    // 水下时长：跌多深之外的第二维。回撤 20% 拖三年，比回撤 30% 六个月恢复更难受。
    const uwNow = uw && uw.current_days > 0
      ? `水下 ${uw.current_days} 天 · 需涨 ${pct(uw.recovery_gain, 1)}`
      : (uw ? "正处于新高" : "");
    const uwMax = uw && uw.max_days > 0
      ? `最长水下 ${(uw.max_days / 365.25).toFixed(1)} 年（${uw.max_start.slice(0, 7)}`
        + `${uw.max_end ? " → " + uw.max_end.slice(0, 7) : " 至今"}）`
      : "";

    /* 首屏只回答三件事：今天要不要动、我的钱大概会怎样、现在持有的是什么。
       原来这里是「组合卡 4 个数字 + 6 张 KPI + 每张 KPI 一段 tooltip + 一段长说明」，
       同一件事说了三四遍，用户要读完才知道自己该看哪个数。
       现在合成一张成绩单：年化 / 最坏跌幅 / 稳不稳，各配一行"纯进攻仓是多少"做对照。 */
    const cb = d.combo;
    const score = (cb && cb.active)
      ? {
        cagr: cb.active.cagr, mdd: cb.active.mdd, calmar: cb.active.calmar,
        sat: cb.satellite, cw: cb.core_weight, roll: cb.rolling,
        start: cb.start, mode: cb.core_weight > 0
          ? `底仓 ${pct(cb.core_weight, 0)} + 进攻仓 ${pct(1 - cb.core_weight, 0)}`
          : "纯进攻仓",
      }
      : {
        cagr: self.cagr, mdd: self.mdd, calmar: self.calmar,
        sat: null, cw: 0, roll: null, start: m.start, mode: "纯进攻仓",
      };

    /* 对照行不能自己跟自己比：纯卫星模式下 combo.active 与 satellite 是同一行，
       原来会渲染出「12.9% / 纯进攻仓 12.9%」。这时改比沪深300——这才是用户在意的参照。 */
    const ref = (score.sat && Math.abs(score.sat.cagr - score.cagr) > 1e-9)
      ? { label: "纯进攻仓", cagr: score.sat.cagr, mdd: score.sat.mdd, calmar: score.sat.calmar }
      : { label: "沪深300", cagr: bench.cagr, mdd: bench.mdd,
          calmar: bench.calmar ?? benchCalmar };

    const scoreCard = `
      <div class="card reveal score-card">
        <div class="card-head">
          <div><div class="card-title">成绩</div>
            <div class="card-sub">回测 ${esc((score.start || "").slice(0, 7))} 起 · 已扣交易成本</div>
          </div>
        </div>
        <div class="card-body">
          <div class="score-grid">
            <div><span>一年赚多少</span><b class="${cls(score.cagr)}">${pct(score.cagr, 1)}</b>
              <em>${ref.label} ${pct(ref.cagr ?? 0, 1)}</em></div>
            <div><span>最坏跌多少</span><b class="down">${pct(score.mdd, 1)}</b>
              <em>${ref.label} ${pct(ref.mdd ?? 0, 1)}</em></div>
            <div><span>赚得稳不稳</span><b>${num(score.calmar)}</b>
              <em>${ref.label} ${num(ref.calmar ?? 0)}</em></div>
            <div><span>最长套多久</span><b>${uw && uw.max_days ? (uw.max_days / 365.25).toFixed(1) + " 年" : "—"}</b>
              <em>${uw && uw.max_start ? esc(uw.max_start.slice(0, 7)) + " 起" : "从最高点跌下去到回本"}</em></div>
          </div>

        </div>
      </div>`;

    view.innerHTML = `
      ${strip}
      ${scoreCard}
      <div class="card reveal">
        <div class="card-head">
          <div>
            <div class="card-title">${score.cw > 0 ? "进攻仓净值曲线" : "净值曲线"}</div>
            <div class="card-sub" title="成本按每次单边 0.10% 扣">${m.start} — ${m.end}${score.cw > 0 ? " · 只画进攻仓" : ""}</div>
          </div>
          <div class="legend" id="navLegend"></div>
        </div>
        <div class="card-body"><div class="chart-wrap"><canvas id="navChart"></canvas></div></div>
      </div>

      <div class="grid-2 mt">
        <div class="card reveal">
          <div class="card-head">
            <div><div class="card-title">进攻仓回撤曲线</div>
            <div class="card-sub">${uwMax || ""}</div></div>
            ${ddNow == null ? "" : `<div class="dd-now">
              <b>${pct(ddNow, 1)}</b><span>当前回撤</span>
              ${uwNow ? `<em>${uwNow}</em>` : ""}</div>`}
          </div>
          <div class="card-body"><div class="chart-wrap" style="height:196px">
            <canvas id="ddChart"></canvas></div></div>
        </div>
        <div class="card reveal">
          <div class="card-head">
            <div><div class="card-title">年度收益</div>
            </div>
            <div class="legend" id="yearLegend"></div>
          </div>
          <div class="card-body"><div class="bars" id="yearBars"></div></div>
        </div>
      </div>

      <details class="card reveal mt more-card">
        <summary>和另外三种做法比一比（点开看）</summary>
        ${perfTable(d)}
      </details>
    `;

    // 净值图
    const navSeries = d.series.map((s) => ({
      name: s.name,
      color: seriesColor(s.name),
      data: s.data,
      width: s.name === SELF ? 2.2 : 1.5,
      dash: s.name === BENCH ? [4, 3] : null,
    }));
    lineChart($("#navChart"), {
      series: navSeries,
      log: true,
      height: 330,
      tipFmt: (v) => v.toFixed(3),
    });

    // 图例
    const legend = $("#navLegend");
    navSeries.forEach((s) => {
      const item = el(`<button class="legend-item"><span class="legend-swatch" style="background:${s.color}"></span>${esc(pretty(s.name))}</button>`);
      item.addEventListener("click", () => {
        if (state.hidden.has(s.name)) state.hidden.delete(s.name);
        else if (state.hidden.size < navSeries.length - 1) state.hidden.add(s.name);
        item.classList.toggle("is-off", state.hidden.has(s.name));
        redrawAll();
      });
      legend.appendChild(item);
    });

    // 回撤图
    const ddSelf = d.drawdown.self.map(([dt, v]) => [dt, v / 100]);
    const ddBench = (d.drawdown.bench || []).map(([dt, v]) => [dt, v / 100]);
    lineChart($("#ddChart"), {
      series: [
        { name: "进攻仓回撤", color: cssVar("--chart-up"), data: ddSelf, area: true, width: 1.4 },
        { name: "基准回撤", color: cssVar("--chart-bench"), data: ddBench, width: 1.2, dash: [3, 3] },
      ],
      height: 196,
      yFmt: (v) => (v * 100).toFixed(0) + "%",
      tipFmt: (v) => (v * 100).toFixed(2) + "%",
    });

    // 年度柱状
    renderYearBars(d);
  }

  function renderYearBars(d) {
    const names = [SELF, "S1-动量基线", BENCH];
    const colors = [seriesColor(SELF), seriesColor("S1-动量基线"), seriesColor(BENCH)];
    const hidden = state.hidden;
    const years = Object.keys(d.annual[SELF] || {}).sort();
    let lo = 0, hi = 0;
    years.forEach((y) => names.forEach((n) => {
      const v = d.annual[n]?.[y];
      if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    }));
    const span = Math.max(Math.abs(lo), Math.abs(hi)) || 1;

    const box = $("#yearBars");
    box.innerHTML = years.map((y) => {
      // 正负分开装进上下两半，柱高按"半格 = span"折算，零点线正好是 50%。
      const up = [], down = [];
      names.forEach((n, i) => {
        if (hidden.has(n)) return;
        const v = d.annual[n]?.[y];
        if (v == null) return;
        const h = Math.max(1, (Math.abs(v) / span) * 100);
        const pos = v >= 0;
        (pos ? up : down).push(
          `<div class="bar ${pos ? "pos" : "neg"}"
            style="height:${h}%;background:${colors[i]}"
            title="${esc(pretty(n))} ${y}年: ${v.toFixed(2)}%"></div>`);
      });
      const selfV = d.annual[SELF]?.[y];
      return `<div class="bar-group">
        <div class="bar-stack">
          <div class="bar-half up">${up.join("")}</div>
          <div class="bar-half down">${down.join("")}</div>
        </div>
        <div class="bar-year">${y.slice(2)}${selfV == null ? ""
          : `<i class="${selfV >= 0 ? "up" : "down"}">${selfV >= 0 ? "+" : ""}${selfV.toFixed(0)}%</i>`}</div>
      </div>`;
    }).join("");

    const lg = $("#yearLegend");
    lg.innerHTML = "";
    names.forEach((n, i) => {
      const item = el(`<button class="legend-item ${hidden.has(n) ? "is-off" : ""}">
        <span class="legend-swatch" style="background:${colors[i]}"></span>${esc(pretty(n))}</button>`);
      item.addEventListener("click", () => {
        if (hidden.has(n)) hidden.delete(n);
        else if (hidden.size < names.length - 1) hidden.add(n);
        renderYearBars(d);
      });
      lg.appendChild(item);
    });
  }

  /* 对比表只留"能改变决策"的五列。
     原来 11 列（累计/波动/夏普/索提诺/收益回撤比/超额/换手…）里，
     累计收益与年化是同一件事的两种写法，波动与夏普又是一组，
     读者要横向比 11 个数字才能得出结论——那不是表格，是数据库。 */
  function perfTable(d) {
    const cols = [
      { key: "name", label: "做法", fmt: (v) => `<span class="strong">${esc(pretty(v))}</span>` },
      { key: "cagr", label: "一年赚多少", fmt: (v) => ({ text: pct(v), cls: cls(v) }) },
      { key: "mdd", label: "最坏跌多少", fmt: (v) => ({ text: pct(v), cls: "down" }) },
      { key: "sharpe", label: "稳不稳", tip: "每承受 1 份波动换来的收益。0.5 以上算可用，1 以上算好。",
        fmt: (v) => num(v) },
      { key: "excess", label: "比买指数多赚", fmt: (v) => ({ text: pct(v, 1), cls: cls(v) }) },
      { key: "turnover", label: "一年换几遍", tip: "换手越高，交易成本吃得越多。",
        fmt: (v) => num(v) },
    ];
    const rows = d.perf.map((p) => ({ ...p }));
    return tableCard("四种做法的实测对比", "同一段行情、同一套成本假设",
      cols, rows,
      { rowClass: (r) => (r.name === SELF ? "row-hl" : "") });
  }

  /* ---------------------------------------------------- 页面：调仓清单 */
  const fmtMoney = (v) => "¥" + Math.round(v).toLocaleString("zh-CN");

  function renderSignal() {
    const d = state.signal;
    if (!d || d.error) {
      view.innerHTML = `<div class="empty">${esc(d?.error || "加载中…")}</div>`;
      return;
    }
    const rows = d.rows.slice().sort((a, b) => b.share - a.share);
    const tw = rows.reduce((s, r) => s + r.share, 0) || 1;
    const regime = d.regime;
    const expo = d.exposure;
    /* 大盘状态只有两档（>=0.7 满仓位，否则半仓位），所以标签也只留两态；
       早期的"中性/防守"三态在仓位上没有区别，容易让人以为防守档会更低。 */
    const stateText = regime == null ? "—" : regime >= 0.7 ? "大盘强，满仓" : "大盘弱，半仓";
    const stateColor = regime >= 0.7 ? "var(--up)" : "var(--down)";

    const stalePriceNames = d.stale_prices || [];
    const priceWarn = stalePriceNames.length
      ? `<div class="note danger mt" style="margin-top:0;margin-bottom:13px">
          <svg viewBox="0 0 24 24" fill="none"><path d="M12 8v5M12 16.6v.1" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.6"/></svg>
          <div><b>${stalePriceNames.length} 只标的的行情未更新到 ${esc(d.date)}</b>（${esc(stalePriceNames.join("、"))}）。
          表中参考价是它们各自的最后成交价，可能已滞后，下单前请核对实时价格。</div></div>`
      : "";

    /* 状态提示。清单在两次调仓之间本来就该保持不变，所以"持有中"不报警；
       只有"今天要动手"和"已经过期"才提示。 */
    const ICON_WARN = `<svg viewBox="0 0 24 24" fill="none"><path d="M12 8v5M12 16.6v.1" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.6"/></svg>`;
    const NOTE = "margin-top:0;margin-bottom:13px";
    const statusNote =
      d.status === "due"
        ? `<div class="note danger mt" style="${NOTE}">${ICON_WARN}
            <div><b>调仓日已过（${esc(d.next_reb)}）却没更新</b>，这份清单是 ${esc(d.reb_date)} 定的，已经作废。
            点右上角「更新行情并重算」重拉行情，别继续照这份买。</div></div>`
      : d.status === "today"
        ? `<div class="note danger mt" style="${NOTE}">${ICON_WARN}
            <div><b>今天是调仓日</b>：收盘后点「更新行情并重算」，明天开盘按新清单换仓。
            现在表里这份是 ${esc(d.reb_date)} 定的旧清单。</div></div>`
      : d.status === "exec_today"
        ? `<div class="note danger mt" style="${NOTE}">${ICON_WARN}
            <div><b>今天开盘就要换仓</b>：按 ${esc(d.reb_date)} 收盘定下的清单买，就是下表这份。</div></div>`
      : d.status === "pending"
        ? `<div class="note mt" style="${NOTE}">${ICON_WARN}
            <div><b>${esc(d.exec_date)}（周${esc(d.exec_weekday)}）开盘执行</b>：这是 ${esc(d.reb_date)} 收盘时定下的清单，
            开盘后按下表买入即可。</div></div>`
        : "";

    /* 调仓日历：把"哪天定的 / 哪天买 / 下次哪天换"三个日期一次性摆出来。
       之前这些信息散在三段文字里，用户要读完才知道今天该不该动手。 */
    const schedBar = `<div class="sched">
      <div class="sched-step is-done"><span>调仓决策</span><b>${esc(d.reb_date)}</b><i>周${esc(d.reb_weekday)} 收盘</i></div>
      <i class="sched-line"></i>
      <div class="sched-step ${["pending", "exec_today"].includes(d.status) ? "is-next" : "is-done"}"><span>开盘买入</span><b>${esc(d.exec_date)}</b><i>${
        d.status === "exec_today" ? "就是今天"
          : d.status === "pending" ? "待执行"
          : (d.held_days != null ? `已持有 ${d.held_days} 个交易日` : "已执行")}</i></div>
      <i class="sched-line"></i>
      <div class="sched-step ${d.status === "today" ? "is-due" : d.status === "due" ? "is-due" : "is-next"}"><span>下次换仓</span><b>${esc(d.next_reb || "—")}</b><i>${
        d.status === "today" ? "就是今天"
          : d.status === "due" ? "已过期，请重算"
          : (d.days_to_next != null ? `还有 ${d.days_to_next} 个交易日` : "—")}</i></div>
    </div>`;

    view.innerHTML = `
      ${statusNote}
      ${priceWarn}
      <div class="card reveal">
        <div class="card-head">
          <div><div class="card-title">当前应持有</div></div>
          <div style="display:flex;align-items:center;gap:8px">
            <button class="btn btn-sm" id="refreshData" title="重新下载最新行情并生成新清单">
              更新行情并重算
            </button>
            <span class="tag" style="color:${stateColor};background:transparent;border:1px solid currentColor">${stateText}</span>
          </div>
        </div>
        <div class="card-body">
          ${schedBar}
          <div class="gauge-row">
            <div class="gauge">
              <div class="gauge-label"><span class="term" data-tip="0~1 分。三项趋势条件分别打分后相加：站上 200 日线得 0.4，站上 60 日线得 0.3，近 20 日上涨得 0.3。">大盘分</span></div>
              <div class="gauge-value">${regime == null ? "—" : regime.toFixed(1)} <span class="dim" style="font-size:13px">/ 1.0</span></div>
              <div class="gauge-hint">${regime == null ? "" : regime >= 0.7 ? "趋势都在，进攻仓满仓" : "趋势走坏，只上一半、其余买债"}</div>
            </div>
            <div class="gauge gauge--pos">
              <div class="gauge-label">仓位结构</div>
              ${(() => {
                const pos = d.position || { equity: expo || 0, bond: 0, cash: 0 };
                const eq = Math.round(pos.equity * 1000) / 10;
                const bd = Math.round(pos.bond * 1000) / 10;
                const cs = Math.max(0, Math.round((1 - pos.equity - pos.bond) * 1000) / 10);
                return `<div class="posbar" title="权益 ${eq}% · 债券 ${bd}% · 现金 ${cs}%">
                  <i class="posbar__eq" style="width:${eq}%"></i>
                  <i class="posbar__bond" style="width:${bd}%"></i>
                  <i class="posbar__cash" style="width:${cs}%"></i>
                </div>
                <div class="posbar__legend">
                  <span><i class="sw sw--eq"></i>权益 ${eq}%</span>
                  <span><i class="sw sw--bond"></i>债券 ${bd}%</span>
                  ${cs > 0 ? `<span><i class="sw sw--cash"></i>现金 ${cs}%</span>` : ""}
                </div>`;
              })()}
            </div>
            <div class="gauge">
              <div class="gauge-label">数据截止</div>
              <div class="gauge-value" style="font-size:17px">${esc(d.date)}</div>
              <div class="gauge-hint">${d.age_days === 0 ? "今日收盘数据" : `距今 ${d.age_days} 天`}${d.is_weekend ? " · 今日休市" : ""}</div>
            </div>
          </div>
        </div>
      </div>

      <div class="card reveal mt">
        <div class="card-head">
          <div><div class="card-title">按资金算手数</div></div>
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <div class="cash-box">
              <span class="cash-unit">¥</span>
              <input id="cashInput" type="text" inputmode="numeric" value="100000" autocomplete="off">
              <button class="btn btn-sm" data-cash="50000">5万</button>
              <button class="btn btn-sm" data-cash="100000">10万</button>
              <button class="btn btn-sm" data-cash="200000">20万</button>
              <button class="btn btn-sm" data-cash="500000">50万</button>
            </div>
            <button class="btn btn-sm" id="copyOrder" title="按当前资金复制成文本，可直接对照下单">复制</button>
          </div>
        </div>
        <div class="card-body">
          <div class="table-wrap">
            <table class="order-table">
              <thead><tr>
                <th style="width:36px">#</th><th style="text-align:left">标的</th><th>类别</th>
                <th>目标占比</th><th>目标金额</th><th>参考价</th><th>买几手</th><th>实付</th>
              </tr></thead>
              <tbody id="orderBody"></tbody>
              <tfoot id="orderFoot"></tfoot>
            </table>
          </div>
        </div>
      </div>

      <!-- 「执行规则」卡原来另起一块，把"今天要不要动"又说了一遍——
           顶上状态条已经回答过，这里删掉，只把独一无二的两条并进脚注。 -->
      <div class="muted-line">
        参考价 ${esc(d.date)} 收盘 · 调仓成本 ${pct(d.per_reb_cost, 2)}
      </div>
    `;

    const input = $("#cashInput");
    // 资金优先级：URL 参数 > 上次输入 > 默认 10 万。
    // `/?cash=30000#signal` 便于把"按 3 万算给我看"直接分享出去，
    // 也让无头浏览器能复现「资金不足」这条分支做回归验证。
    const saved = localStorage.getItem("etfquant-cash");
    const qCash = parseInt(new URLSearchParams(location.search).get("cash") || "", 10);
    if (Number.isFinite(qCash) && qCash > 0) input.value = String(qCash);
    else if (saved) input.value = saved;

    /* 清单按"操作频率"分组：底仓买完基本不动，卫星每期可能换，防守债券
       只在大盘状态降档时出现。原来的表按占比混排，8 行里哪几行要动手要看徽章猜。 */
    const legOf = (r) => (r.leg === "core" ? "core" : r.category === "债券" ? "bond" : "sat");
    const GROUPS = [
      { key: "core", title: "底仓", sub: "" },
      { key: "sat", title: "进攻仓", sub: "" },
      { key: "bond", title: "债券", sub: "" },
    ];
    const GROUP_OF = Object.fromEntries(GROUPS.map((g) => [g.key, g]));
    const orderedRows = GROUPS.flatMap((g) => rows.filter((r) => legOf(r) === g.key));

    function paint() {
      const raw = input.value.replace(/[^\d]/g, "");
      const cash = parseInt(raw || "0", 10);
      input.value = cash ? cash.toLocaleString("zh-CN") : "";
      localStorage.setItem("etfquant-cash", String(cash));

      let total = 0;
      let gapSum = 0;
      let idx = 0;
      let lastGroup = null;
      const body = orderedRows.map((r) => {
        const w = r.share / tw;
        const target = cash * w;
        const lotCost = r.price * 100;
        const lots = Math.floor(target / lotCost);
        const shares = lots * 100;
        const actual = shares * r.price;
        total += actual;
        const short = lots === 0;
        const gap = target - actual;                 // 取整后没买上的金额
        gapSum += gap;
        const gapBad = !short && target > 0 && gap / target > 0.01;
        // 决策现场提示：这张表才是实际下单的地方，欠配/买不起必须标在行上，
        // 不能等到「我的持仓」建仓之后才告诉用户。
        const gapNote = short
          ? `<span class="lot-gap">一手 ¥${Math.round(lotCost).toLocaleString("zh-CN")} 超预算</span>`
          : gapBad
            ? `<span class="lot-gap">缺 ¥${Math.round(gap).toLocaleString("zh-CN")}</span>`
            : "";
        // 组头：换了组就插一行说明，让"哪几行不用动"在表格里一眼可辨
        const g = GROUP_OF[legOf(r)];
        const head = g !== lastGroup
          ? `<tr class="group-head"><td colspan="8"><b>${esc(g.title)}</b>${g.sub ? `<span class="dim"> · ${esc(g.sub)}</span>` : ""}</td></tr>`
          : "";
        lastGroup = g;
        idx += 1;
        return `${head}<tr${short ? ' class="row-warn"' : ""}>
          <td class="dim">${idx}</td>
          <td style="text-align:left">
            <span class="strong kopen" data-kcode="${esc(r.code)}">${esc(r.name)}</span>
            ${r.leg === "core" ? '<span class="leg-tag" title="底仓：每期等权重置，不随信号换出">核心</span>' : ""}
            <span class="mono dim">${esc(r.code)}</span>
          </td>
          <td><span class="tag c-${esc(r.category)}">${esc(r.category)}</span></td>
          <td>${(w * 100).toFixed(1)}%</td>
          <td class="dim">${fmtMoney(target)}</td>
          <td>${r.price.toFixed(3)}${r.price_stale
            ? ` <span class="up" title="该标的（${esc(r.code)}）最新行情只到 ${esc(r.price_date)}">⚠</span>` : ""}</td>
          <td class="strong">${short ? '<span class="up">不足1手</span>' : lots + " 手"}</td>
          <td class="strong">${short ? "—" : fmtMoney(actual)}${gapNote}</td>
        </tr>`;
      }).join("");

      const left = cash - total;
      const usedPct = cash ? (total / cash) * 100 : 0;
      $("#orderBody").innerHTML = body;
      $("#orderFoot").innerHTML = `<tr>
        <td colspan="3" style="text-align:left">合计投入 <span class="dim">· 已用 ${usedPct.toFixed(1)}%</span></td>
        <td colspan="3" style="text-align:right" class="strong">${fmtMoney(total)}</td>
        <td colspan="2" class="strong">剩余 ${fmtMoney(left)}</td>
      </tr>`;

      const minCash = Math.max(...rows.map((r) => r.lot_cost / (r.share / tw || 1)));
      if (cash >= 1e7) {
        /* 容量瓶颈不在选股，而在防御债券腿：城投债/地方债 ADV 仅几十万，
           却要吃一半仓位。1000 万时约 7% 的建仓要超过 3 天。 */
        $("#orderFoot").innerHTML += `<tr><td colspan="8" class="lot-gap-row">
          <span class="up"><b>资金已接近容量上限</b></span>：防御债券腿的 20 日均额只有几十万，
          1000 万规模时约 7% 的建仓要超过 3 天才完成，1 亿时是 30%。
          再往上应先替换薄量债券 ETF 并重新回测，否则回测容量假设不成立。</td></tr>`;
      }
      if (cash > 0 && cash < minCash) {
        // 指出卡住的那一行（通常是一手 1.2 万的债券 ETF），并给可执行的三条路，
        // 而不是只扔一个"至少需要 X"——用户看到 4.8 万门槛时最需要的是选项。
        const blocker = rows.reduce((a, b) =>
          (b.lot_cost / (b.share / tw || 1)) > (a.lot_cost / (a.share / tw || 1)) ? b : a);
        const satOnly = rows
          .filter((r) => legOf(r) !== "core")
          .map((r) => r.lot_cost / (r.share / tw || 1));
        const satMin = satOnly.length ? Math.max(...satOnly) : minCash;
        $("#orderFoot").innerHTML += `<tr><td colspan="8" class="lot-fit-row">
          <b>当前资金买不齐整份清单</b>：卡在「${esc(blocker.name)}」——
          一手就要 ${fmtMoney(blocker.lot_cost)}，而它的预算只有
          ${fmtMoney(cash * (blocker.share / tw))}。每只买满 1 手至少需要
          <b>${fmtMoney(minCash)}</b>。三条路任选：<br>
          ① 资金加到 ${fmtMoney(minCash)} 以上，按本清单正常执行；<br>
          ② 切到纯卫星模式（<code>python scripts/run_signal.py --core-weight 0</code>），
          清单只剩 ${rows.length - rows.filter((r) => r.leg === "core").length} 只、
          门槛约 ${fmtMoney(satMin)}——代价是回到回撤 −35% 的形态；<br>
          ③ 保持清单但接受缺口：买不进的预算留在现金，
          实际仓位比回测假设更保守，收益与回撤都会偏离口径。
        </td></tr>`;
      } else if (cash > 0 && left > 0.5) {
        const gapPct = (left / cash) * 100;
        $("#orderFoot").innerHTML += `<tr><td colspan="8" class="lot-gap-row">
          整手取整后剩 <b>${fmtMoney(left)}</b>（${gapPct.toFixed(1)}%）现金。按「实付」列下单，不要凑整。</td></tr>`;
      }
    }

    input.addEventListener("input", paint);
    input.addEventListener("blur", paint);
    $$("[data-cash]").forEach((b) => b.addEventListener("click", () => {
      input.value = b.dataset.cash;
      paint();
    }));

    $("#refreshData")?.addEventListener("click", () => {
      if (state.task) { toast("已有任务在执行，请稍候", "err"); return; }
      // 走组合任务：行情 → 回测 → 信号，三个都做完页面才有意义
      startTask("refresh", true);
    });

    $("#copyOrder")?.addEventListener("click", async () => {
      const cash = parseInt(input.value.replace(/[^\d]/g, "") || "0", 10);
      let total = 0;
      const lines = rows.map((r, i) => {
        const w = r.share / tw;
        const lots = Math.floor((cash * w) / (r.price * 100));
        total += lots * 100 * r.price;
        return `${i + 1}. ${r.name} (${r.code})  ${(w * 100).toFixed(1)}%  买 ${lots} 手 / ${lots * 100} 份  参考价 ${r.price.toFixed(3)}`;
      });
      const text = [
        `ETF 轮动策略 · ${d.reb_date || d.date} 收盘信号`,
        `执行日：${d.exec_date}（周${d.exec_weekday}）开盘`,
        `下次换仓：${d.next_reb || "—"}`,
        `资金：${fmtMoney(cash)}   状态：${stateText}`,
        "".padEnd(40, "-"),
        ...lines,
        "".padEnd(40, "-"),
        `合计约 ${fmtMoney(total)}，剩余 ${fmtMoney(cash - total)}`,
      ].join("\n");
      try {
        await navigator.clipboard.writeText(text);
        toast("清单已复制，可直接对照下单", "ok");
      } catch (e) {
        toast("复制失败，请手动选择表格内容", "err");
      }
    });

    paint();
    maybeRemind(d);      // 命中调仓日且用户开了提醒时才弹，每天一次
  }

  /* ------------------------------------------------------- 页面：实盘跟踪 */
  /* 和「回测持仓」的区别：回测是引擎内部重算的结果，数据修订后会变；
     这一页读的是 data/signal_archive.csv——当时真实输出（live）或事后回放（backfill）。
     回填期天然与回测重合，真正的分歧从 live 记录开始积累。 */
  function renderTrack() {
    const d = state.track;
    if (!d || d.error) {
      view.innerHTML = `<div class="empty">${esc(d?.error || "加载中…")}</div>`;
      return;
    }
    if (!d.count) {
      view.innerHTML = `<div class="card reveal"><div class="card-head"><div>
        <div class="card-title">还没有归档信号</div>
        
      </div></div><div class="card-body">
        <ol class="steps">
          <li>到「数据与维护」运行一次「生成选股信号」，当期就会留下一条 live 记录；</li>
          <li>想先看点内容，可以跑 <b>python scripts/backfill_archive.py</b> 回填历史调仓日（标记为 backfill，与 live 分开显示）。</li>
        </ol>
      </div></div>`;
      return;
    }

    const p = d.paper || {};
    const b = d.backtest || {};
    const diff = (p.total ?? 0) - (b.total ?? 0);

    /* 三张卡各说一件事。原来"纸上累计"和"同期回测"数值完全一样（回填期按构造相等），
       四张卡里有两张在重复同一个数字，偏差卡再解释一次——砍掉中间那张。 */
    const kpi = `<div class="kpi-grid">
      ${kpiCard("已跟踪", `${d.count} 期`, `真实 ${d.live_count} · 回填 ${d.backfill_count}`, "",
        "期数＝归档里的信号日数量。真实＝当日实际输出，回填＝事后用当前策略回放。")}
      ${kpiCard("纸上累计", pct(p.total, 1), `年化 ${pct(p.cagr, 1)} · 回撤 ${pct(p.mdd, 1)}`, cls(p.total),
        "严格按归档里的目标权重、次日开盘成交、双边 0.10% 成本计算。")}
      ${kpiCard("对比同期回测", `${diff >= 0 ? "+" : ""}${(diff * 100).toFixed(2)}pp`,
        `回测 ${pct(b.total, 1)}`, cls(diff),
        "纸上净值 − 同期回测。回填期由同一策略生成，应当吻合；出现偏差说明记账或对齐有问题。")}
    </div>`;

    /* 前向验证：先确认"记录的信号就是策略给的"，再谈偏差。
       只有 2 期 live 时任何结论都不成立，所以把门槛写死在界面上。 */
    const fwd = d.forward || { live_periods: 0, live_days: 0, divergence: null, verdict: "还没有 live 记录", need: 8, selection_cum: null, selection_max_share: null, selection_verdict: "—" };
    const fid = d.fidelity || [];
    // 偏差小到 0.005pp 以下就不上色：给 0.00pp 涂红戴绿只会制造噪音
    const fwdCls = fwd.divergence == null || Math.abs(fwd.divergence) < 5e-5 ? "" : cls(fwd.divergence);
    const fwdCard = `
      <div class="card reveal">
        <div class="card-head"><div>
          <div class="card-title">归档与回测是否一致</div>
          <div class="card-sub">归档 vs 回测同日目标</div>
        </div><span class="src-tag live">${fwd.live_periods} 期</span></div>
        <div class="card-body">
          <div class="fwd-stats">
            <div><span>已记录 / 已执行</span><b>${fwd.live_periods} 期 · 已执行 ${fwd.live_executed ?? 0} 期</b></div>
            <div><span>live 段偏差 · ${fwd.live_days} 个交易日</span><b class="${fwdCls}">${fwd.divergence == null ? "—" : `${fwd.divergence >= 0 ? "+" : ""}${(fwd.divergence * 100).toFixed(2)}pp`}</b></div>
            <div><span>判定</span><b>${esc(fwd.verdict)}</b></div>
            <div><span>选股贡献 · ${fwd.selection_verdict || "—"}</span><b class="${(fwd.selection_cum ?? 0) >= 0 ? "up" : "down"}">${fwd.selection_cum == null ? "—" : `${(fwd.selection_cum * 100).toFixed(2)}pp`}${fwd.selection_max_share == null ? "" : ` <span class="dim">· 最大单期 ${(fwd.selection_max_share * 100).toFixed(0)}%</span>`}</b></div>
          </div>
          ${fid.length ? `<div class="table-wrap"><table>
            <thead><tr><th style="text-align:left">信号日</th><th>持仓（live/回测）</th><th>重合</th><th>权重 L1</th><th>最大偏差</th><th>归档价偏差</th><th>结论</th></tr></thead>
            <tbody>${fid.map((r) => `<tr>
              <td style="text-align:left" class="mono">${esc(r.signal_date)}</td>
              <td>${r.n_live} / ${r.n_backtest}</td>
              <td>${r.overlap} 只</td>
              <td>${(r.l1 * 100).toFixed(2)}pp</td>
              <td>${(r.max_diff * 100).toFixed(2)}pp</td>
              <td class="${r.price_stale ? "up" : "dim"}">${r.price_diff == null ? "—" : `${(r.price_diff * 100).toFixed(2)}%`}</td>
              <td>${r.match && !r.price_stale ? '<span class="down">一致</span>' : '<span class="up">不一致</span>'}</td>
            </tr>`).join("")}</tbody>
          </table></div>` : '<div class="empty">还没有 live 归档，运行一次「生成选股信号」后开始记录。</div>'}
          ${(fwd.live_executed ?? 0) < (fwd.need || 8)
            ? `<div class="note mt">已执行 <b>${fwd.live_executed ?? 0}</b> 期，还差 <b>${(fwd.need || 8) - (fwd.live_executed ?? 0)}</b> 期到 ${fwd.need || 8} 期门槛。</div>`
            : ""}
        </div>
      </div>`;

    const rows = (d.periods || []).slice().reverse().map((r) => {
      const holds = (r.holdings || []).map((h) =>
        `<span class="thold"><b>${pct(h.weight, 1)}</b> ${esc(h.name)}</span>`).join("");
      return `<tr>
        <td class="cell-name"><div class="nm">${esc(r.signal_date)}</div>
          <div class="sub"><span>${r.pending ? "待执行" : "执行 " + esc(r.exec_date)}</span></div></td>
        <td class="cell-holds">${holds}</td>
        <td class="num ${cls(r.ret)}">${r.ret == null ? '<span class="dim">待执行</span>' : `${r.ret >= 0 ? "+" : ""}${(r.ret * 100).toFixed(2)}%`}</td>
        <td class="num ${cls(r.cum)}">${r.cum == null ? "—" : `${r.cum >= 0 ? "+" : ""}${(r.cum * 100).toFixed(2)}%`}</td>
        <td><span class="src-tag ${r.source}">${r.source === "live" ? "真实" : "回填"}</span></td>
      </tr>`;
    }).join("");

    /* 回填期两条线按构造完全重合，画两条只会让人怀疑"是不是少画了一条"。
       重合时只画一条并在副标题说明；一旦出现偏差就自动切回双线。 */
    const overlap = Math.abs(diff) < 5e-5;
    view.innerHTML = `
      ${kpi}
      ${fwdCard}
      <div class="card reveal">
        <div class="card-head"><div>
          <div class="card-title">${overlap ? "纸上净值" : "纸上净值 vs 同期回测"}</div>
          <div class="card-sub">${esc(d.first)} 起 · ${d.count} 期${overlap ? " · 与回测完全重合" : ""}</div>
        </div></div>
        <div class="card-body"><canvas id="trackChart"></canvas></div>
      </div>
      <div class="card reveal">
        <div class="card-head"><div>
          <div class="card-title">逐期信号</div>
          
        </div></div>
        <div class="table-wrap"><table class="track-table">
          <thead><tr><th style="text-align:left">信号日</th><th style="text-align:left">目标持仓</th>
            <th>期间收益</th><th>累计</th><th>来源</th></tr></thead>
          <tbody>${rows}</tbody>
        </table></div>
      </div>
      ${d.hint ? `<div class="note mt"><svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.6"/><path d="M12 8.5v5M12 16.6v.1" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></svg><div>${esc(d.hint)}</div></div>` : ""}
    `;

    const cv = $("#trackChart");
    if (cv && d.series && d.series.length) {
      const paperLine = { name: "纸上净值", color: cssVar("--chart-s4"),
        data: d.series.map((x) => [x.date, x.paper]) };
      const refLine = { name: "同期回测", color: cssVar("--chart-bench"), dash: [4, 3],
        data: d.series.map((x) => [x.date, x.backtest]) };
      lineChart(cv, {
        series: overlap ? [paperLine] : [paperLine, refLine],
        height: 300,
        tipFmt: (v) => v.toFixed(4),
        yFmt: (v) => v.toFixed(2),
      });
    }
  }

  /* ------------------------------------------------------- 页面：我的持仓 */
  /* 与「回测持仓」的区别：这一页是用户自己录入的份额，不是策略的模拟账户。
     数据存在 data/my_holdings.json，由后端估值后返回，接口 /api/holdings。 */

  let editingCode = null;   // 正在编辑的代码，null = 新增

  function pnlText(v, d = 2) {
    if (v == null || Number.isNaN(v)) return "—";
    return (v > 0 ? "+" : "") + pct(v, d);
  }

  function moneySigned(v) {
    if (v == null || Number.isNaN(v)) return "—";
    return (v > 0 ? "+" : v < 0 ? "−" : "") + fmtMoney(Math.abs(v));
  }

  function holdFormHtml(known) {
    const opts = Object.entries(known || {})
      .map(([c, n]) => `<option value="${esc(c)}">${esc(n)}</option>`).join("");
    return `<div class="card reveal" id="holdFormCard">
      <div class="card-head"><div>
        <div class="card-title" id="holdFormTitle">添加一笔持仓</div>
        <div class="card-sub">填<b>摊薄成本</b></div>
      </div><button class="btn btn-ghost btn-sm" id="holdCancel" hidden>取消编辑</button></div>
      <div class="card-body">
        <form class="hold-form" id="holdForm" autocomplete="off">
          <label class="field"><span>代码</span>
            <input id="hCode" list="hCodeList" placeholder="输入代码或从下拉选" required>
            <datalist id="hCodeList">${opts}</datalist>
          </label>
          <label class="field"><span>份额（份）</span>
            <input id="hShares" type="text" inputmode="decimal" placeholder="1000" required>
          </label>
          <label class="field"><span>成本价（元）</span>
            <input id="hCost" type="text" inputmode="decimal" placeholder="120.576" required>
          </label>
          <label class="field"><span>买入日</span>
            <input id="hDate" type="date">
          </label>
          <label class="field field-wide"><span>备注（可选）</span>
            <input id="hNote" maxlength="60" placeholder="例如 定投第 3 期">
          </label>
          <div class="field field-btn">
            <button class="btn btn-primary" type="submit" id="hSubmit">保存</button>
          </div>
        </form>
        <div class="form-hint" id="holdMsg"></div>
      </div>
    </div>`;
  }

  function renderMyHoldings() {
    const d = state.myholdings;
    if (!d || d.error) {
      view.innerHTML = `<div class="empty">${esc(d?.error || "加载中…")}</div>`;
      return;
    }

    const items = d.items || [];
    const advice = (d.advice && d.advice.plan) || [];
    const acts = advice.filter((p) => ["add", "cut", "exit"].includes(p.action));
    const pnlTone = d.total_pnl > 0 ? "up" : d.total_pnl < 0 ? "down" : "";
    const dayTone = d.day_pnl > 0 ? "up" : d.day_pnl < 0 ? "down" : "";

    let html = "";

    /* ---- 整手约束提示（只在刚建仓时有，切页或刷新即消失） ---- */
    const buildNotes = d.build_notes || [];
    if (buildNotes.length) {
      const miss = buildNotes.filter((n) => n.kind === "lot_too_big");
      const under = buildNotes.filter((n) => n.kind === "underweight");
      html += `<div class="card reveal build-notes">
        <div class="card-head"><div>
          <div class="card-title">整手约束提示</div>
          
        </div></div>
        <div class="card-body">
          ${miss.length ? `<div class="bn-head">未买入 ${miss.length} 只 — 一手金额已超过该只预算，整只没买上</div>` : ""}
          ${under.length ? `<div class="bn-head">欠配 ${under.length} 只 — 买到了，但取整后权重低于目标</div>` : ""}
          <ul class="bn-list">${buildNotes.map((n) => `<li>
            <span class="bn-tag ${n.kind}">${n.kind === "lot_too_big" ? "未买入" : "欠配"}</span>
            <b>${esc(n.name)}</b><span class="bn-code">${esc(n.code)}</span>
            <span class="bn-msg">${esc(n.message)}</span>
          </li>`).join("")}</ul>
          <div class="bn-foot">加大投入资金、或减少持仓只数，可以缩小这个缺口。回测收益未扣这部分偏差。</div>
        </div>
      </div>`;
    }

    /* ---- 账户概览（空仓时不显示，四张 0 卡片对新手没有信息量） ---- */
    if (items.length) {
      html += `<div class="kpi-grid">
        ${kpiCard("总资产", fmtMoney(d.total_assets),
          `现金 ${fmtMoney(d.cash)}`, "",
          "持仓市值 + 可用现金。现金可以手动修改，用来算真实仓位比例。")}
        ${kpiCard("持仓市值", fmtMoney(d.total_market_value),
          `${items.length} 只 · 仓位 ${pct(d.position_ratio, 1)}`)}
        ${kpiCard("浮动盈亏", moneySigned(d.total_pnl),
          `成本 ${fmtMoney(d.total_cost)} · ${pnlText(d.total_pnl_pct)}`, pnlTone,
          "市值 − 成本额。只算浮动盈亏，不含已卖出的已实现盈亏，也不扣手续费。")}
        ${kpiCard("今日盈亏", moneySigned(d.day_pnl),
          d.day_pnl_pct == null ? "—" : pnlText(d.day_pnl_pct),
          dayTone,
          "老仓按（现价 − 昨收）算；今天刚买的按（现价 − 成本价）算，避免把买入前的行情算成自己的盈亏。")}
      </div>`;
    }

    /* ---- 首次使用：引导 ---- */
    if (!items.length) {
      html += `<div class="card reveal">
        <div class="card-head"><div>
          <div class="card-title">还没有持仓</div>
          <div class="card-sub">数据只存在本机</div>
        </div></div>
        <div class="card-body">
          <div class="quick-build">
            <div class="quick-build__text">
              <b>按今日信号建仓</b>
              <span>输入准备投入的资金，按今日清单的占比换算成整手买入，买不满的留现金</span>
            </div>
            <div class="cash-box">
              <span class="cash-unit">¥</span>
              <input id="buildAmount" type="text" inputmode="numeric" value="100000" autocomplete="off">
              <button class="btn btn-primary btn-sm" id="btnBuild">按信号建仓</button>
            </div>
          </div>
          <div class="form-hint">多笔持仓用下面的表单逐笔录入，份额与成本照券商 App 抄。</div>
        </div>
      </div>`;
    }

    /* ---- 持仓明细 ---- */
    if (items.length) {
      const rows = items.map((r) => {
        const noPrice = r.price == null;
        const live = r.live ? `<span class="live-dot" title="盘中实时价"></span>` : "";
        return `<tr>
          <td class="cell-name">
            <div class="nm kopen" data-kcode="${esc(r.code)}">${esc(r.name)}${r.leg === "core"
              ? '<span class="leg-tag" title="底仓：每期等权重置，不随信号换出">核心</span>' : ""}</div>
            <div class="sub"><span class="mono">${esc(r.code)}</span>${live}
              ${r.buy_date ? `<span>${esc(r.buy_date)} 买入</span>` : ""}
              ${r.note ? `<span>· ${esc(r.note)}</span>` : ""}</div>
          </td>
          <td class="num">${num(r.shares, 0)}</td>
          <td class="num">${num(r.cost, 3)}</td>
          <td class="num">${noPrice ? "—" : num(r.price, 3)}</td>
          <td class="num">${noPrice ? "—" : fmtMoney(r.market_value)}</td>
          <td class="num ${cls(r.pnl)}">${noPrice ? "—" : moneySigned(r.pnl)}</td>
          <td class="num ${cls(r.pnl)}">${noPrice ? "—" : pnlText(r.pnl_pct)}</td>
          <td class="num ${cls(r.day_pnl)}">${r.day_pnl == null ? "—" : moneySigned(r.day_pnl)}</td>
          <td class="num">${r.pos_weight == null ? "—" : pct(r.pos_weight, 1)}</td>
          <td class="cell-act">
            <button class="link-btn" data-edit="${esc(r.code)}">编辑</button>
            <button class="link-btn danger" data-del="${esc(r.code)}">删除</button>
          </td>
        </tr>`;
      }).join("");

      html += `<div class="card reveal">
        <div class="card-head"><div>
          <div class="card-title">持仓明细</div>
          <div class="card-sub">${items.some((x) => x.live)
            ? "现价为<b>盘中实时价</b>（带红点），开盘前后自动回落到最近收盘价"
            : `现价取最近收盘价（${esc(items.find((x) => x.price_date)?.price_date || "—")}）`}${d.missing_quotes.length ? `；<b>${esc(d.missing_quotes.join("、"))}</b> 暂无行情` : ""}</div>
        </div></div>
        <div class="table-wrap"><table class="hold-table">
          <thead><tr>
            <th style="text-align:left">标的</th><th>份额</th><th>成本价</th><th>现价</th>
            <th>市值</th><th>浮动盈亏</th><th>盈亏率</th><th>今日</th><th>仓位</th><th></th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table></div>
      </div>`;
    }

    /* ---- 调仓建议 ---- */
    if (items.length) {
      if (!acts.length) {
        html += `<div class="note ok-note">
          <svg viewBox="0 0 24 24" fill="none"><path d="m5 12.5 4.2 4.2L19 7" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
          <div><b>当前持仓与今日信号基本一致</b>（每只偏离都在 2 个百分点以内）。
          差额太小反复买卖只会给券商交手续费，今天不用动。</div></div>`;
      } else {
        const ACT_TEXT = { add: "加仓", cut: "减仓", exit: "清仓" };
        const actCls = { add: "up", cut: "down", exit: "down" };
        const arows = acts.map((p) => `<tr>
          <td class="cell-name"><div class="nm kopen" data-kcode="${esc(p.code)}">${esc(p.name)}${p.leg === "core"
              ? '<span class="leg-tag">核心</span>' : ""}</div>
            <div class="sub"><span class="mono">${esc(p.code)}</span></div></td>
          <td class="num">${pct(p.target_weight, 1)}</td>
          <td class="num">${pct(p.actual_weight, 1)}</td>
          <td class="num ${cls(p.diff)}">${pnlText(p.diff, 1)}</td>
          <td class="num ${actCls[p.action]}">${moneySigned(p.delta_value)}</td>
          <td class="num">${p.price == null ? "—" : num(p.price, 3)}</td>
          <td class="num td-act-${p.action}">${p.lots} 手</td>
          <td><span class="pill ${actCls[p.action]}">${ACT_TEXT[p.action]}</span></td>
        </tr>`).join("");

        html += `<div class="card reveal">
          <div class="card-head"><div>
            <div class="card-title">按今日信号，这些需要动一动</div>
            <div class="card-sub">取 ${esc(d.signal_date || "—")} 那期清单 · 买 ${fmtMoney(d.advice.buy_value)} / 卖 ${fmtMoney(d.advice.sell_value)}</div>
          </div></div>
          <div class="table-wrap"><table>
            <thead><tr><th style="text-align:left">标的</th><th>目标仓位</th><th>当前仓位</th>
              <th>差额</th><th>金额</th><th>现价</th><th>操作手数</th><th>动作</th></tr></thead>
            <tbody>${arows}</tbody>
          </table></div>
          <div class="card-body" style="padding-top:0">
            <div class="form-hint">按仓位差估算，不含手续费与滑点。</div>
          </div>
        </div>`;
      }
    }

    /* ---- 表单 + 现金设置 ---- */
    html += holdFormHtml(d.known);

    html += `<div class="card reveal">
      <div class="card-head"><div>
        <div class="card-title">账户设置</div>
        <div class="card-sub">用于算仓位比例，不影响盈亏</div>
      </div></div>
      <div class="card-body">
        <div class="cash-box">
          <span class="cash-unit">¥</span>
          <input id="cashSet" type="text" inputmode="numeric" value="${Math.round(d.cash)}" autocomplete="off">
          <button class="btn btn-sm" id="btnSaveCash">保存现金</button>
          ${items.length ? `<button class="btn btn-sm btn-ghost" id="btnClearHold">清空全部持仓</button>` : ""}
        </div>
        <div class="form-hint">${d.updated_at ? `上次修改：${esc(d.updated_at.replace("T", " "))}` : "还没有修改过"}</div>
      </div>
    </div>`;

    view.innerHTML = html;
    bindMyHoldings(d);
  }

  function bindMyHoldings(d) {
    const form = $("#holdForm");
    const msg = $("#holdMsg");

    const fill = (r) => {
      $("#hCode").value = r ? r.code : "";
      $("#hShares").value = r ? String(r.shares) : "";
      $("#hCost").value = r ? String(r.cost) : "";
      $("#hDate").value = r ? (r.buy_date || "") : "";
      $("#hNote").value = r ? (r.note || "") : "";
      $("#holdFormTitle").textContent = r ? `编辑：${r.name}` : "添加一笔持仓";
      $("#hSubmit").textContent = r ? "保存修改" : "保存";
      $("#holdCancel").hidden = !r;
      $("#hCode").readOnly = !!r;      // 代码是主键，编辑时不允许改
    };

    const submit = async (payload, okText) => {
      try {
        state.myholdings = await api("/api/holdings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (state.myholdings.error) {
          if (msg) msg.textContent = state.myholdings.error;
          toast(state.myholdings.error, "err");
          return false;
        }
        editingCode = null;
        renderMyHoldings();
        const notes = state.myholdings.build_notes || [];
        if (notes.length) {
          const miss = notes.filter((n) => n.kind === "lot_too_big").length;
          const under = notes.filter((n) => n.kind === "underweight").length;
          const parts = [
            miss && `${miss} 只未买入`,
            under && `${under} 只欠配`,
          ].filter(Boolean);
          toast(`${okText}，但整手约束导致 ${parts.join("、")}——明细见持仓页顶部`, "err", 5200);
        } else {
          toast(okText, "ok");
        }
        return true;
      } catch (e) {
        toast("保存失败：" + e.message, "err");
        return false;
      }
    };

    form?.addEventListener("submit", (e) => {
      e.preventDefault();
      const item = {
        code: $("#hCode").value.trim().toLowerCase(),
        shares: $("#hShares").value.trim(),
        cost: $("#hCost").value.trim(),
        buy_date: $("#hDate").value || "",
        note: $("#hNote").value.trim(),
      };
      if (!item.shares || !item.cost) {
        if (msg) msg.textContent = "份额和成本价都要填";
        return;
      }
      submit({ op: editingCode ? "update" : "add", item }, editingCode ? "已更新" : "已添加");
    });

    $("#holdCancel")?.addEventListener("click", () => {
      editingCode = null;
      fill(null);
      if (msg) msg.textContent = "";
    });

    $$("[data-edit]").forEach((b) => b.addEventListener("click", () => {
      const r = (d.items || []).find((x) => x.code === b.dataset.edit);
      if (!r) return;
      editingCode = r.code;
      fill(r);
      $("#holdFormCard")?.scrollIntoView({ behavior: "smooth", block: "center" });
      $("#hShares")?.focus();
    }));

    $$("[data-del]").forEach((b) => b.addEventListener("click", async () => {
      const r = (d.items || []).find((x) => x.code === b.dataset.del);
      if (!r) return;
      if (!window.confirm(`确定删除 ${r.name}（${r.shares} 份）？`)) return;
      await submit({ op: "remove", code: r.code }, "已删除");
    }));

    $("#btnBuild")?.addEventListener("click", async () => {
      const amount = Number(String($("#buildAmount").value).replace(/[^\d.]/g, ""));
      if (!(amount > 0)) { toast("请先填投入金额", "err"); return; }
      if (!window.confirm(`按今日清单用 ${fmtMoney(amount)} 建仓？\n会覆盖现有持仓，成本按最新收盘价计。`)) return;
      await submit({ op: "build", amount }, "已按信号建仓");
    });

    $("#btnSaveCash")?.addEventListener("click", async () => {
      const v = Number(String($("#cashSet").value).replace(/[^\d.]/g, ""));
      await submit({ op: "cash", cash: Number.isFinite(v) ? v : 0 }, "现金已保存");
    });

    $("#btnClearHold")?.addEventListener("click", async () => {
      if (!window.confirm("清空全部持仓与现金？此操作不可撤销。")) return;
      await submit({ op: "clear" }, "已清空");
    });
  }

  function renderPortfolio() {
    const d = state.portfolio;
    if (!d || d.error) {
      view.innerHTML = `<div class="empty">${esc(d?.error || "加载中…")}</div>`;
      return;
    }
    const s = d.stats;
    const totalW = d.current.reduce((a, r) => a + r.weight, 0) || 1;

    const holdHtml = d.current.map((r) => `
      <div class="hold-card">
        <div>
          <div class="hold-name kopen" data-kcode="${esc(r.code)}">${esc(r.name)}</div>
          <div class="hold-sub">
            <span class="mono">${esc(r.code)}</span>
            <span class="tag c-${esc(r.category)}">${esc(r.category)}</span>
            <span>${esc(r.since)} 买入 · 已持 ${r.days} 个交易日</span>
          </div>
          <div class="wbar" style="margin-top:8px"><i style="width:${(r.weight / totalW) * 100}%"></i></div>
        </div>
        <div class="hold-weight">${(r.weight * 100).toFixed(1)}%</div>
        <div class="hold-meta">累计贡献<br><span class="${cls(r.contrib)}">${r.contrib > 0 ? "+" : ""}${r.contrib.toFixed(1)} pct</span></div>
        <div class="hold-ret ${cls(r.ret_since)}">${(r.ret_since * 100).toFixed(1)}%</div>
      </div>`).join("");

    // 贡献榜：前 10 + 后 5
    const top = d.contrib.slice(0, 10);
    const bottom = d.contrib.slice(-5).filter((r) => !top.includes(r));
    const all = [...top, ...bottom];
    const maxAbs = Math.max(...all.map((r) => Math.abs(r.share)), 0.01);

    const contribHtml = all.map((r, i) => {
      const isBottom = i >= top.length;
      const w = (Math.abs(r.share) / maxAbs) * 50;
      const positive = r.share >= 0;
      const color = positive ? "var(--up)" : "var(--down)";
      const style = positive ? `left:50%;width:${w}%` : `right:50%;width:${w}%`;
      return `<div class="contrib-row" ${isBottom && i === top.length ? 'style="margin-top:10px;border-top:1px solid var(--line);padding-top:12px"' : ""}>
        <div class="contrib-name" title="${esc(r.name)}">
          ${r.now ? '<span style="color:var(--accent)">●</span> ' : ""}${esc(r.name)}
        </div>
        <div class="cc-track"><i class="cc-fill" style="${style};background:${color}"></i></div>
        <div class="contrib-val ${cls(r.share)}">${(r.share * 100).toFixed(1)}%</div>
      </div>`;
    }).join("");

    const tradeHtml = d.trades.map((t) => {
      const buys = t.buys.filter((b) => b.to - b.from > 0.005)
        .map((b) => `<span class="chip buy">${esc(b.name)} <b>+${((b.to - b.from) * 100).toFixed(1)}%</b></span>`).join("");
      const sells = t.sells.filter((b) => b.from - b.to > 0.005)
        .map((b) => `<span class="chip sell">${esc(b.name)} <b>-${((b.from - b.to) * 100).toFixed(1)}%</b></span>`).join("");
      const body = [
        buys ? `<div class="tl-line"><span class="tl-label">买入</span>${buys}</div>` : "",
        sells ? `<div class="tl-line"><span class="tl-label">卖出</span>${sells}</div>` : "",
      ].join("") || `<div class="tl-line"><span class="tl-label dim">仅再平衡</span></div>`;
      return `<div class="tl-item">
        <div class="tl-head">
          <span class="tl-date">${esc(t.date)}</span>
          <span class="tl-turn">换手 ${(t.turnover * 100).toFixed(1)}%</span>
        </div>
        ${body}
      </div>`;
    }).join("");

    const st = state.status || {};
    const known = !!state.status;        // 状态还没取到时不猜，否则会和侧栏自相矛盾
    const today = todayLocal();
    const tail = d.as_of >= today ? ""
      : st.intraday ? "（今日尚未收盘，收盘后打开会自动更新）"
      : known ? "（数据落后，见顶部提示）" : "";
    const dateLine = `今天 <b>${today}</b>${st.intraday ? "（盘中）" : ""} ·
      本页数据截至 <b>${esc(d.as_of)}</b> 收盘${tail}`;

    view.innerHTML = `
      <div class="dateline">${dateLine}</div>
      <div class="muted-line" style="margin-top:0"><b>只含进攻仓</b>，不含底仓；清单是今日信号，这里是回测最后一天。</div>
      <div class="kpi-grid">
        ${kpiCard("当前持仓", d.current.length, `平均 ${s.avg_holdings} 只`
          + (state.status && state.status.stale ? `（数据落后 ${state.status.behind_sessions} 天）`
            : state.status && state.status.bt_stale ? "（回测未跟上行情）" : ""), "",
          "进攻仓回测最后一日的持仓数量。目标 3 只，加上防守债券后通常为 3~4 只。")}
        ${kpiCard("累计调仓", s.rebalances, "次 · 每 5 个交易日一次", "",
          "回测期内真正发生交易的次数。5 个交易日调仓一次的频率，7.5 年约 375 次。")}
        ${kpiCard("年均换手", s.avg_turnover.toFixed(1) + " 倍",
          `单边 · 每次约 ${(s.per_reb_turn * 100).toFixed(0)}%`, "",
          "进攻仓一年内大约换掉几倍仓位。数值越高，交易越频繁、成本越高，约 24 倍。")}
        ${kpiCard("年均交易成本", pct(s.cost_annual), "按单边 0.10% 计", "down",
          "换手带来的摩擦成本，直接从净值里扣除。这是高换手策略最真实的代价。")}
      </div>

      <div class="card reveal">
        <div class="card-head">
          <div>
            <div class="card-title">进攻仓 · 回测最后一天的持仓</div>
            <div class="card-sub">截至 ${esc(d.as_of)} · 右侧为持有期涨幅</div>
          </div>
          <span class="tag">共 ${(totalW * 100).toFixed(0)}% 仓位</span>
        </div>
        <div class="card-body"><div class="hold-grid">${holdHtml || '<div class="empty">当前空仓</div>'}</div></div>
      </div>

      <div class="grid-2 mt">
        <div class="card reveal">
          <div class="card-head"><div>
            <div class="card-title">赚的钱是哪几只贡献的</div>
            <div class="card-sub">累计贡献占比 · 合计 100%</div>
          </div></div>
          <div class="card-body" style="padding-top:2px">${contribHtml}</div>
        </div>

        <div class="card reveal">
          <div class="card-head"><div>
            <div class="card-title">最近调仓记录</div>
            <div class="card-sub">最近 ${d.trades.length} 次 · 幅度 &gt;0.5%</div>
          </div></div>
          <div class="card-body">
            <div class="tl">${tradeHtml || '<div class="empty">暂无调仓</div>'}</div>
          </div>
        </div>
      </div>

      <div class="note mt">
        <svg viewBox="0 0 24 24" fill="none"><path d="M12 8.5v5M12 16.6v.1" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.6"/></svg>
        <div>年均换手 <b>${s.avg_turnover.toFixed(1)} 倍</b>意味着每年要付出约 <b>${pct(s.cost_annual)}</b> 的交易成本
        （${s.avg_turnover.toFixed(1)} × 2 × ${(s.fee_side * 100).toFixed(2)}%）。这是进攻仓收益的主要漏损来源——若能把换手降低一半，
        每年可多留约 ${pct(s.cost_annual / 2)} 的收益。</div>
      </div>
    `;
  }

  /* ------------------------------------------------------- 页面：实验 */
  function renderResearch() {
    const d = state.research;
    if (!d || d.error) {
      view.innerHTML = `<div class="empty">${esc(d?.error || "加载中…")}</div>`;
      return;
    }

    // 「组合口径是 12.0% / −19.4%」这句原来写死在说明里。组合比例一调、
    // 或回测记账口径一改，它就变成假话。改成从 overview 的 combo 段取。
    const _cb = state.overview && state.overview.combo;

    const colFmt = (c) => {
      if (/持续/.test(c)) return (v) => ({ text: v == null ? "—" : v.toFixed(0) });
      if (/Sharpe|夏普|卡玛|收益回撤比|Calmar|Beta|换手/i.test(c)) return (v) => ({ text: num(v) });
      if (/CAGR|收益|回撤|MaxDD|胜率|波动/i.test(c)) {
        const colorized = /CAGR|收益|回撤|MaxDD/i.test(c) && !/基准累计|胜率/i.test(c);
        return (v) => ({ text: v == null ? "—" : pct(v), cls: colorized ? cls(v) : (c.includes("回撤") || c.includes("MaxDD")) && v < 0 ? "down" : "" });
      }
      return (v) => ({ text: num(v) });
    };

    const COLTIP = {
      "年化收益(CAGR)": "每年平均的复合增长率，越高越好",
      "年化波动": "净值起伏的剧烈程度，越低越平稳",
      "夏普比率": "每承担 1 份波动换来的超额收益，越高越好",
      "索提诺比率": "只把下跌时的波动计入风险，比夏普更宽容",
      "最大回撤": "最坏情况下的跌幅，越接近 0 越好",
      "最大回撤持续(交易日)": "从高点到低点一共熬了多少个交易日",
      "卡玛比率": "年化收益 ÷ 最大回撤，越高越划算",
      "收益回撤比": "年化收益 ÷ 最大回撤，越高越划算",
      "日胜率": "上涨的交易日占多大比例",
      "月胜率": "上涨的月份占多大比例",
      "年均换手": "一年换掉几倍仓位，越高交易成本越大",
      "基准累计收益": "同期沪深300 的总收益，作为参照",
      "超额收益": "相对沪深300 多赚的部分",
      "基准CAGR": "沪深300 同期的年化收益",
      "基准MaxDD": "沪深300 同期的最大回撤",
      "基准SHARPE": "沪深300 同期的夏普比率",
    };

    /* 实验表原本把 CSV 的 17 列全搬上来（波动/索提诺/Beta/信息比率…），
       真正能改变判断的只有四列：赚多少、跌多深、稳不稳、换手贵不贵。
       其余列留在 CSV 里供研究，不上界面。 */
    const KEEP_COLS = {
      "年化收益(CAGR)": "一年赚多少",
      "最大回撤": "最坏跌多少",
      "夏普比率": "稳不稳",
      "年均换手": "一年换几遍",
    };
    const tbl = (data, labelMap) => {
      if (!data) return "";
      const keep = data.columns.filter((c) => Object.prototype.hasOwnProperty.call(KEEP_COLS, c));
      const cols = [
        { key: "name", label: "做法", fmt: (v) => `<span class="strong">${esc(pretty(v))}</span>` },
        ...keep.map((c) => ({ key: "c" + c, label: KEEP_COLS[c],
          tip: COLTIP[c], fmt: colFmt(c) })),
      ];
      const rows = data.index.map((name, ri) => {
        const o = { name };
        data.columns.forEach((c, ci) => { o["c" + c] = data.rows[ri][ci]; });
        return o;
      });
      return tableCard(labelMap.title, labelMap.sub, cols, rows,
        { rowClass: (r) => (labelMap.hl && r.name.includes(labelMap.hl) ? "row-hl" : "") });
    };

    view.innerHTML = `
      ${d.overfit ? (() => {
        const o = d.overfit;
        const decay = o.is_sharpe_median ? (o.oos_sharpe_median / o.is_sharpe_median - 1) : null;
        const walk = (o.walk || []).map((w) => `<tr>
          <td style="text-align:left"><span class="strong">${esc(w.oos_start)}</span>
            <span class="dim mono" style="margin-left:7px">${esc(w.picked)}</span></td>
          <td class="num">${w.is_sharpe == null ? "—" : num(w.is_sharpe)}</td>
          <td class="num ${cls(w.oos_sharpe)}">${w.oos_sharpe == null ? "—" : num(w.oos_sharpe)}</td>
          <td class="num ${cls(w.oos_ret)}">${w.oos_ret == null ? "—" : pct(w.oos_ret, 1)}</td>
        </tr>`).join("");
        return `<div class="card reveal mt">
          <div class="card-head"><div>
            <div class="card-title">实验四 · 这些结果里有多少是「挑」出来的？</div>
            <div class="card-sub">${o.n_variants} 个变体 · ${o.n_days} 个交易日</div>
          </div></div>
          <div class="card-body">
            <div class="kpi-grid">
              ${kpiCard("碰运气的风险", pct(o.pbo, 1), "越低越好 · 超过 50% 说明多半是挑出来的",
                o.pbo > 0.5 ? "down" : o.pbo > 0.3 ? "" : "up",
                "把历史切成 16 段，轮流用一半挑参数、另一半检验；挑出来的最佳方案在另一半里掉到中位数以下的概率。")}
              ${kpiCard("挑的时候 → 换段行情", `${num(o.is_sharpe_median)} → ${num(o.oos_sharpe_median)}`,
                decay == null ? "" : `衰减 ${pct(decay, 0)}`, cls(decay),
                "同一批参数在挑它时表现出的稳定性，与换一段行情后实际拿到的稳定性（取中位数）。")}
              ${kpiCard("滚动重挑 vs 固定", pct(o.wf_cagr, 1), `固定默认 ${pct(o.wf_fixed_cagr, 1)}`,
                cls(o.wf_cagr - o.wf_fixed_cagr),
                "每 6 个月只用过去 2 年重新挑一次参数、再往前走，拼出来的年化。对照同区间从不换参数的版本。")}
              ${o.expectation ? (() => {
                const e = o.expectation;
                const hasA = e.alpha_ann != null && e.alpha_t != null;
                const foot = hasA
                  ? `历史回测 ${pct(e.sample, 1)} · 超出大盘 ${pct(e.alpha_ann, 1)}`
                  : (e.sample == null ? `锚点 ${pct(e.anchor, 1)}`
                      : `历史回测 ${pct(e.sample, 1)}（上限）`);
                const tip = "把「挑参数」的代价扣掉之后，可以对外引用的年化区间："
                  + "以左边那张卡的「换段行情后」年化为锚点，上下各放宽 1 个百分点。"
                  + (hasA ? `<br>但这个区间建立在「超出大盘 ${pct(e.alpha_ann, 1)} 之上——`
                      + `它的 HAC t 值只有 ${num(e.alpha_t, 2)}，小于 2 就是与 0 在统计上分不开。`
                      + `历史回测 ${pct(e.sample, 1)} 是上限，不是预期。`
                    : "");
                return kpiCard("对外预期", e.label, foot, "", tip);
              })() : ""}
            </div>
            ${walk ? `<div class="table-wrap"><table class="track-table walk-table">
              <thead><tr><th style="text-align:left">换段行情的起点</th><th>挑参数时</th>
                <th>换段行情后</th><th>这一年赚多少</th></tr></thead>
              <tbody>${walk}</tbody>
            </table></div>` : ""}
            <div class="card-sub" style="margin-top:10px">
              ${o.n_variants} 个变体全部跑赢基准；滚动重挑比固定默认差
              ${pct(o.wf_fixed_cagr - o.wf_cagr, 1)}/年；默认参数在网格里并列第 ${o.default_rank}。
            </div>
          </div>
        </div>`;
      })() : ""}

      <div class="mt">
        ${tbl(d.ladder, {
          title: "实验一 · 每加一个模块，效果是变好还是变差？",
          sub: "从只看 20 日涨幅开始，每次加一个功能",
          hl: "闸门改为仅降半仓",
        })}
      </div>

      <div class="mt">
        ${tbl(d.ablation, {
          title: "实验二 · 反过来：每拿掉一个模块，会损失多少？",
          sub: "从功能全开开始，每次关掉一个",
        })}
      </div>

      <div class="mt">
        ${tbl(d.subsample, {
          title: "实验三 · 把时间切成两段，收益是不是只靠某一段行情？",
          sub: "2019-2022 与 2023-2026 分别单跑，检验策略是否只在特定的市场环境里有效",
        })}
      </div>

      <div class="grid-2 mt">
        <div class="card reveal">
          <div class="card-head"><div>
            <div class="card-title">与直觉相反的结论</div>
            
          </div></div>
          <div class="card-body">
            <ol style="margin:0;padding-left:20px;font-size:12.5px;line-height:1.9;color:var(--txt-2)">
              ${flipPoints(d.brief)}
            </ol>
          </div>
        </div>
        <div class="card reveal">
          <div class="card-head"><div>
            <div class="card-title">超额收益的时间分布</div>
            
          </div></div>
          <div class="card-body">
            <div style="font-size:12.5px;line-height:2;color:var(--txt-2)">
              ${(d.excess_split || []).map((s) => `
                <div style="display:flex;justify-content:space-between;padding:9px 0;border-bottom:1px solid var(--line-2)">
                  <span>${esc(s.label)}</span>
                  <span><span class="dim">策略</span> <b class="num ${cls(s.strat)}">${pct(s.strat, 1)}</b>
                  <span class="dim" style="margin-left:8px">基准</span> <b class="num ${cls(s.bench)}">${pct(s.bench, 1)}</b>
                  <span class="dim" style="margin-left:8px">超额</span> <b class="num ${cls(s.excess)}">${s.excess >= 0 ? "+" : ""}${(s.excess * 100).toFixed(1)}pp</b></span>
                </div>`).join("") || '<div class="empty">缺少分段数据</div>'}
            </div>
            <div class="note mt" style="margin-top:16px">
              <svg viewBox="0 0 24 24" fill="none"><path d="M12 8.5v5M12 16.6v.1" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.6"/></svg>
              <div>前段跑输不是计价误差，而是<b>结构性牛市里大盘状态主动降仓的代价</b>：
              只做到半仓，涨得比满仓慢。不做「全天候跑赢」的假设，是使用该策略的前提。</div>
            </div>
          </div>
        </div>
      </div>
    `;
  }

  /**
   * 「与直觉相反的结论」。原来这段写死了「多因子 17.7% vs 纯动量 23.9%」
   * 「缓冲带 12.5% → 14.7%」——都是修复隔夜跳空记账之前的数。
   * 现在按 brief 里现算的 delta 生成，并且**只在确实反直觉时才列出**：
   * 数据变了而结论也变了的话，页面应当跟着不说话，而不是坚持旧叙事。
   */
  function flipPoints(b) {
    if (!b) return '<li class="dim">缺少实验数据，先跑 scripts/grid2.py 与 scripts/ablation.py</li>';
    const items = [];
    const pctD = (a, c) => `${pct(a, 1)} vs ${pct(c, 1)}`;

    if (b.multi && b.multi.multi_cagr < b.multi.pure_cagr) {
      items.push([`多因子合成劣于纯短期动量`,
        `${pctD(b.multi.multi_cagr, b.multi.pure_cagr)}。A 股行业轮动周期 3—8 周，中期因子拖累。`]);
    }
    if (b.ablation?.dd_brake && b.ablation.full
        && b.ablation.dd_brake.cagr < b.ablation.full.cagr) {
      const dC = (b.ablation.dd_brake.cagr - b.ablation.full.cagr) * 100;
      items.push([`回撤熔断是负贡献`,
        `在下跌末期减仓形成追跌：收益 ${dC.toFixed(1)}pp，回撤反而从 `
        + `${pct(b.ablation.full.mdd, 1)} 恶化到 ${pct(b.ablation.dd_brake.mdd, 1)}。`]);
    }
    if (b.buffer && b.buffer.after_cagr > b.buffer.before_cagr) {
      items.push([`缓冲带是唯一「免费午餐」`,
        `排名缓冲带同时改善收益与夏普（${pctD(b.buffer.before_cagr, b.buffer.after_cagr)}），`
        + `换手还降了——它只是「别急着卖」，不是新因子。`]);
    }
    if (b.ablation?.inv_vol && b.ablation.full
        && b.ablation.inv_vol.cagr > b.ablation.full.cagr) {
      items.push([`波动率倒数加权其实比等权好`,
        `${pct(b.ablation.inv_vol.cagr, 1)} vs 等权 ${pct(b.ablation.full.cagr, 1)}，`
        + `夏普 ${num(b.ablation.inv_vol.sharpe)} vs ${num(b.ablation.full.sharpe)}。`
        + `这条没进默认配置，原因是另一轮实验里它的优势不稳（见实验一第 9 步），`
        + `属于「看起来更优但换一段行情就翻转」的参数，不作为结论。`]);
    }
    if (b.ablation?.trend && b.ablation.full && b.ablation.trend.cagr > b.ablation.full.cagr) {
      items.push([`MA60 趋势过滤是净拖累`,
        `关掉它年化 ${pctD(b.ablation.full.cagr, b.ablation.trend.cagr)}，回撤 ${pct(b.ablation.full.mdd, 1)} → `
        + `${pct(b.ablation.trend.mdd, 1)}。它挡掉的上涨比挡掉的下跌多，但确实压住了回撤——`
        + `保留它是为了波动可控，不是为了收益。`]);
    }
    if (b.ablation?.reb20 && b.ablation.full && b.ablation.reb20.cagr < b.ablation.full.cagr * 0.75) {
      items.push([`调仓频率是最脆弱的参数`,
        `20 日调仓只有 ${pct(b.ablation.reb20.cagr, 1)}（${pct(b.ablation.full.cagr, 1)}）。`
        + `轮动周期只有 3—8 周，延迟一倍等于错过整段行情。`]);
    }
    if (!items.length) {
      return '<li class="dim">当前数据下没有出现方向明确的「反直觉」结论——'
        + '各模块的贡献都符合预期，或者互相抵消。</li>';
    }
    return items.map(([h, t], i) => `<li${i ? ' style="margin-top:10px"' : ""}>`
      + `<b style="color:var(--txt)">${esc(h)}</b><br>${esc(t)}</li>`).join("");
  }

  /* ------------------------------------------------------- 页面：数据 */
  function renderData() {
    const d = state.universe;
    if (!d || d.error) {
      view.innerHTML = `<div class="empty">${esc(d?.error || "加载中…")}</div>`;
      return;
    }
    const total = d.categories.reduce((s, c) => s + c.count, 0) || 1;

    view.innerHTML = `
      <div class="kpi-grid">
        ${kpiCard("可选 ETF 数量", d.total, "同一指数只留成交额最大的一只", "",
          "经过流动性筛选和同质化去重后，最终参与策略的 ETF 数量。同一指数的多只 ETF 只保留一只，避免重复买入。")}
        ${kpiCard("已下载历史数据", d.cache, "只 · 前复权日线", "",
          "已保存到本地的日线数据文件数。有缓存就能离线回测，不必每次联网下载。")}
        ${kpiCard("数据截止日", d.latest || "—", "行情更新到这里", "",
          "本地行情数据的最新日期。若要拿到当日最新信号，需先执行「更新历史行情」。")}
        ${kpiCard("覆盖资产类别", d.categories.length, "类别越分散，轮动空间越大", "",
          "标的覆盖的资产类型数量。类别越丰富，某一类走弱时越可能找到替代的强势品种。")}
      </div>

      <div class="card reveal mt">
        <div class="card-head"><div>
          <div class="card-title">手机 App</div>
          
        </div></div>
        <div class="card-body">
          <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
            <code style="flex:1;min-width:220px;padding:11px 13px;border-radius:9px;background:var(--code-bg);border:1px solid var(--line);font-size:13px">dist/etf-quant.apk</code>
            <button class="btn btn-sm" id="openApk">打开所在文件夹</button>
          </div>
          <div class="card-sub" style="margin-top:9px">
            数据版本 <b>${esc(state.mobile?.data_version || "—")}</b> · 调仓日 ${esc(state.mobile?.signal_date || "—")}
            · 重打包 <code>bash android/build.sh</code>
          </div>
        </div>
      </div>

      <div class="card reveal mt">
        <div class="card-head">
          <div>
            <div class="card-title">调仓日提醒</div>
            <div class="card-sub">换仓当天提醒一次</div>
          </div>
          <button class="btn btn-sm${remindOn() ? " btn-primary" : ""}" id="toggleRemind">${
            remindOn() ? "已开启" : "已关闭"}</button>
        </div>
      </div>

      <div class="grid-2">
        <div class="card reveal">
          <div class="card-head"><div>
            <div class="card-title">各类资产有多少可选</div>
            <div class="card-sub"></div>
          </div></div>
          <div class="card-body" style="padding-top:2px">
            ${d.categories.map((c) => `
              <div style="padding:10px 0;border-bottom:1px solid rgba(255,255,255,.04)">
                <div style="display:flex;justify-content:space-between;font-size:12.5px;margin-bottom:7px">
                  <span>${esc(c.name)} <span class="dim">(${c.amount})</span></span>
                  <span class="num strong">${c.count}</span>
                </div>
                <div class="wbar"><i style="width:${(c.count / total) * 100}%;
                  background:${catColor(c.name)}"></i></div>
              </div>`).join("")}
          </div>
        </div>

        <div class="card reveal">
          <div class="card-head"><div>
            <div class="card-title">数据更新与后台任务</div>
            <div class="card-sub">后台执行，日志在右下角</div>
          </div></div>
          <div class="card-body">
            ${[
              ["refresh", "更新行情并重算（推荐）", "依次跑：更新行情 → 重跑回测 → 生成信号 → 组合指标，约 195 秒"],
              ["universe", "扫描全市场 ETF", "枚举 1.8 万个代码段，约 110 秒，需联网"],
              ["download", "更新历史行情", "259 只 × 8.8 年前复权日线，约 70 秒"],
              ["backtest_combo", "重跑回测并更新组合", "四策略全样本回测 + 组合指标重算，约 105 秒"],
              ["combo", "只重算组合指标", "按当前 core_weight 重跑各档组合回测，约 45 秒"],
              ["test", "运行单元测试", ""],
            ].map(([id, t, s]) => `
              <div style="display:flex;align-items:center;gap:12px;padding:11px 0;
                          border-bottom:1px solid rgba(255,255,255,.04)">
                <div style="flex:1;min-width:0">
                  <div style="font-size:12.5px;font-weight:540">${t}</div>
                  <div style="font-size:11px;color:var(--txt-3);margin-top:2px">${s}</div>
                </div>
                <button class="btn btn-sm" data-run="${id}">执行</button>
              </div>`).join("")}
            <div style="margin-top:14px;display:flex;gap:8px">
              <button class="btn btn-sm btn-ghost" id="openReport2" style="flex:1;justify-content:center">
                打开完整研究报告</button>
            </div>
          </div>
        </div>
      </div>

      <div class="mt">
        ${tableCard("成交额最大的 30 只", "",
          [
            { key: "code", label: "代码", fmt: (v) => `<span class="mono kopen" data-kcode="${esc(v)}">${esc(v)}</span>` },
            { key: "name", label: "名称", fmt: (v) => `<span class="strong">${esc(v)}</span>` },
            { key: "category", label: "类别", fmt: (v) => `<span class="tag c-${esc(v)}">${esc(v)}</span>` },
            { key: "price", label: "最新价", fmt: (v) => num(v, 3) },
            { key: "pct", label: "涨跌幅", fmt: (v) => ({ text: pctRaw(v), cls: cls(v) }) },
            { key: "amount", label: "成交额(亿)", fmt: (v) => num(v, 2) },
            { key: "turnover", label: "换手率", fmt: (v) => (v == null ? "—" : pctRaw(v)) },
          ],
          d.top) }
      </div>
    `;

    $$("[data-run]").forEach((b) => {
      b.addEventListener("click", () => runAction(b.dataset.run, b, { showLog: true }));
    });
    $("#openReport2")?.addEventListener("click", () => window.open("/report", "_blank"));

    $("#openApk")?.addEventListener("click", () => {
      window.open("/api/reveal-apk", "_blank");
    });

    $("#toggleRemind")?.addEventListener("click", async (e) => {
      const btn = e.currentTarget;      // await 之后 currentTarget 会是 null，先存住
      await setRemind(!remindOn());
      btn.textContent = remindOn() ? "已开启" : "已关闭";
      btn.classList.toggle("btn-primary", remindOn());
    });
  }

  /* ------------------------------------------------------- 任务 */
  async function runAction(action, btn, opts = {}) {
    if (state.task) {
      toast("已有任务在执行，请稍候", "err");
      return;
    }
    try {
      const r = await api("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action }),
      });
      if (r.error) { toast(r.error, "err"); return; }
      state.task = r.task_id;
      if (btn) btn.disabled = true;
      setTaskRing(0, 1, TASK_LABEL[action] || "执行中");
      // 只有显式要求看日志（数据页的脚本按钮）才开面板
      if (opts.showLog) openLog(r.task_id);
      pollTask(r.task_id);
    } catch (e) {
      toast("启动任务失败：" + e.message, "err");
    }
  }

  /* 顶栏圆环：默认不定态旋转，拿到步数后按弧长走。
     任务不再自动弹日志面板——用户要的是"转个圈就行"，日志只在
     主动点「查看进度」或数据页执行脚本时打开。 */
  const RING_C = 56.55;      // 2πr，r = 9

  function setTaskRing(step, total, text) {
    const chip = $("#taskChip");
    if (!chip) return;
    const fg = $("#taskRingFg");
    chip.hidden = false;
    chip.classList.remove("is-err");     // 新任务开始，清掉上一次的失败红标
    if (text != null) $("#taskChipText").textContent = text;
    const hasStep = total > 1 && step > 0;
    chip.classList.toggle("is-indeterminate", !hasStep);
    if (fg) {
      const p = hasStep ? Math.max(0.08, Math.min(1, step / total)) : 0.25;
      fg.setAttribute("stroke-dashoffset", String(RING_C * (1 - p)));
    }
  }

  function hideTaskRing() {
    const chip = $("#taskChip");
    if (!chip) return;
    chip.hidden = true;
    chip.classList.remove("is-indeterminate");
    const fg = $("#taskRingFg");
    if (fg) fg.setAttribute("stroke-dashoffset", String(RING_C * 0.75));
  }

  function openLog(tid) {
    const p = $("#logPanel");
    p.hidden = false;
    $("#logTitle").textContent = "任务执行中";
    $("#logStatus").textContent = "";
    $("#logBody").textContent = "";
    $("#taskChip").hidden = false;
    $("#taskChipText").textContent = "任务执行中";
    $("#taskChip").classList.remove("is-err");
  }

  /* 日志只渲染一处：轮询与"点开历史任务"共用，避免两套格式漂移。 */
  function paintLog(t) {
    const body = $("#logBody");
    const wasBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
    body.innerHTML = (t.log || []).map((l) => {
      const k = /^\[OK\]|^\[完成\]|passed|全部测试通过/.test(l) ? "ok"
        : /^\[ERR\]|^\[失败\]|Error|Traceback/.test(l) ? "err" : "";
      return k ? `<span class="${k}">${esc(l)}</span>` : esc(l);
    }).join("\n");
    if (wasBottom) body.scrollTop = body.scrollHeight;
    const stateText = t.status === "done" ? "成功"
      : t.status === "wait" ? "行情未入库" : "失败";
    $("#logStatus").textContent = t.status === "running"
      ? `${((Date.now() / 1000) - t.started).toFixed(0)}s`
      : `${t.duration}s · ${stateText}`;
  }

  /* 已结束任务的日志：面板默认不弹，用户点顶栏的"失败"胶囊才展开。 */
  async function showFinishedLog(tid) {
    const p = $("#logPanel");
    p.hidden = false;
    $("#logBody").textContent = "";
    try {
      const t = await api("/api/task?id=" + tid);
      const title = t.status === "done" ? "任务完成"
        : t.status === "wait" ? "当日行情未入库" : "任务失败";
      $("#logTitle").textContent = title;
      // 顶栏胶囊保持"失败"语义——任务已经结束，不能再显示"任务执行中"
      $("#taskChip").hidden = false;
      $("#taskChipText").textContent = t.status === "error" ? "失败 · 查看日志" : title;
      $("#taskChip").classList.toggle("is-err", t.status === "error");
      paintLog(t);
    } catch (e) {
      $("#logStatus").textContent = "日志加载失败";
    }
  }

  function closeLog() {
    $("#logPanel").hidden = true;
  }
  $("#logClose").addEventListener("click", closeLog);

  /* 点顶栏圆环 = 我想看细节；任务失败后胶囊保留，点它看失败原因 */
  $("#taskChip")?.addEventListener("click", () => {
    if (state.task) {
      openLog(state.task);
      pollTask(state.task);
    } else if (state.lastTask) {
      showFinishedLog(state.lastTask);
    }
  });

  async function pollTask(tid) {
    clearInterval(state.pollTimer);
    state.pollTimer = setInterval(async () => {
      try {
        const t = await api("/api/task?id=" + tid);
        paintLog(t);

        if (t.status === "running") {
          const total = t.total_steps || 1;
          const step = t.step || 0;
          setTaskRing(step, total,
            total > 1 && step ? `${t.label} ${step}/${total}` : t.label);
        }

        if (t.status !== "running") {
          clearInterval(state.pollTimer);
          state.pollTimer = null;
          state.task = null;
          state.lastTask = tid;
          $("#modeSwitch")?.classList.remove("is-busy");
          $$("[data-run]").forEach((b) => (b.disabled = false));
          $("#btnBacktest").disabled = false;
          if (t.status === "done") {
            $("#logTitle").textContent = "任务完成";
            hideTaskRing();
            toast(`${t.label} 完成（${t.duration}s）`, "ok");
            if (state.pendingAction) {
              const next = state.pendingAction;
              state.pendingAction = null;
              await runAction(next, null);
              return;
            }
            await refreshAll();
            await checkFreshness(false);
          } else if (t.status === "wait") {
            /* 收盘后当日 Bar 尚未发布 = "还没到"，不是失败：
               不弹日志、不刷红，顶栏胶囊提示会自动恢复。 */
            hideTaskRing();
            $("#logTitle").textContent = "当日行情未入库";
            toast(`${t.label}：当日行情尚未入库，稍后会自动重试`, "ok", 5200);
            await checkFreshness(false);
          } else {
            /* 失败不再自动弹面板（用户明确要求）：改成顶栏红色胶囊，
               点它才展开日志，需要排查时一步可达。 */
            $("#logTitle").textContent = "任务失败";
            $("#taskChip").hidden = false;
            $("#taskChip").classList.add("is-err");
            $("#taskChipText").textContent = "失败 · 查看日志";
            toast(`${t.label} 失败，点右上角「失败 · 查看日志」看原因`, "err", 6000);
            await checkFreshness(false);
          }
        }
      } catch (e) { /* 忽略瞬时错误 */ }
    }, 700);
  }

  /* ------------------------------------------------------- 路由 */
  async function loadPage(page) {
    state.page = page;
    state.charts = [];
    state.hidden = new Set();

    $$(".nav-item").forEach((b) => b.classList.toggle("is-active", b.dataset.page === page));
    const meta = PAGES[page];
    $("#pageTitle").textContent = meta.title;
    $("#pageSub").textContent = meta.sub;

    view.innerHTML = `<div class="skeleton" style="height:150px;margin-bottom:13px"></div>
                      <div class="skeleton" style="height:340px"></div>`;

    try {
      if (page === "overview") {
        if (!state.overview) state.overview = await api("/api/overview");
        renderOverview();
      } else if (page === "signal") {
        state.signal = await api("/api/signal");
        renderSignal();
      } else if (page === "track") {
        state.track = await api("/api/track");
        renderTrack();
      } else if (page === "myholdings") {
        state.myholdings = await api("/api/holdings");
        renderMyHoldings();
      } else if (page === "portfolio") {
        state.portfolio = await api("/api/portfolio");
        renderPortfolio();
      } else if (page === "research") {
        if (!state.research) state.research = await api("/api/research");
        // 顶部要引用组合口径的年化/回撤，直接输 #research 进来时 overview 还没加载
        if (!state.overview) state.overview = await api("/api/overview");
        renderResearch();
      } else if (page === "data") {
        state.universe = await api("/api/universe");
        try { state.mobile = await api("/api/mobile/info"); } catch (e) { state.mobile = null; }
        renderData();
      } else {
        // 旧书签（例如已删除的 #strategy）不该留白屏：回退到总览并改写 hash。
        location.replace("#overview");
        return;
      }
      guardFrozen();
    } catch (e) {
      view.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
    }
  }

  /* 便携版只读：把注定失败的按钮关掉，并在页首说明原因。
     之前它照常显示"更新行情"，点下去必然报"exe 版不含数据管道"——这是 UX 陷阱。 */
  function guardFrozen() {
    if (!state.status || !state.status.frozen) return;
    $$("[data-run], #refreshData, #btnBacktest").forEach((b) => {
      b.disabled = true;
      b.title = "便携版不含数据管道，请用源码版更新";
    });
    const b = $("#bannerUpdate");
    if (b) { b.disabled = true; b.textContent = "便携版不可更新"; }
    if (!view.querySelector(".frozen-note")) {
      const div = el(`<div class="note frozen-note"><b>便携版只读</b>：可以看清单、看 K 线，`
        + `但不能更新行情或重跑回测。需要更新请用源码版（<code>启动.bat</code>）。</div>`);
      view.prepend(div);
    }
  }

  async function refreshAll() {
    state.overview = null;
    state.signal = null;
    state.research = null;
    state.universe = null;
    state.portfolio = null;
    await loadPage(state.page);
    updateFoot();
    updateBadge();
  }

  function updateFoot() {
    const d = state.overview;
    if (!d || d.error) {
      $("#footDot").className = "dot bad";
      $("#footStatus").textContent = "数据未就绪";
      return;
    }
    $("#footDot").className = "dot ok";
    $("#footStatus").textContent = "数据就绪";
    $("#footMeta").innerHTML = `${d.meta.pool} 只标的 · ${d.meta.cache} 只缓存<br>
      ${d.meta.start} → ${d.meta.end}`;
  }

  /* ------------------------------------------------------- 调仓日提醒 */
  /* 只在「今天要换仓」的两种状态（决策日 / 买入日）提醒，其余时间不打扰。
     浏览器通知需要授权；被拒时退回页面内 toast，功能不至于失效。 */
  const REMIND_KEY = "etfquant-remind";
  const REMIND_SENT = "etfquant-remind-sent";     // 记录哪天提醒过，刷新不重复弹

  const remindOn = () => localStorage.getItem(REMIND_KEY) === "1";

  async function setRemind(on) {
    if (on && "Notification" in window && Notification.permission === "default") {
      try { await Notification.requestPermission(); } catch (e) { /* 用户取消 */ }
      if (Notification.permission !== "granted") {
        toast("浏览器未授权通知，仍会在页面顶部提示", "err");
      }
    }
    localStorage.setItem(REMIND_KEY, on ? "1" : "0");
    toast(on ? "已开启调仓日提醒" : "已关闭调仓日提醒", "ok");
  }

  function maybeRemind(d) {
    const r = d && d.remind;
    if (!remindOn() || !r || !r.should) return;
    const today = todayLocal();
    if (localStorage.getItem(REMIND_SENT) === today) return;
    localStorage.setItem(REMIND_SENT, today);
    if ("Notification" in window && Notification.permission === "granted") {
      try { new Notification(r.title, { body: r.body, tag: "etfquant-remind" }); } catch (e) { /* 忽略 */ }
    }
    toast(`${r.title} · ${r.body}`, "ok", 6000);
  }

  /* ------------------------------------------------------- 数据新鲜度 */
  /* 桌面端不像手机那样每次打开都自己拉行情：完整更新要跑
     download(≈70s) + backtest(≈60s) + signal(≈15s)。
     这里做的是「打开时检查一次 → 落后就自动跑一次 → 跑完自动刷新」，
     同一目标交易日只自动尝试一次（服务端记 auto_attempted），失败后降级为手动按钮。
     收盘后目标日推进，会再允许自动跑一次——不用手动点。 */

  function paintFootStatus(st) {
    if (!st) return;
    const dot = $("#footDot");
    const txt = $("#footStatus");
    if (!dot || !txt) return;
    if (st.running) {
      dot.className = "dot";
      txt.textContent = "数据更新中";
    } else if (st.stale) {
      dot.className = "dot bad";
      txt.textContent = `数据落后 ${st.behind_sessions} 个交易日`;
    } else if (st.bt_stale) {
      dot.className = "dot bad";
      txt.textContent = "回测未跟上行情";
    } else {
      dot.className = "dot ok";
      txt.textContent = "数据已是最新";
    }
  }

  function paintBanner(st) {
    const el = $("#banner");
    if (!el) return;
    /* 只有"会改数据"的任务才值得横幅；跑单元测试这种就别占用页面顶部了，
       顶栏圆环已经在转，要看细节点圆环即可。 */
    const DATA_ACTIONS = ["refresh", "download", "backtest", "backtest_combo", "signal", "combo"];
    if (st && st.running && DATA_ACTIONS.includes(st.running.action)) {
      const step = st.running.total_steps > 1 && st.running.step
        ? `（第 ${st.running.step}/${st.running.total_steps} 步）` : "";
      el.className = "banner busy";
      el.innerHTML = `<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.8" stroke-dasharray="14 42"/><path d="M12 7.5v5l3 1.8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
        <div class="banner__text"><b>正在执行：${esc(st.running.label)}${step}</b>
          <br>完成后页面会自动刷新；这段时间可以先看旧数据。</div>
        <button class="btn btn-sm" id="bannerLog">查看进度</button>`;
      el.hidden = false;
      const b = $("#bannerLog");
      if (b) b.onclick = () => openLog(st.running.id);
      return;
    }
    /* 情况二：行情是新的，但回测没重跑 —— 权重表还停在旧日期。
       这时侧栏显示"最新"，页面日期却更旧，最容易被当成 bug。 */
    if (st && !st.stale && st.bt_stale) {
      el.className = "banner";
      el.innerHTML = `<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.7"/><path d="M12 7.6v5.2M12 16.2v.1" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></svg>
        <div class="banner__text"><b>回测产物落后于行情</b>
          （行情已到 ${esc(st.data_date)}，回测结果停在 ${esc(st.bt_date)}）。
          总览与回测持仓的日期取的是回测最后一日，需要重跑一次回测才会跟上。</div>
        <button class="btn btn-sm" id="bannerBacktest">重跑回测</button>`;
      el.hidden = false;
      const bb = $("#bannerBacktest");
      if (bb) bb.onclick = () => startTask("backtest_combo", true);
      return;
    }
    if (!st || !st.stale) { el.hidden = true; return; }
    /* 收盘后数据仍落后，多半是数据源的当日 K 线还没入库（15:00 后陆续落库，
       实测 15:0x 常拉不到）。自动更新扑空后每 15 分钟会自动重试一次，
       把这件事说清楚，避免用户反复手点。 */
    const retryHint = !st.intraday
      ? "<br>已收盘但当日行情尚未入库（收盘后陆续发布），每 15 分钟会自动重试，无需反复手点。"
      : "";
    el.className = "banner";
    el.innerHTML = `<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.7"/><path d="M12 7.6v5.2M12 16.2v.1" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></svg>
      <div class="banner__text"><b>数据落后 ${st.behind_sessions} 个交易日</b>
        （当前到 ${esc(st.data_date)}，应到 ${esc(st.expected_date)}）。
        总览、回测持仓、效果验证都按数据末日计算，所以日期会停在那一天，不是程序算错。
        ${st.intraday ? "<br>" + esc(st.market_open_hint) : ""}${retryHint}</div>
      <button class="btn btn-sm" id="bannerUpdate">立即更新</button>`;
    el.hidden = false;
    const btn = $("#bannerUpdate");
    if (btn) btn.onclick = () => startTask("refresh", true);
  }

  const TASK_LABEL = { refresh: "更新行情并重算", backtest: "重跑回测", signal: "生成信号" };

  async function startTask(action, manual, extra) {
    if (state.task) { if (manual) toast("已有任务在执行，请稍候", "err"); return; }
    try {
      const r = await api("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, ...(extra || {}) }),
      });
      if (r.error) {
        if (manual) toast(r.error, "err");
        return;
      }
      state.task = r.task_id;
      setTaskRing(0, 1, (TASK_LABEL[action] || "执行") + "中");
      if (manual) toast("已开始" + (TASK_LABEL[action] || "任务"), "ok");
      pollTask(r.task_id);
      paintBanner(await api("/api/status"));
    } catch (e) {
      if (manual) toast("启动失败：" + e.message, "err");
    }
  }

  async function checkFreshness(auto) {
    let st;
    try { st = await api("/api/status"); } catch (e) { return; }
    const first = !state.status;
    state.status = st;
    paintBanner(st);
    paintFootStatus(st);
    // 首屏是"状态未知"渲染出来的，回测持仓页顶部那行说明要按真实状态补一次
    if (first && state.page === "portfolio" && state.portfolio) renderPortfolio();
    if (auto && st.stale && !st.running && !st.auto_attempted) {
      await startTask("refresh", false);
    }
  }

  async function updateBadge() {
    try {
      if (!state.signal) state.signal = await api("/api/signal");
      const s = state.signal;
      if (!s.error && s.rows) {
        const badge = $("#navBadge");
        badge.textContent = s.rows.length;
        badge.title = `${s.date} · ${s.rows.length} 只持仓`;
      }
      syncModeSwitch();
    } catch (e) { /* noop */ }
  }

  /* 模式开关：只反映"当前清单是按哪种配置算的"，不做乐观更新——
     切换要真跑一次信号（选股与仓位都不同），所以按钮等任务完成才动。 */
  function syncModeSwitch() {
    const box = $("#modeSwitch");
    if (!box) return;
    const cw = state.signal ? state.signal.core_weight : null;
    if (cw == null) return;
    const on = cw > 0 ? "0.5" : "0";
    $$("#modeSwitch button").forEach((b) => b.classList.toggle("is-on", b.dataset.cw === on));
  }

  async function switchMode(cw) {
    const cur = state.signal ? (state.signal.core_weight || 0) : 0.5;
    if ((cw > 0) === (cur > 0)) return;
    const name = cw > 0 ? "核心+卫星" : "纯卫星";
    const tip = cw > 0
      ? "切到「核心+卫星」：一半买红利低波打底、一半按信号进攻，波动小、涨得慢。"
      : "切到「纯卫星」：全部资金按信号进攻，涨得可能更多，回撤也更大（历史最深 −35%）。";
    if (!window.confirm(`${tip}\n\n切换会重算今天的清单（约 15 秒）。继续？`)) return;
    $("#modeSwitch").classList.add("is-busy");
    await startTask("signal", true, { core_weight: cw });
    // 启动失败（锁被占 / 403 等）不会进轮询，这里兜住，否则开关会一直灰着
    if (!state.task) $("#modeSwitch").classList.remove("is-busy");
  }

  /* ------------------------------------------------------- K 线弹窗 */
  /* 只读本地缓存的日线（前复权）。B/S 来自 output/trades_s4_tgar_main.csv
     的建仓/清仓记录——不是日频权重 diff，那个口径会把价格漂移也当成交易。 */
  const KL = { code: "", rows: [], trades: [], bars: 250, hover: -1, geom: null, cache: {} };

  function cssv(name, fb) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fb;
  }

  function roundRect(ctx, x, y, w, h, r) {
    const rr = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + rr, y);
    ctx.arcTo(x + w, y, x + w, y + h, rr);
    ctx.arcTo(x + w, y + h, x, y + h, rr);
    ctx.arcTo(x, y + h, x, y, rr);
    ctx.arcTo(x, y, x + w, y, rr);
    ctx.closePath();
  }

  function kMovingAvg(rows, n) {
    const out = new Array(rows.length).fill(null);
    let sum = 0;
    for (let i = 0; i < rows.length; i++) {
      sum += rows[i][4];
      if (i >= n) sum -= rows[i - n][4];
      if (i >= n - 1) out[i] = sum / n;
    }
    return out;
  }

  function drawKline() {
    const canvas = $("#kCanvas");
    if (!canvas || !KL.rows.length) return;
    const rows = KL.rows;
    const H = Math.max(320, Math.min(500, Math.round(window.innerHeight * 0.58)));
    const { ctx, w, h } = setupCanvas(canvas, H);

    const C = {
      up: cssv("--up", "#f6465d"), down: cssv("--down", "#2ebd85"),
      txt: cssv("--txt", "#e8ecf4"), txt2: cssv("--txt-2", "#98a3b8"),
      txt3: cssv("--txt-3", "#616b7f"), line: cssv("--line", "#1b202b"),
      panel: cssv("--panel-2", "#12151d"), accent: cssv("--accent", "#6d8cff"),
      gold: cssv("--gold", "#e9b949"), violet: cssv("--violet", "#a78bfa"),
      border: cssv("--line-2", "rgba(255,255,255,0.08)"),
    };
    const PAD = { l: 12, r: 66, t: 18, b: 24 };
    const GAP = 18;
    const volH = Math.max(60, Math.round((h - PAD.t - PAD.b) * 0.26));
    const priceH = h - PAD.t - PAD.b - volH - GAP;
    const plotW = w - PAD.l - PAD.r;
    const n = rows.length;
    const step = plotW / n;
    const bw = Math.max(1.2, Math.min(11, step * 0.68));
    const xAt = (i) => PAD.l + step * (i + 0.5);
    KL.geom = { PAD, step, n, volH, priceH, GAP, w, h };

    const ma5 = kMovingAvg(rows, 5);
    const ma20 = kMovingAvg(rows, 20);
    const ma60 = kMovingAvg(rows, 60);

    let lo = Infinity, hi = -Infinity;
    rows.forEach((r) => { lo = Math.min(lo, r[3]); hi = Math.max(hi, r[2]); });
    [ma5, ma20, ma60].forEach((arr) => arr.forEach((v) => {
      if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    }));
    const padY = (hi - lo) * 0.08 || 0.01;
    lo -= padY; hi += padY;
    const yAt = (v) => PAD.t + ((hi - v) / (hi - lo)) * priceH;

    ctx.clearRect(0, 0, w, h);
    ctx.font = "11px 'Segoe UI', system-ui, sans-serif";
    ctx.textBaseline = "middle";

    /* 横向网格 + 右侧价格刻度 */
    const ticks = niceTicks(lo, hi, 4);
    ticks.forEach((v) => {
      const y = yAt(v);
      if (y < PAD.t - 1 || y > PAD.t + priceH + 1) return;
      ctx.strokeStyle = C.line;
      ctx.globalAlpha = 0.55;
      ctx.beginPath();
      ctx.moveTo(PAD.l, y);
      ctx.lineTo(w - PAD.r + 6, y);
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.fillStyle = C.txt3;
      ctx.textAlign = "left";
      ctx.fillText(v.toFixed(3), w - PAD.r + 10, y);
    });

    /* 日期刻度 + 纵向网格（先画线，再写字） */
    const labelCount = Math.min(5, n);
    const dateMarks = [];
    for (let k = 0; k < labelCount; k++) {
      const i = Math.round((n - 1) * (k / (labelCount - 1 || 1)));
      if (rows[i]) dateMarks.push({ i, text: rows[i][0].slice(5) });
    }
    dateMarks.forEach((m) => {
      const x = xAt(m.i);
      ctx.strokeStyle = C.line;
      ctx.globalAlpha = 0.28;
      ctx.beginPath();
      ctx.moveTo(x, PAD.t);
      ctx.lineTo(x, h - PAD.b);
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.globalAlpha = 1;
    });
    ctx.textAlign = "center";
    ctx.fillStyle = C.txt3;
    dateMarks.forEach((m) => {
      const x = Math.min(Math.max(xAt(m.i), 26), w - PAD.r - 18);
      ctx.fillText(m.text, x, h - PAD.b / 2 - 1);
    });

    /* 成交量 */
    const vTop = PAD.t + priceH + GAP;
    const vols = rows.map((r) => r[5]);
    const vmax = Math.max(...vols) || 1;
    const vAvg = kMovingAvg(rows.map((r) => [0, 0, 0, 0, r[5], r[5]]), 20);
    ctx.strokeStyle = C.line;
    ctx.globalAlpha = 0.5;
    ctx.beginPath();
    ctx.moveTo(PAD.l, vTop - 6);
    ctx.lineTo(w - PAD.r + 6, vTop - 6);
    ctx.stroke();
    ctx.globalAlpha = 1;

    /* K 线与成交量柱 */
    rows.forEach((r, i) => {
      const up = r[4] >= r[1];
      const color = up ? C.up : C.down;
      const x = xAt(i);
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.globalAlpha = 0.92;
      ctx.beginPath();
      ctx.moveTo(x, yAt(r[2]));
      ctx.lineTo(x, yAt(r[3]));
      ctx.lineWidth = 1;
      ctx.stroke();

      const yO = yAt(r[1]);
      const yC = yAt(r[4]);
      const top = Math.min(yO, yC);
      const bodyH = Math.max(1, Math.abs(yC - yO));
      ctx.fillRect(x - bw / 2, top, bw, bodyH);

      const yv = vTop + volH - (r[5] / vmax) * volH * 0.9;
      ctx.globalAlpha = 0.5;
      ctx.fillRect(x - bw / 2, yv, bw, Math.max(1, vTop + volH - yv));
      ctx.globalAlpha = 1;
    });

    /* 20 日均量线：判断"这根量是不是异常"比单看柱子直观 */
    ctx.strokeStyle = C.txt3;
    ctx.globalAlpha = 0.85;
    ctx.lineWidth = 1;
    ctx.beginPath();
    let vs = false;
    vAvg.forEach((v, i) => {
      if (v == null) return;
      const x = xAt(i);
      const y = vTop + volH - (v / vmax) * volH * 0.9;
      if (!vs) { ctx.moveTo(x, y); vs = true; } else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.fillStyle = C.txt3;
    ctx.textAlign = "left";
    ctx.font = "10px 'Segoe UI', system-ui, sans-serif";
    ctx.fillText("成交量 · 20日均量", PAD.l + 2, vTop - 10);
    ctx.font = "11px 'Segoe UI', system-ui, sans-serif";

    /* 均线 */
    const drawMa = (arr, color, width) => {
      ctx.strokeStyle = color;
      ctx.lineWidth = width;
      ctx.globalAlpha = 0.95;
      ctx.beginPath();
      let started = false;
      arr.forEach((v, i) => {
        if (v == null) { started = false; return; }
        const x = xAt(i), y = yAt(v);
        if (!started) { ctx.moveTo(x, y); started = true; }
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
      ctx.globalAlpha = 1;
    };
    drawMa(ma60, C.accent, 1.4);
    drawMa(ma20, C.violet, 1.3);
    drawMa(ma5, C.gold, 1.2);

    /* 买点 / 卖点：贴边旗标 + 竖向虚线。
       不把标签放在蜡烛旁边——相邻两次调仓会把标签叠成一团，
       而且永远会遮住某根 K 线。卖点贴顶、买点贴底，语义也更清楚。 */
    const idxOf = {};
    rows.forEach((r, i) => { idxOf[r[0]] = i; });
    const placed = [];
    KL.trades.forEach((t) => {
      const i = idxOf[t[0]];
      if (i == null) return;
      const buy = t[1] === "B";
      const text = buy ? "买" : "卖";
      const x = xAt(i);

      ctx.strokeStyle = buy ? C.up : C.down;
      ctx.globalAlpha = 0.3;
      ctx.setLineDash([2, 3]);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, PAD.t);
      ctx.lineTo(x, PAD.t + priceH);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.globalAlpha = 1;

      ctx.font = "9.5px 'Segoe UI', system-ui, sans-serif";
      const tw2 = ctx.measureText(text).width + 10;
      const pillH = 14;
      let py = buy ? PAD.t + priceH - pillH - 2 : PAD.t + 2;
      // 同一侧水平过近就错开一层
      for (let guard = 0; guard < 3; guard++) {
        const clash = placed.some((q) => q.buy === buy
          && Math.abs(q.x - x) < tw2 + 5 && Math.abs(q.y - py) < pillH + 2);
        if (!clash) break;
        py += buy ? -pillH - 3 : pillH + 3;
      }
      py = Math.max(PAD.t, Math.min(py, PAD.t + priceH - pillH));
      const px = Math.max(PAD.l, Math.min(x - tw2 / 2, w - PAD.r - tw2));
      placed.push({ x, y: py, buy });

      ctx.fillStyle = buy ? C.up : C.down;
      ctx.globalAlpha = 0.95;
      roundRect(ctx, px, py, tw2, pillH, 3);
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.fillStyle = "#fff";
      ctx.textAlign = "center";
      ctx.fillText(text, px + tw2 / 2, py + pillH / 2 + 0.5);
      ctx.font = "11px 'Segoe UI', system-ui, sans-serif";
    });

    /* 底部轴线 */
    ctx.strokeStyle = C.line;
    ctx.globalAlpha = 0.7;
    ctx.beginPath();
    ctx.moveTo(PAD.l, h - PAD.b);
    ctx.lineTo(w - PAD.r + 6, h - PAD.b);
    ctx.stroke();
    ctx.globalAlpha = 1;

    /* 最新价虚线与标签 */
    const last = rows[n - 1];
    const lastY = yAt(last[4]);
    ctx.setLineDash([3, 4]);
    ctx.strokeStyle = C.txt3;
    ctx.globalAlpha = 0.8;
    ctx.beginPath();
    ctx.moveTo(PAD.l, lastY);
    ctx.lineTo(w - PAD.r + 6, lastY);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;
    const tag = last[4].toFixed(3);
    ctx.fillStyle = last[4] >= last[1] ? C.up : C.down;
    ctx.font = "10.5px 'Segoe UI', system-ui, sans-serif";
    const tw = ctx.measureText(tag).width + 10;
    roundRect(ctx, w - PAD.r + 8, lastY - 8, tw, 16, 4);
    ctx.fill();
    ctx.fillStyle = "#fff";
    ctx.textAlign = "center";
    ctx.fillText(tag, w - PAD.r + 8 + tw / 2, lastY);

    /* 悬停十字线 + 信息框 */
    if (KL.hover >= 0 && KL.hover < n) {
      const i = KL.hover;
      const r = rows[i];
      const x = xAt(i);
      ctx.strokeStyle = C.txt3;
      ctx.globalAlpha = 0.65;
      ctx.setLineDash([2, 3]);
      ctx.beginPath();
      ctx.moveTo(x, PAD.t);
      ctx.lineTo(x, h - PAD.b);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.globalAlpha = 1;

      const chg = i > 0 ? (r[4] / rows[i - 1][4] - 1) * 100 : 0;
      const lines = [
        [r[0], C.txt2],
        [`开 ${r[1].toFixed(3)}   高 ${r[2].toFixed(3)}`, C.txt],
        [`低 ${r[3].toFixed(3)}   收 ${r[4].toFixed(3)}`, C.txt],
        [`涨跌 ${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%   量 ${(r[5] / 1e4).toFixed(1)} 万手`, chg >= 0 ? C.up : C.down],
      ];
      const tradeAt = KL.trades.find((t) => t[0] === r[0]);
      if (tradeAt) lines.push([tradeAt[1] === "B" ? "策略买入" : "策略卖出", tradeAt[1] === "B" ? C.up : C.down]);

      ctx.font = "11px 'Segoe UI', system-ui, sans-serif";
      const bwBox = Math.max(...lines.map((l) => ctx.measureText(l[0]).width)) + 20;
      const bhBox = lines.length * 17 + 12;
      const bx = Math.min(PAD.l + 10, w - PAD.r - bwBox - 8);
      const by = PAD.t + 4;
      ctx.fillStyle = C.panel;
      ctx.globalAlpha = 0.94;
      roundRect(ctx, bx, by, bwBox, bhBox, 8);
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.strokeStyle = C.border;
      ctx.lineWidth = 1;
      roundRect(ctx, bx, by, bwBox, bhBox, 8);
      ctx.stroke();
      ctx.textAlign = "left";
      lines.forEach((l, k) => {
        ctx.fillStyle = l[1];
        ctx.fillText(l[0], bx + 10, by + 15 + k * 17);
      });
    }
  }

  function renderKlineHeader(d) {
    const last = d.rows[d.rows.length - 1];
    const prev = d.prev_close != null ? d.prev_close : (d.rows[d.rows.length - 2] || last)[4];
    const chg = (last[4] / prev - 1) * 100;
    $("#kTitle").textContent = d.name || d.code;
    $("#kCode").textContent = d.code;
    const cat = $("#kCat");
    cat.textContent = d.category || "";
    cat.className = "tag" + (d.category ? " c-" + d.category : "");
    cat.hidden = !d.category;
    const lastEl = $("#kLast");
    lastEl.textContent = last[4].toFixed(3);
    lastEl.className = "kpanel__last " + (chg >= 0 ? "up" : "down");
    const chgEl = $("#kChg");
    chgEl.textContent = `${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%`;
    chgEl.className = "kpanel__chg " + (chg >= 0 ? "up" : "down");

    const rows = d.rows;
    const m = (n) => {
      if (rows.length < n) return "—";
      let s = 0;
      for (let i = rows.length - n; i < rows.length; i++) s += rows[i][4];
      return (s / n).toFixed(3);
    };
    $("#kLegend").innerHTML = `
      <span><i style="background:var(--gold)"></i>MA5 ${m(5)}</span>
      <span><i style="background:var(--violet)"></i>MA20 ${m(20)}</span>
      <span><i style="background:var(--accent)"></i>MA60 ${m(60)}</span>
      <span style="margin-left:auto">前复权 · 日线</span>`;
    const nBuy = d.trades.filter((t) => t[1] === "B").length;
    const nSell = d.trades.filter((t) => t[1] === "S").length;
    const first = rows[0][4];
    const span = (last[4] / first - 1) * 100;
    const spanCls = span >= 0 ? "up" : "down";
    $("#kFoot").innerHTML = `
      <span>数据到 <b>${d.data_date}</b></span>
      <span>区间涨跌 <b class="${spanCls}">${span >= 0 ? "+" : ""}${span.toFixed(1)}%</b></span>
      <span>策略建仓 <b class="up">${nBuy}</b> · 清仓 <b class="down">${nSell}</b></span>
      <span>买卖为调仓信号日，次日开盘成交</span>`;
  }

  async function openKline(code) {
    if (!code) return;
    KL.code = code;
    const modal = $("#klineModal");
    modal.hidden = false;
    document.body.style.overflow = "hidden";
    $("#kTitle").textContent = "载入中…";
    $("#kCode").textContent = code;
    $("#kCat").hidden = true;
    $("#kLast").textContent = "—";
    $("#kChg").textContent = "";
    $("#kLegend").innerHTML = "";
    $("#kFoot").innerHTML = "";

    const key = code + ":" + KL.bars;
    let d = KL.cache[key];
    if (!d) {
      try {
        d = await api(`/api/kline?code=${encodeURIComponent(code)}&bars=${KL.bars}`);
      } catch (e) {
        toast("K 线加载失败：" + e.message, "err");
        closeKline();
        return;
      }
      if (!d || d.error) {
        toast((d && d.error) || "没有这只标的的行情", "err");
        closeKline();
        return;
      }
      KL.cache[key] = d;
    }
    if (KL.code !== code) return;      // 用户已切到别的标的
    KL.rows = d.rows;
    KL.trades = d.trades || [];
    KL.hover = -1;
    renderKlineHeader(d);
    drawKline();
  }

  function closeKline() {
    $("#klineModal").hidden = true;
    document.body.style.overflow = "";
    KL.hover = -1;
  }

  $("#kRanges").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-bars]");
    if (!b) return;
    $$("#kRanges button").forEach((x) => x.classList.toggle("is-on", x === b));
    KL.bars = parseInt(b.dataset.bars, 10) || 250;
    if (KL.code) openKline(KL.code);
  });

  $("#klineModal")?.addEventListener("click", (e) => {
    if (e.target.closest("[data-kclose]")) closeKline();
  });

  $("#kCanvas")?.addEventListener("mousemove", (e) => {
    const g = KL.geom;
    if (!g) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const i = Math.floor((x - g.PAD.l) / g.step);
    const idx = Math.max(-1, Math.min(g.n - 1, i));
    if (idx !== KL.hover) {
      KL.hover = idx;
      drawKline();
    }
  });
  $("#kCanvas")?.addEventListener("mouseleave", () => {
    if (KL.hover !== -1) { KL.hover = -1; drawKline(); }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#klineModal").hidden) closeKline();
  });
  window.addEventListener("resize", () => {
    if (!$("#klineModal").hidden) drawKline();
  });

  /* 任意带 data-kcode 的元素点击即开图（表格里的代码/名称） */
  document.addEventListener("click", (e) => {
    const hit = e.target.closest("[data-kcode]");
    if (!hit) return;
    e.preventDefault();
    openKline(hit.dataset.kcode);
  });

  /* ------------------------------------------------------- 事件绑定 */
  $("#nav").addEventListener("click", (e) => {
    const btn = e.target.closest(".nav-item");
    if (btn) {
      const page = btn.dataset.page;
      if (location.hash !== "#" + page) location.hash = page;
      else loadPage(page);
    }
  });

  $("#modeSwitch")?.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-cw]");
    if (b) switchMode(parseFloat(b.dataset.cw));
  });

  window.addEventListener("hashchange", () => {
    const page = location.hash.slice(1);
    if (PAGES[page] && page !== state.page) loadPage(page);
  });

  $("#btnReport").addEventListener("click", () => window.open("/report", "_blank"));

  $("#btnBacktest").addEventListener("click", (e) => {
    e.currentTarget.disabled = true;
    runAction("backtest_combo", null);
  });

  /* ------------------------------------------------------- 启动 */
  (async function boot() {
    try {
      state.overview = await api("/api/overview");
    } catch (e) { /* noop */ }
    updateFoot();
    // 首屏状态条要回答「今天该不该操作」，所以信号必须先到位再渲染总览
    await updateBadge();

    const initial = location.hash.slice(1);
    await loadPage(PAGES[initial] ? initial : "overview");
    // 状态检查不能挡在首屏前面：/api/status 冷启动要打一次行情快照，
    // 最坏等 3 秒，用户看到的就是白屏。先渲染，状态回来后再补那行说明。
    await checkFreshness(true);

    // 深链：?kline=sh516620 直接打开该标的的 K 线（分享/排查都方便）
    const kp = new URLSearchParams(location.search).get("kline");
    if (kp) openKline(kp);

    /* 冒烟模式：把"页面是否健康"写进 <title>，供无头浏览器一次性读取。
       比截图对比稳（截图会因为字体/抗锯齿抖动），也比引入 puppeteer 轻。 */
    if (/[?&]uismoke=1/.test(location.search)) {
      setTimeout(() => {
        const view = $("#view");
        const text = view ? view.textContent || "" : "";
        const de = document.documentElement;
        const modal = $("#klineModal");
        /* 陈旧数字检测：修完回测的隔夜跳空记账后，全样本年化从 17% 级降到 12% 级，
           一批旧结论数字在界面上就不该再出现。把它们放进探针，冒烟脚本才能发现
           "页面还在讲旧数据"这类回归——这类问题不报错、不溢出、不空白，
           靠原来的三项检查永远发现不了。

           必须用**整句短语**而不是裸数字：第一版写 "15.8%"，结果在「回测持仓」页
           命中了地产 ETF 的真实权重 15.8%，冒烟直接误报成回归。
           一个数字在图上和在正文里是两回事，只有跟着上下文才判得准。 */
        const STALE = [
          "17.7% vs 纯动量 23.9%", "1/σ 加权 22.6%", "Top5 分散仅 15.8%",
          "12.5% → 14.7%", "累计超额仅 +0.9pp", "年化从 12.8% 掉到 6.9%",
          "组合收益回撤比有 88% 优于纯卫星", "对外口径 11%~13%",
        ];
        const hits = STALE.filter((s) => text.indexOf(s) >= 0);
        /* 渲染失败的兜底检测。loadPage 会把异常吞成一句"加载失败：xxx"写在正文里，
           于是控制台没有未捕获错误、正文不空、也没有 NaN——前四项检查全部放过它。
           「怎么选」页就曾经因为一个未定义变量在这里静默崩掉，冒烟却报 ok。 */
        const failed = /^加载失败/.test(text.trim()) || /ReferenceError|TypeError|is not defined/.test(text);
        document.title = "UISMOKE " + JSON.stringify({
          page: state.page,
          vw: innerWidth,
          sw: de.scrollWidth,
          overflow: de.scrollWidth > innerWidth + 1,
          empty: !text.trim(),
          failed: failed ? text.trim().slice(0, 160) : "",
          badText: /NaN|undefined|Infinity/.test(text),
          stale: hits,
          // 清单页的"资金买不齐"分支：低资金用例靠这个断言，见 scripts/ui_smoke.py
          fitNote: !!document.querySelector(".lot-fit-row"),
          kline: !!(modal && !modal.hidden),
          errs: (window.__errs || []).slice(0, 6),
        });
      }, 2600);
    }

    // 若后端正在跑任务，接管日志
    try {
      const a = await api("/api/actions");
      const running = Object.values(a.running || {})[0];
      if (running) {
        state.task = running;
        // 一律不弹日志面板：顶部圆环足够。想看细节点右上角圆环。
        setTaskRing(running.step || 0, running.total_steps || 1, running.label || "执行中");
        pollTask(running);
      }
    } catch (e) { /* noop */ }

    setInterval(updateBadge, 60000);
    setInterval(() => { if (!state.task) checkFreshness(true); }, 120000);
  })();
})();
