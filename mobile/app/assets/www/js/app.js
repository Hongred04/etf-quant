/* ==========================================================================
   ETF Quant · 移动端应用逻辑
   数据来自 data.js（构建时由 scripts/export_mobile_data.py 生成），完全离线。
   无框架、无构建步骤，单文件约 900 行。
   ========================================================================== */
(function () {
  "use strict";

  /* 数据来源：打包内置的 data.js（含 300 天日线），
     运行时用本地引擎在其上追加新行情并重算。不再从服务器拉整包。 */
  var D = window.__DATA__ || {};
  var OV = D.overview || {};
  /* 注意：这里拿到的是一个**引用**。D.signal 被整体替换时（同步/重算），
     必须同时把 SIG 指过去，否则界面永远显示打包时那份快照。
     下面的 setSignal() 是唯一的替换入口。 */
  var SIG = D.signal || {};
  function setSignal(next) {
    if (!next) return;
    D.signal = next;
    SIG = next;
  }
  var PF = D.portfolio || {};
  var RS = D.research || {};
  var UN = D.universe || {};
  var META = OV.meta || {};

  var SELF = "S4-TGAR(自研)";
  var BENCH = "基准-沪深300";

  /* ------------------------------------------------------------ 基础工具 */
  function $(s, r) { return (r || document).querySelector(s); }
  function $$(s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function isNum(v) { return typeof v === "number" && isFinite(v); }
  function pct(v, d) { return isNum(v) ? (v * 100).toFixed(d == null ? 2 : d) + "%" : "—"; }
  function spct(v, d) {
    if (!isNum(v)) return "—";
    return (v > 0 ? "+" : "") + (v * 100).toFixed(d == null ? 1 : d) + "%";
  }
  function num(v, d) { return isNum(v) ? v.toFixed(d == null ? 2 : d) : "—"; }
  function money(v) {
    if (!isNum(v)) return "—";
    return "¥" + Math.round(v).toLocaleString("en-US");
  }
  function cls(v) { return isNum(v) ? (v > 0 ? "up" : v < 0 ? "down" : "") : ""; }

  function toast(msg) {
    var t = $("#toast");
    t.textContent = msg;
    t.hidden = false;
    t.style.animation = "none";
    void t.offsetWidth;
    t.style.animation = "";
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { t.hidden = true; }, 2200);
  }

  /* ------------------------------------------------------------ 网络层 */
  /* 页面在 file:// 下是 opaque origin，fetch 跨域会被直接拒绝；
     因此 Android 版走原生桥取字节，浏览器预览时才回落到 fetch。 */
  function nativeBytes(url) {
    if (!(window.AndroidBridge && AndroidBridge.httpGetBase64)) return null;
    var b64;
    try { b64 = AndroidBridge.httpGetBase64(url); } catch (e) { return null; }
    if (!b64) return null;
    var bin = atob(b64);
    var arr = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    return arr;
  }

  function getText(url, encoding) {
    return new Promise(function (resolve, reject) {
      var bytes = nativeBytes(url);
      if (bytes) {
        try { resolve(new TextDecoder(encoding || "utf-8").decode(bytes)); }
        catch (e) { reject(e); }
        return;
      }
      if (typeof fetch !== "function") { reject(new Error("无网络能力")); return; }
      fetch(url, { cache: "no-store" }).then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.arrayBuffer();
      }).then(function (buf) {
        resolve(new TextDecoder(encoding || "utf-8").decode(new Uint8Array(buf)));
      }).catch(reject);
    });
  }

  /* ------------------------------------------------------------ 本地数据仓库 */
  /* 为什么不再依赖电脑
   * ----------------
   * 上一版是"手机上只做展示，数据由 PC 导出后同步过来"。那要求电脑开机、
   * 同 Wi-Fi、端口放行——本质上还是一个联网才能用的客户端。
   *
   * 现在改成：APK 内置 300 天日线（260 只），手机自己拉最新行情、
   * 用本地引擎（engine.js）重算信号。算力需求很小——260 只 × 300 天，
   * 一次完整重算约 30ms。
   */
  var HIST = D.history || { dates: [], codes: [], close: [], vol: [] };
  /* 最新价缓存：菜单里要显示"参考价已用 14:32 实时价"，
     下单表也需要它来算手数。每次同步后整体替换。 */
  var LIVE = { at: 0, prices: {} };
  var STORE_KEY = "etfq-store";
  var ENGINE_VER = 4;          // 引擎口径变了就 +1，让旧缓存作废
                               // v3：执行日改用本地日期（原 toISOString 少一天）
                               // v4：mergeBars 保留 meta，修复同步后债券仓位消失

  function loadStore() {
    try {
      var raw = localStorage.getItem(STORE_KEY);
      if (!raw) return null;
      var o = JSON.parse(raw);
      if (!o || o.ver !== ENGINE_VER) return null;   // 口径变更后旧结果不可信
      return o;
    } catch (e) { return null; }
  }
  function saveStore(o) {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(o)); }
    catch (e) { /* 存储满/隐私模式，降级为仅本次会话有效 */ }
  }

  /* 把同步得到的 bar 合并进内置历史。
     bars 形如 { "2026-10-26": { "sh510300": [价, 量], ... } }，只存增量，
     所以 localStorage 占用极小（一天约 4KB），不像整包快照那样上兆。 */
  function mergeBars(store) {
    if (!store || !store.bars) return;
    var codeIdx = {};
    HIST.codes.forEach(function (c, i) { codeIdx[c] = i; });
    var dates = HIST.dates.slice();
    var close = HIST.close.map(function (a) { return a.slice(); });
    var vol = HIST.vol.map(function (a) { return a.slice(); });

    Object.keys(store.bars).sort().forEach(function (d) {
      var rec = store.bars[d];
      var k = dates.indexOf(d);
      if (k < 0) {
        k = dates.length;
        dates.push(d);
        close.forEach(function (a) { a.push(null); });
        vol.forEach(function (a) { a.push(null); });
      }
      Object.keys(rec).forEach(function (code) {
        var i = codeIdx[code];
        if (i === undefined) return;      // 内置池里没有的标的直接忽略
        close[i][k] = rec[code][0];
        vol[i][k] = rec[code][1];
      });
    });

    /* 超出窗口就裁掉最老的，保证计算量恒定（MA200 只需要 200 天） */
    var MAX_BARS = 320;
    if (dates.length > MAX_BARS) {
      var drop = dates.length - MAX_BARS;
      dates = dates.slice(drop);
      close = close.map(function (a) { return a.slice(drop); });
      vol = vol.map(function (a) { return a.slice(drop); });
    }

    /* meta 必须一起带过去。
     * 它存的是 code → {name, category}，引擎靠 category === "债券" 把债券
     * 从打分池里摘出来、再当作剩余敞口的容器。这里漏掉 meta，同步过一次之后
     * 债券就被当成普通标的一起打分：不参与打分的规则失效、债券仓位直接不分配，
     * 而且界面上完全不报错（表现为"信号里少了那只债券"）。
     */
    HIST = { dates: dates, codes: HIST.codes, close: close, vol: vol, meta: HIST.meta };
  }

  /* ------------------------------------------------------------ 行情抓取 */

  function chunk(arr, n) {
    var out = [];
    for (var i = 0; i < arr.length; i += n) out.push(arr.slice(i, i + n));
    return out;
  }

  /**
   * 拉腾讯快照。一次可查 60 只，260 只约 5 个请求。
   * 返回 { code: {close, vol, amt, date, time, pct} }
   *
   * 字段位置（实测，88 个字段）：
   *   f[3] 当前价  f[4] 昨收  f[6] 成交量(手)  f[30] 时间戳  f[32] 涨跌幅%
   */
  function fetchSnapshot(codes) {
    var groups = chunk(codes, 60);
    var jobs = groups.map(function (g) {
      return getText("https://qt.gtimg.cn/q=" + g.join(","), "gbk");
    });
    return Promise.all(jobs).then(function (texts) {
      var out = {};
      texts.forEach(function (txt) {
        txt.split(";").forEach(function (seg) {
          var eq = seg.indexOf("=");
          if (eq < 0) return;
          var key = seg.slice(0, eq).trim().replace(/^v_/, "");
          if (!key) return;
          var f = seg.slice(eq + 1).trim().replace(/^"|"$/g, "").split("~");
          if (f.length < 33) return;
          var px = parseFloat(f[3]);
          if (!isNum(px) || px <= 0) return;
          var ts = String(f[30] || "");
          if (ts.length < 14) return;
          out[key] = {
            close: px,
            vol: parseFloat(f[6]) || 0,
            prev: parseFloat(f[4]),
            pct: parseFloat(f[32]),
            date: ts.slice(0, 4) + "-" + ts.slice(4, 6) + "-" + ts.slice(6, 8),
            time: ts.slice(8, 10) + ":" + ts.slice(10, 12)
          };
        });
      });
      return out;
    });
  }

  /**
   * 用日线接口补齐缺失的交易日。
   * 只在缺口 > 1 天时调用——日常更新用快照就够，不必打 260 次请求。
   */
  function fetchKline(code, startDate) {
    var url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=" +
      code + ",day," + startDate + ",,320,qfq";
    return getText(url, "utf-8").then(function (txt) {
      var j = JSON.parse(txt);
      var d = j && j.data && j.data[code];
      if (!d) return [];
      var rows = d.qfqday || d.day || [];
      return rows.map(function (r) {
        return { date: r[0], close: parseFloat(r[2]), vol: parseFloat(r[5]) || 0 };
      }).filter(function (r) { return isNum(r.close) && r.close > 0; });
    }).catch(function () { return []; });
  }

  /* ------------------------------------------------------------ 本地重算 */

  /** 本地日期 YYYY-MM-DD。不能用 toISOString——那是 UTC，跨零点会差一天。 */
  function localDate() {
    var d = new Date();
    var p2 = function (n) { return String(n).padStart(2, "0"); };
    return d.getFullYear() + "-" + p2(d.getMonth() + 1) + "-" + p2(d.getDate());
  }

  /* ---------------------------------------------------------- 调仓日提醒 */
  /* 只在「今天要换仓」的两种状态提醒（决策日 / 买入日），每天最多一次。
     优先走原生通知（AndroidBridge.notify），没有就退回页面 toast。 */
  function remindOn() {
    try { return localStorage.getItem("etfq-remind") === "1"; } catch (e) { return false; }
  }
  function setRemind(on) {
    try { localStorage.setItem("etfq-remind", on ? "1" : "0"); } catch (e) { /* 隐私模式 */ }
  }
  function maybeRemind() {
    if (!remindOn()) return;
    var r = SIG && SIG.remind;
    if (!r || !r.should) return;
    var today = localDate();
    try {
      if (localStorage.getItem("etfq-remind-sent") === today) return;
      localStorage.setItem("etfq-remind-sent", today);
    } catch (e) { /* 存不进去就每次都提醒，可接受 */ }
    if (window.AndroidBridge && AndroidBridge.notify) {
      try { AndroidBridge.notify(r.title, r.body); return; } catch (e) { /* 回落 toast */ }
    }
    toast(r.title + " · " + r.body);
  }

  /** Date → 本地 YYYY-MM-DD。绝不走 toISOString：那是 UTC。 */
  function fmtLocal(d) {
    var p2 = function (n) { return String(n).padStart(2, "0"); };
    return d.getFullYear() + "-" + p2(d.getMonth() + 1) + "-" + p2(d.getDate());
  }

  /* 执行日 = 信号日之后的第一个工作日。
   * 注意：不能写 toISOString().slice(0,10)。new Date("2026-10-23T00:00:00") 是
   * 本地零点，转 UTC 后落在前一天 16:00，切日期会把结果整体前移一天，
   * 表现为「执行日 = 信号日」甚至落到周日。必须用本地字段拼日期。 */
  function nextTradingDay(dateStr) {
    var d = new Date(dateStr + "T00:00:00");
    if (isNaN(d.getTime())) return dateStr;
    do { d.setDate(d.getDate() + 1); } while (d.getDay() === 0 || d.getDay() === 6);
    return fmtLocal(d);
  }

  /* 调仓日锚点推进：服务端给出最近一个调仓日（reb_date），它是等步长网格上的一点。
     手机端可能又补进了几天新行情，于是沿同一网格往前推到最新：每 freq 个交易日一步。
     这样不必在两端各写一遍 warmup/相位逻辑，也不会因为数据起点不同而错位。 */
  function pinRebalanceIndex(dates, anchor, freq) {
    var n = dates.length;
    if (!n) return -1;
    freq = Math.max(1, freq | 0 || 5);
    var t = anchor ? dates.indexOf(anchor) : -1;
    if (t < 0) return n - 1;        // 没有锚点（老数据）：退回最后一根，行为同旧版
    while (t + freq < n) t += freq;
    return t;
  }

  /** 加 k 个工作日（粗略，仅用于"下次大约几号"这类提示）。 */
  function addBizDays(dateStr, k) {
    var d = new Date(dateStr + "T00:00:00");
    if (isNaN(d.getTime())) return dateStr;
    var n = 0;
    while (n < k) {
      d.setDate(d.getDate() + 1);
      var w = d.getDay();
      if (w !== 0 && w !== 6) n++;
    }
    return fmtLocal(d);
  }

  /** a 之后到 b（含）之间隔了几个交易日；b 超出数据范围时用工作日补足。 */
  function tdBetween(dates, aStr, bStr) {
    if (!aStr || !bStr || bStr <= aStr) return 0;
    var last = dates.length ? dates[dates.length - 1] : "";
    if (last && bStr <= last) {
      var n = 0;
      for (var i = 0; i < dates.length; i++) if (dates[i] > aStr && dates[i] <= bStr) n++;
      return n;
    }
    var known = 0;
    for (var j = 0; j < dates.length; j++) if (dates[j] > aStr) known++;
    // bizDaysBetween 统计的是 (last, b] 区间（不含起点），不能再减 1——
    // 这里少算一个交易日会让"还有 N 个交易日"和桌面端差一天。
    return known + Math.max(0, bizDaysBetween(last || aStr, bStr));
  }

  function bizDaysBetween(aStr, bStr) {
    var a = new Date(aStr + "T00:00:00"), b = new Date(bStr + "T00:00:00");
    if (isNaN(a.getTime()) || isNaN(b.getTime()) || !(a < b)) return 0;
    var n = 0, d = new Date(a.getTime());
    while (d < b) {
      d.setDate(d.getDate() + 1);
      var w = d.getDay();
      if (w !== 0 && w !== 6) n++;
    }
    return n;
  }

  /** 把引擎输出翻译成界面用的信号结构（与桌面端 api_signal 保持一致）。 */
  function toSignal(sig, meta) {
    var rows = sig.rows.map(function (r) {
      var live = LIVE.prices[r.code];
      return {
        code: r.code, name: r.name, category: r.category,
        weight: r.weight, price: r.price,
        r20: r.r20, r60: null, vol: null,
        above60: (meta.candMap && meta.candMap[r.code]) || false,
        lot_cost: r.price ? Math.round(r.price * 100 * 100) / 100 : 0,
        market: r.code.indexOf("sh") === 0 ? "沪市" : "深市",
        is_live: !!live
      };
    });
    var eq = sig.equityWeight;
    var wd = ["日", "一", "二", "三", "四", "五", "六"];
    var dates = HIST.dates || [];
    var freq = (D.signal && D.signal.reb_freq) || 5;
    var t = dates.indexOf(sig.date);

    /* 调仓日历：sig.date 是**调仓日**（引擎按锚点推进后算的那一天），不是数据截止日。
       执行日 = 之后第一个交易日；下次=同一网格再走 freq 步。 */
    var dataDate = dates.length ? dates[dates.length - 1] : sig.date;
    var ex = (t >= 0 && t + 1 < dates.length) ? dates[t + 1] : nextTradingDay(sig.date);
    var nextReb = (t >= 0 && t + freq < dates.length) ? dates[t + freq] : "";
    var nextEst = !nextReb;
    if (!nextReb) nextReb = addBizDays(sig.date, freq);

    var todayStr = localDate();
    var exd = new Date(ex + "T00:00:00");
    var ageDays = Math.round((new Date(todayStr + "T00:00:00") - new Date(dataDate + "T00:00:00")) / 86400000);
    /* 与 server._signal_status 同一套规则（Python 侧有单测）。
       pending 还没到买入日 / exec_today 今天买入 / holding 持有中 /
       today 今天是调仓日 / due 调仓日已过还没重算。 */
    var status = todayStr < ex ? "pending"
      : todayStr === ex ? "exec_today"
      : todayStr > nextReb ? "due"
      : todayStr === nextReb ? "today"
      : "holding";
    var remind = status === "exec_today"
      ? { should: true, kind: "exec", title: "今天开盘要换仓",
          body: "按 " + sig.date + " 收盘定的清单执行。" }
      : status === "today"
        ? { should: true, kind: "decision", title: "今天是调仓日",
            body: "收盘后更新行情重算，明天开盘按新清单换仓。" }
        : { should: false, kind: "", title: "", body: "" };
    return {
      date: dataDate,
      today: todayStr,
      reb_date: sig.date,
      reb_weekday: wd[new Date(sig.date + "T00:00:00").getDay()],
      exec_date: ex,
      exec_weekday: wd[exd.getDay()],
      next_reb: nextReb,
      next_weekday: wd[new Date(nextReb + "T00:00:00").getDay()],
      next_est: nextEst,
      reb_freq: freq,
      days_to_next: tdBetween(dates, dataDate, nextReb),
      held_days: tdBetween(dates, ex, dataDate),
      status: status,
      remind: remind,
      /* 与桌面端 api_signal 同口径：过期 = 已过下一个调仓日。
         清单在两次调仓之间本来就该保持不变，用"信号距今几天"当标准会误判。 */
      exec_past: todayStr > ex,
      age_days: ageDays,
      stale: status === "due",
      regime: sig.regime,
      exposure: sig.exposure,
      equity_weight: eq,
      bond_weight: sig.bondWeight,
      rows: rows,
      local: true,
      diag: sig.diag
    };
  }

  /** 用本地引擎在最新交易日重算，返回信号对象。
   *  @param {Object} [store] 调用方已 loadStore 过就传进来，省一次 JSON 解析 */
  function recompute(store) {
    var prep = EtfEngine.prepare(HIST);   // prep.meta 已含名称与类别
    var held = [];
    /* 缓冲带的「上一期持仓」必须跟着实际演进，不能永远拿 APK 内置快照。
     * 优先级：上次算出并落盘的信号 → 内置快照（首次运行）。
     * 否则手机用满一个月后，缓冲带还在护着打包那天的标的，
     * 与桌面端「现有持仓仍在前 6 名就不卖」的口径会悄悄分叉。 */
    var saved = store || loadStore();
    var prev = (saved && saved.sig && saved.sig.rows && saved.sig.rows.length)
      ? saved.sig.rows
      : (D.signal && D.signal.rows ? D.signal.rows : []);
    prev.forEach(function (r) { held.push(r.code); });

    /* 钉在最近一个调仓日，而不是最后一根 K 线：
       策略每 N 个交易日才换仓，行情却每天都有。旧写法每天算出不同持仓，
       照做等于每天换仓，与回测口径不符。 */
    var anchor = (D.signal && D.signal.reb_date) || "";
    var freq = (D.signal && D.signal.reb_freq) || 5;
    var t = pinRebalanceIndex(prep.dates, anchor, freq);
    var sig = EtfEngine.signalAt(prep, t < 0 ? prep.n - 1 : t, prep.meta, held);
    var candMap = {};
    (sig.candidates || []).forEach(function (c) { candMap[c.code] = c.above60; });
    return { raw: sig, api: toSignal(sig, { candMap: candMap }) };
  }

  /* ------------------------------------------------------------ 同步入口 */

  /* ------------------------------------------------------------ 进度环 */
  /* 行情更新全程不弹任何文字提示，只用这一个圆：
       请求快照 / 重算 → 圆弧旋转（不确定进度）
       补齐缺失交易日   → 圆弧按完成比例推进
       结束             → 绿勾 / 红叉，短暂停留后淡出
     理由：自动同步在每次打开 App 时都会跑，弹 toast 属于噪音；
     手动点刷新时用户也不需要读文字，看一眼环就知道成了没有。 */
  var PROG = (function () {
    var el = null;
    var timers = [];
    var CIRC = 116.24;             // 2πr，r = 18.5

    function node() {
      if (!el) el = $("#prog");
      return el;
    }
    function clearTimers() {
      timers.forEach(function (t) { clearTimeout(t); });
      timers = [];
    }
    function later(fn, ms) {
      timers.push(setTimeout(fn, ms));
    }

    function show() {
      var n = node();
      if (!n) return;
      clearTimers();
      n.classList.remove("is-out", "is-done", "is-err");
      n.classList.add("is-busy");
      n.hidden = false;
      n.removeAttribute("aria-hidden");
      var fg = n.querySelector(".prog__fg");
      if (fg) fg.style.strokeDashoffset = "";
    }

    /** pct 为 null 表示不确定进度（转圈），0~1 为确定进度 */
    function phase(pct) {
      var n = node();
      if (!n) return;
      var fg = n.querySelector(".prog__fg");
      if (pct === null || pct === undefined) {
        n.classList.add("is-busy");
        if (fg) fg.style.strokeDashoffset = "";
        return;
      }
      n.classList.remove("is-busy");
      var p = Math.max(0, Math.min(1, pct));
      // 留一点底，别让 0% 时看不见圆弧
      if (fg) fg.style.strokeDashoffset = String(CIRC * (1 - Math.max(0.04, p)));
    }

    function done(ok) {
      var n = node();
      if (!n) return;
      n.classList.remove("is-busy");
      n.classList.add(ok ? "is-done" : "is-err");
      var fg = n.querySelector(".prog__fg");
      if (fg) fg.style.strokeDashoffset = "0";
      clearTimers();
      later(function () {
        n.classList.add("is-out");
        later(function () {
          n.hidden = true;
          n.classList.remove("is-out", "is-done", "is-err");
        }, 240);
      }, ok ? 500 : 1500);
    }

    function hide() {
      var n = node();
      clearTimers();
      if (!n) return;
      n.hidden = true;
      n.classList.remove("is-busy", "is-done", "is-err", "is-out");
    }

    return { show: show, phase: phase, done: done, hide: hide };
  })();

  var SYNC = { busy: false, lastAt: 0, lastMsg: "", intraday: false, quoteTime: "" };

  /**
   * 同步行情并重算信号。
   * manual=true 时给出可见反馈，否则静默失败（开机自检用）。
   */
  function syncMarket(manual) {
    // 已在跑就直接返回，不提示——环还在转，用户能看见
    if (SYNC.busy) return Promise.resolve(false);
    if (typeof EtfEngine === "undefined") {
      SYNC.lastMsg = "引擎缺失，无法重算";
      if (manual) PROG.done(false);
      return Promise.resolve(false);
    }
    SYNC.busy = true;
    var store = loadStore() || { ver: ENGINE_VER, bars: {} };
    var lastDate = HIST.dates[HIST.dates.length - 1];
    PROG.show();

    return fetchSnapshot(HIST.codes).then(function (snap) {
      var codes = Object.keys(snap);
      if (!codes.length) throw new Error("行情接口无返回");

      /* 取出现次数最多的日期作为"快照交易日"，避免个别标的停牌导致误判 */
      var cnt = {};
      codes.forEach(function (c) { cnt[snap[c].date] = (cnt[snap[c].date] || 0) + 1; });
      var snapDate = Object.keys(cnt).sort(function (a, b) { return cnt[b] - cnt[a]; })[0];

      var gapDays = 0;
      if (snapDate > lastDate) {
        var d1 = new Date(lastDate + "T00:00:00");
        var d2 = new Date(snapDate + "T00:00:00");
        gapDays = Math.round((d2 - d1) / 86400000);
      }

      /* 盘中识别（关键）
       * ---------------
       * 策略用的是**收盘价**。9:30~15:00 之间快照给的是实时价，
       * 若直接当成当日 K 线写进去，等于用未完成的一天算指标：
       * 均线被半天波动拉偏、量比失真、信号当天内反复变化。
       * 所以盘中只更新实时价（给下单表算手数用），不写 bar，
       * 信号继续基于上一个完整收盘日。收盘后（快照时间 ≥ 15:00）才落盘。
       */
      var times = [];
      codes.forEach(function (c) {
        if (snap[c].date === snapDate && snap[c].time) times.push(snap[c].time);
      });
      times.sort();
      var lastQuote = times.length ? times[times.length - 1] : "15:00";
      var intraday = (snapDate === localDate()) && (lastQuote < "15:00");
      SYNC.intraday = intraday;
      SYNC.quoteTime = lastQuote;

      PROG.phase(0.35);   // 快照到手

      /* 记录实时价（用于下单表） */
      LIVE.at = Date.now();
      LIVE.prices = {};
      codes.forEach(function (c) {
        LIVE.prices[c] = { price: snap[c].close, prev: snap[c].prev, pct: snap[c].pct, time: snap[c].time };
      });

      var writeBar = function () {
        if (intraday) return Promise.resolve(false);              // 盘中不落盘，见上面的说明
        if (snapDate < lastDate) return Promise.resolve(false);   // 内置数据比快照还新
        var rec = {};
        codes.forEach(function (c) {
          if (snap[c].date === snapDate) rec[c] = [snap[c].close, snap[c].vol];
        });
        store.bars = store.bars || {};
        store.bars[snapDate] = rec;
        return Promise.resolve(true);
      };

      /* 缺口超过 1 个自然日说明中间漏了交易日，需要用日线接口回补。
         只补到这里为止，不做无限回溯——回补超过 40 天意义不大，
         那种情况下直接更新 APK 更省事。 */
      var needBackfill = gapDays > 1 && gapDays <= 40;
      var backfill = function () {
        if (!needBackfill) return Promise.resolve();
        var startD = new Date(lastDate + "T00:00:00");
        startD.setDate(startD.getDate() + 1);
        var startStr = fmtLocal(startD);
        var idx = 0, total = HIST.codes.length, finished = 0;
        var CONC = 6;

        return new Promise(function (resolve) {
          var next = function () {
            if (idx >= total) { resolve(); return; }
            var code = HIST.codes[idx++];
            fetchKline(code, startStr).then(function (rows) {
              rows.forEach(function (r) {
                // 只补 lastDate 与 snapDate 之间的完整交易日；
                // snapDate 当天若是盘中，日线接口返回的也是未完成数据，必须排除
                if (r.date <= lastDate || r.date >= snapDate) return;
                store.bars = store.bars || {};
                store.bars[r.date] = store.bars[r.date] || {};
                store.bars[r.date][code] = [r.close, r.vol];
              });
              finished++;
              // 补齐占整体进度的 35% → 92% 区间
              PROG.phase(0.35 + 0.57 * (finished / total));
              next();
            });
          };
          for (var i = 0; i < CONC; i++) next();
        });
      };

      return writeBar().then(backfill).then(function (changed) {
        PROG.phase(0.94);
        mergeBars(store);
        var r = recompute(store);
        setSignal(r.api);
        // 状态文案要先算好再存：早先写在 saveStore 之后，
        // 导致存进去的永远是上一次的结果（首次运行是空串）
        SYNC.lastAt = Date.now();
        SYNC.lastMsg = intraday
          ? "盘中 " + SYNC.quoteTime + " · 信号基于 " + HIST.dates[HIST.dates.length - 1]
          : "已更新至 " + HIST.dates[HIST.dates.length - 1];

        store.at = SYNC.lastAt;
        store.lastDate = HIST.dates[HIST.dates.length - 1];
        store.sig = r.api;
        store.msg = SYNC.lastMsg;
        saveStore(store);
        PROG.phase(1);
        PROG.done(true);

        if (current === "today" || current === "holdings" || current === "more") render();
        maybeRemind();
        return true;
      });
    }).catch(function (err) {
      SYNC.lastMsg = "上次更新失败：" + (err.message || "网络错误");
      // 自动同步失败就静默收起环（地铁里打开 App 不该看到红叉）；
      // 手动点刷新才给一个红叉，让用户知道没成功。
      if (manual) PROG.done(false);
      else PROG.hide();
      return false;
    }).then(function (r) {
      SYNC.busy = false;
      return r;
    });
  }

  function liveOf(code) { return LIVE.prices[code] || null; }
  /* 下单用实时价；没有实时数据时回落到信号日收盘价 */
  function priceOf(r) {
    var l = liveOf(r.code);
    return l && isNum(l.price) && l.price > 0 ? l.price : r.price;
  }

  /* ------------------------------------------------------------ 底部说明层 */
  var sheet = $("#sheet"), backdrop = $("#sheetBackdrop");
  function openSheet(title, html) {
    $("#sheetTitle").textContent = title;
    $("#sheetBody").innerHTML = html;
    backdrop.hidden = false;
    sheet.hidden = false;
  }
  function closeSheet() {
    sheet.hidden = true;
    backdrop.hidden = true;
  }
  backdrop.addEventListener("click", closeSheet);
  $("#sheetClose").addEventListener("click", closeSheet);

  /* 术语解释：手机上没法 hover，统一改成点按弹层 */
  var TERMS = {
    cum: ["累计收益", "回测区间内的总收益率：1 元变成多少元。不含年化，区间越长数值自然越大。"],
    cagr: ["年化收益", "把总收益折算成<b>每年平均</b>的复合增长率，方便和存款、基金、指数等不同期限的产品横向比较。本项目按<b>日历天数</b>折算，A 股每年约 242 个交易日，用 252 会高估约 0.8pct。<br>大号数字是<b>样本内</b>结果（参数在同一段历史上挑过），可对外引用的预期要打折，见「验证」页顶部。"],
    sharpe: ["夏普比率", "每承受 1 份波动能换来多少超额收益。<b>大于 1 优秀，0.5~1 可用，低于 0.5 一般</b>。衡量的是「赚得稳不稳」，不只是赚得多不多。"],
    mdd: ["最大回撤", "从历史最高点跌到最低点的<b>最大跌幅</b>，代表最坏情况下账户会缩水多少，是判断自己能不能拿得住的关键指标。"],
    calmar: ["卡玛比率", "年化收益 ÷ 最大回撤。数值越高，说明<b>赚的钱相对承受的回撤越划算</b>。"],
    excess: ["年化超额", "策略年化收益 − 沪深300 年化收益。正数代表跑赢。<b>不要用「累计超额 ÷ 年数」</b>，那不是年化口径。"],
    turnover: ["年均换手", "一年内大约换掉几倍仓位。<b>越高交易成本越大</b>，24 倍意味着每年换了 24 遍。"],
    cost: ["年化交易成本", "换手带来的摩擦成本，直接从净值里扣除。按双边 0.10% 计：换手 24.1 × 2 × 0.10% ≈ 4.8%。"],
    ir: ["信息比率", "超额收益的稳定性：超额均值 ÷ 超额波动。越高说明跑赢不是靠某几次运气。"],
    regime: ["大盘强弱分", "0~1 分。站上 200 日线得 0.4，站上 60 日线得 0.3，近 20 日上涨得 0.3。<b>分数越高说明大盘越强</b>。"],
    exposure: ["权益仓位", "投入股票/商品类 ETF 的比例。剩下的钱买债券 ETF，账户始终满仓——「半仓」是权益占一半，不是留一半现金。"],
    weight: ["建议仓位", "建议把多少钱投在这只 ETF 上，占比合计不超过 100%。"],
    r20: ["近20日涨幅", "最近 20 个交易日的涨幅，同时<b>剔除最近 5 日</b>以避开短期急涨急跌的干扰——这是排序的主要依据。"],
    price: ["参考价", "信号日的真实收盘价。实际成交价以下单时为准。"],
    expect: ["对外预期", "把「挑参数」的代价扣掉之后可以对外引用的年化区间。锚点是每 6 个月用过去 2 年重挑一次参数的<b>纯样本外</b>年化，不是回测那个样本内数字。<br>注意：这个区间建立在 <b>α（归因残差）</b> 之上，而 α 的 HAC t 值只有 1.38——小于 2 就是与 0 在统计上分不开。所以它是<b>折价后的点估计</b>，不是置信区间（真正的 95% 区间宽达 ±20pp）。"],
    pbo: ["PBO 过拟合概率", "把收益切成 16 段、枚举一半做样本内另一半做样本外：样本内冠军在样本外掉到中位数以下的概率。<b>0 理想，超过 50% 属严重</b>。"],
  };
  function qmark(key) { return '<span class="q" data-term="' + key + '">?</span>'; }
  document.addEventListener("click", function (e) {
    var el = e.target.closest("[data-term]");
    if (!el) return;
    var t = TERMS[el.dataset.term];
    if (!t) return;
    openSheet(t[0], "<p>" + t[1] + "</p>");
  });

  /* ------------------------------------------------------------ 主题 */
  function theme() { return document.documentElement.dataset.theme || "dark"; }
  function applyTheme(t) {
    document.documentElement.dataset.theme = t;
    try { localStorage.setItem("etfq-theme", t); } catch (e) { /* 隐私模式 */ }
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", t === "light" ? "#f5f6f8" : "#0a0d13");
    if (window.AndroidBridge && AndroidBridge.setTheme) {
      try { AndroidBridge.setTheme(t); } catch (e) { /* 非 Android 环境 */ }
    }
    // canvas 用的是取色后的字面值，换肤后必须重绘
    render();
  }
  $("#btnTheme").addEventListener("click", function () {
    applyTheme(theme() === "dark" ? "light" : "dark");
  });

  /* 取 CSS 变量（canvas 不能直接用 var()） */
  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  /* ------------------------------------------------------------ 折线图 */
  function drawSpark(canvas, lines, opts) {
    var dpr = window.devicePixelRatio || 1;
    var w = canvas.clientWidth, h = canvas.clientHeight || 92;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    var ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    var all = [];
    lines.forEach(function (l) { l.points.forEach(function (p) { all.push(p[1]); }); });
    if (!all.length) return;
    var lo = Math.min.apply(null, all), hi = Math.max.apply(null, all);
    if (hi - lo < 1e-9) { hi = lo + 0.01; }
    var pad = (hi - lo) * 0.12;
    lo -= pad; hi += pad;

    var n = lines[0].points.length;
    function X(i) { return (i / Math.max(n - 1, 1)) * w; }
    function Y(v) { return h - ((v - lo) / (hi - lo)) * (h - 6) - 3; }

    // 主序列：渐变面积
    var main = lines[0];
    var grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, opts.fillTop || "rgba(91,140,255,0.34)");
    grad.addColorStop(1, opts.fillBottom || "rgba(91,140,255,0)");
    ctx.beginPath();
    main.points.forEach(function (p, i) {
      if (i === 0) ctx.moveTo(X(i), Y(p[1]));
      else ctx.lineTo(X(i), Y(p[1]));
    });
    ctx.lineTo(X(main.points.length - 1), h);
    ctx.lineTo(0, h);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    // 线条
    lines.forEach(function (l, idx) {
      ctx.beginPath();
      l.points.forEach(function (p, i) {
        if (i === 0) ctx.moveTo(X(i), Y(p[1]));
        else ctx.lineTo(X(i), Y(p[1]));
      });
      ctx.lineWidth = idx === 0 ? 2 : 1.3;
      ctx.strokeStyle = l.color;
      ctx.lineJoin = "round";
      if (idx > 0) ctx.setLineDash([3, 3]);
      ctx.stroke();
      ctx.setLineDash([]);
    });

    // 末端点
    var last = main.points[main.points.length - 1];
    ctx.beginPath();
    ctx.arc(X(main.points.length - 1), Y(last[1]), 2.6, 0, Math.PI * 2);
    ctx.fillStyle = main.color;
    ctx.fill();
  }

  /* ------------------------------------------------------------ 环形图 */
  function drawDonut(canvas, items) {
    var dpr = window.devicePixelRatio || 1;
    var size = 132;
    canvas.width = size * dpr;
    canvas.height = size * dpr;
    var ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, size, size);

    var cx = size / 2, cy = size / 2, r = 52, thick = 15;
    var total = items.reduce(function (a, b) { return a + b.value; }, 0) || 1;
    var start = -Math.PI / 2;
    items.forEach(function (it) {
      var ang = (it.value / total) * Math.PI * 2;
      ctx.beginPath();
      ctx.arc(cx, cy, r, start + 0.012, start + ang - 0.012);
      ctx.lineWidth = thick;
      ctx.lineCap = "butt";
      ctx.strokeStyle = it.color;
      ctx.stroke();
      start += ang;
    });

    ctx.fillStyle = cssVar("--txt");
    ctx.font = "700 20px -apple-system, 'PingFang SC', sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(items.length + " 只", cx, cy - 7);
    ctx.fillStyle = cssVar("--txt-3");
    ctx.font = "500 10.5px -apple-system, 'PingFang SC', sans-serif";
    ctx.fillText("当前持仓", cx, cy + 13);
  }

  var CAT_COLOR = {
    宽基: "--c-broad", 行业: "--c-industry", 主题: "--c-theme",
    跨境: "--c-overseas", 商品: "--c-commodity", 债券: "--c-bond", 转债: "--c-convert",
  };
  function catColor(name) {
    var v = cssVar(CAT_COLOR[name] || "--c-broad");
    return v || "#5b8cff";
  }

  /* 环形图专用色板：持仓里常出现同类别（如两只跨境），
     按类别取色会让相邻扇区同色，故改用互不重复的固定序列。 */
  var PIE = ["#5b8cff", "#2ecf9a", "#e0b355", "#a07aff", "#f97316", "#4dd0e1"];
  function pieColor(i) { return PIE[i % PIE.length]; }

  /* ------------------------------------------------------------ 页面：概览 */
  var period = "all";

  function seriesFor(name) {
    var s = (OV.series || []).filter(function (x) { return x.name === name; })[0];
    return s ? s.data : [];
  }

  function cutoff() {
    if (period === "1y") return "2025-10-23";
    if (period === "3y") return "2023-10-23";
    return "0000-00-00";
  }

  function sliceFrom(data) {
    var c = cutoff();
    return data.filter(function (p) { return p[0] >= c; });
  }

  function pageOverview() {
    var perf = OV.perf || [];
    var self = perf.filter(function (p) { return p.name === SELF; })[0] || {};
    var bench = perf.filter(function (p) { return p.name === BENCH; })[0] || {};

    // 周期筛选后的累计收益（用净值序列重算，而不是复用全期数字）
    var sp = sliceFrom(seriesFor(SELF));
    var bp = sliceFrom(seriesFor(BENCH));
    var cum = sp.length > 1 ? sp[sp.length - 1][1] / sp[0][1] - 1 : self.cum;
    var bCum = bp.length > 1 ? bp[bp.length - 1][1] / bp[0][1] - 1 : bench.cum;
    var periodLabel = period === "all" ? "2019-01 至今" : period === "3y" ? "近 3 年" : "近 1 年";

    var annual = OV.annual || {};
    var years = Object.keys(annual[SELF] || {}).sort();

    var html = "";

    /* ---- 主卡 ---- */
    html += '<section class="hero">' +
      '<div class="hero__label">累计收益 ' + qmark("cum") + '</div>' +
      '<div class="hero__value num">' + pct(cum, 1) + '</div>' +
      '<div class="hero__row">' +
        '<span class="hero__delta num" style="' +
          (cum - bCum >= 0 ? "background:rgba(46,207,154,.16);color:#4fe0b0"
                           : "background:rgba(255,90,103,.16);color:#ff8b95") + '">' +
          spct(cum - bCum, 1) + ' 超额</span>' +
        '<span class="hero__hint">' + periodLabel + ' · 基准 ' + pct(bCum, 1) + '</span>' +
      '</div>' +
      '<div class="hero__spark"><canvas id="spark"></canvas></div>' +
      '<dl class="hero__foot">' +
        '<div class="hero__stat"><dt>年化' + qmark("cagr") + '</dt><dd>' + pct(self.cagr) + '</dd></div>' +
        '<div class="hero__stat"><dt>最大回撤' + qmark("mdd") + '</dt><dd>' + pct(self.mdd) + '</dd></div>' +
        '<div class="hero__stat"><dt>夏普' + qmark("sharpe") + '</dt><dd>' + num(self.sharpe) + '</dd></div>' +
      '</dl>' +
    '</section>';

    /* ---- 周期切换 ---- */
    html += '<div class="mt12"><div class="segmented" id="period">' +
      '<button data-p="all" class="' + (period === "all" ? "is-active" : "") + '">全部</button>' +
      '<button data-p="3y" class="' + (period === "3y" ? "is-active" : "") + '">近 3 年</button>' +
      '<button data-p="1y" class="' + (period === "1y" ? "is-active" : "") + '">近 1 年</button>' +
    '</div></div>';

    /* ---- KPI ---- */
    html += '<div class="section-label">关键指标</div><div class="kpi-grid">' +
      kpi("年化超额", pct(self.cagr - bench.cagr, 1), "累计 " + pct(self.excess, 0), "excess", (self.cagr - bench.cagr) > 0 ? "up" : "down") +
      kpi("卡玛比率", num(self.calmar), "收益 / 回撤", "calmar", "") +
      kpi("年均换手", num(self.turnover, 1) + " 倍", "每年换 " + num(self.turnover, 0) + " 遍", "turnover", "") +
      kpi("信息比率", num(self.ir), "超额稳定性", "ir", "") +
    '</div>';

    /* ---- 年度收益 ---- */
    var maxAbs = 1;
    years.forEach(function (y) {
      maxAbs = Math.max(maxAbs, Math.abs(annual[SELF][y] || 0), Math.abs(annual[BENCH][y] || 0));
    });
    html += '<div class="section-label">年度收益<span class="legend">' +
      '<span><i style="background:var(--accent)"></i>TGAR</span>' +
      '<span><i style="background:var(--txt-3)"></i>沪深300</span></span></div>';
    html += '<div class="card"><div class="bars">';
    years.forEach(function (y) {
      var a = annual[SELF][y] || 0, b = annual[BENCH][y] || 0;
      html += '<div class="bars__group"><div class="bars__stack">' +
        barEl(a, maxAbs, "var(--up)", "a") + barEl(b, maxAbs, "var(--txt-3)", "b") +
        '</div><div class="bars__year">' + String(y).slice(2) + '</div></div>';
    });
    html += '</div><div class="card__sub mt8">柱高即当年涨跌幅，红涨绿跌。' +
      '2026 为年初至今，尚未走完完整年度。</div></div>';

    /* ---- 策略对比 ---- */
    var order = [SELF, "S1-动量基线", "S2-双动量", "S3-多因子"];
    html += '<div class="section-label">四个策略横向对比</div><div class="card"><div class="rows">';
    order.forEach(function (nm) {
      var p = perf.filter(function (x) { return x.name === nm; })[0];
      if (!p) return;
      var isSelf = nm === SELF;
      html += '<div class="row" data-strat="' + esc(p.name) + '">' +
        '<div class="row__main"><div class="row__name">' +
          (isSelf ? '<span style="color:var(--accent)">● </span>' : "") + esc(p.name) + '</div>' +
        '<div class="row__meta">回撤 ' + pct(p.mdd) + ' · 卡玛 ' + num(p.calmar) + '</div></div>' +
        '<div class="row__value"><b class="' + cls(p.cagr) + '">' + pct(p.cagr) + '</b>' +
        '<small>年化 · 夏普 ' + num(p.sharpe) + '</small></div></div>';
    });
    html += '</div></div>';

    html += '<div class="notice mt12">' +
      '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.7"/><path d="M12 8.4v5.2M12 16.4v.1" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></svg>' +
      '<div>超额收益<b>几乎全部来自 2023-2026 段</b>，2019-2022 段跑输基准但回撤更浅。' +
      '这是一个「熊市抗跌 + 结构性行情抓轮动」的模型，不是全天候跑赢。</div></div>';

    return html;
  }

  function kpi(label, value, sub, term, tone) {
    return '<div class="kpi"><div class="kpi__label">' + esc(label) + qmark(term) + '</div>' +
      '<div class="kpi__value num ' + (tone || "") + '">' + value + '</div>' +
      '<div class="kpi__sub">' + esc(sub) + '</div></div>';
  }

  function barEl(v, maxAbs, color, slot) {
    // 半轴 = 50%，所以柱高是 |v|/maxAbs 的一半
    var h = Math.max(0.8, (Math.abs(v) / maxAbs) * 50);
    var neg = v < 0;
    return '<i class="bar bar--' + slot + ' ' + (neg ? "is-neg" : "is-pos") + '" style="' +
      'height:' + h + '%;background:' + (neg ? "var(--down)" : color) + ';' +
      (color === "var(--txt-3)" ? "opacity:.42;" : "") + '"></i>';
  }

  /* ------------------------------------------------------------ 页面：今日 */
  var cash = 100000;

  function pageToday() {
    var rows = (SIG.rows || []).slice().sort(function (a, b) { return b.weight - a.weight; });
    var tw = rows.reduce(function (a, r) { return a + r.weight; }, 0) || 1;
    var regime = SIG.regime, expo = SIG.exposure;
    /* 仓位结构：权益 / 债券 / 现金。以前只有一句"风险资产仓位 50%"，
       用户看到下面 100% 全买满会以为"说好半仓怎么买满了"——
       「半仓」指权益占一半，另一半买债券，账户始终满仓。 */
    var _rows0 = SIG.rows || [];
    var _eqSum = _rows0.reduce(function (a, r) { return a + (r.category === "债券" ? 0 : (r.weight || 0)); }, 0);
    var _bdSum = _rows0.reduce(function (a, r) { return a + (r.category === "债券" ? (r.weight || 0) : 0); }, 0);
    // 老版本 data.js 没有 equity_weight 字段时按类别回推，别让条形图静默变空
    var eqPct = Math.round((SIG.equity_weight != null ? SIG.equity_weight : _eqSum) * 100);
    var bondPct = Math.round((SIG.bond_weight != null ? SIG.bond_weight : _bdSum) * 100);
    var cashPct = Math.max(0, 100 - eqPct - bondPct);
    /* 闸门两档，标签也只留两态（>=0.7 满敞口，否则半敞口） */
    var stateTxt = regime == null ? "—" : regime >= 0.7 ? "进攻" : "防守";
    var stateCls = regime >= 0.7 ? "up" : "down";

    var html = "";

    /* 三态提示。清单在两次调仓之间本就该不变，"持有中"不报警；
       只有"该换仓了"才需要用户动手。 */
    var ICON_INFO = '<svg viewBox="0 0 24 24" fill="none"><path d="M12 8v5M12 16.6v.1" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.6"/></svg>';
    if (SIG.status === "due") {
      html += '<div class="notice notice--warn" style="margin-bottom:12px">' + ICON_INFO +
        '<div><b>调仓日已过（' + esc(SIG.next_reb) + '）却没更新</b>，这份清单是 ' + esc(SIG.reb_date) +
        ' 定的，已经作废。请先更新行情再重算。</div></div>';
    } else if (SIG.status === "today") {
      html += '<div class="notice notice--warn" style="margin-bottom:12px">' + ICON_INFO +
        '<div><b>今天是调仓日</b>：收盘后更新行情重算，明天开盘按新清单换仓。</div></div>';
    } else if (SIG.status === "exec_today") {
      html += '<div class="notice notice--warn" style="margin-bottom:12px">' + ICON_INFO +
        '<div><b>今天开盘就要换仓</b>：按 ' + esc(SIG.reb_date) + ' 收盘定下的清单买，就是下面这份。</div></div>';
    } else if (SIG.status === "pending") {
      html += '<div class="notice" style="margin-bottom:12px">' +
        '<svg viewBox="0 0 24 24" fill="none"><path d="M12 8v5M12 16.6v.1" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.6"/></svg>' +
        '<div><b>' + esc(SIG.exec_date) + '（周' + esc(SIG.exec_weekday) + '）开盘执行</b>：' +
        '这是 ' + esc(SIG.reb_date) + ' 收盘定下的清单。</div></div>';
    }

    /* 数据/行情状态条 */
    var liveOn = LIVE.at > 0;
    var liveTime = liveOn
      ? new Date(LIVE.at).toTimeString().slice(0, 5)
      : "";
    var isToday = HIST.dates.length && SIG.date === HIST.dates[HIST.dates.length - 1];
    html += '<div class="srcbar">' +
      '<div class="srcbar__main">' +
        '<div class="srcbar__line">调仓 <b>' + esc(SIG.reb_date || SIG.date) + '</b> 收盘' +
        (liveOn ? ' · <b>' + liveTime + '</b> 实时价' : '') +
        (SYNC.intraday ? ' · <b style="color:var(--gold)">盘中</b>' : '') + '</div>' +
        '<div class="srcbar__line dim">' +
          (SYNC.intraday
            ? '未收盘 · 收盘后并入今日 K 线'
            : (isToday ? '本机计算' : '本地缓存') +
              ' · 行情 ' + esc(HIST.dates[HIST.dates.length - 1] || '—')) +
          (SYNC.lastAt ? ' · ' + fmtTime(SYNC.lastAt) : '') +
        '</div>' +
      '</div>' +
      '<button class="iconbtn iconbtn--sm" id="btnSyncTop" aria-label="更新行情">' +
        '<svg viewBox="0 0 24 24" fill="none"><path d="M20 12a8 8 0 10-2.4 5.7M20 12V6m0 6h-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>' +
      '</button>' +
    '</div>';

    /* 调仓日历：哪天定的 / 哪天买 / 下次哪天换 */
    html += '<div class="card"><div class="card__head"><div>' +
      '<div class="card__title">当前应持有</div>' +
      '</div><span class="tag t-' + (regime >= 0.7 ? "行业" : "债券") + '">' + stateTxt + '</span></div>' +
      '<div class="sched">' +
        '<div class="sched__step is-done"><span>调仓决策</span><b>' + esc(SIG.reb_date) + '</b><i>周' + esc(SIG.reb_weekday) + ' 收盘</i></div>' +
        '<i class="sched__line"></i>' +
        '<div class="sched__step ' + ((SIG.status === "pending" || SIG.status === "exec_today") ? "is-next" : "is-done") + '"><span>开盘买入</span><b>' + esc(SIG.exec_date) + '</b><i>' +
          (SIG.status === "exec_today" ? "就是今天"
            : SIG.status === "pending" ? "待执行"
            : (SIG.held_days != null ? "已持有 " + SIG.held_days + " 个交易日" : "已执行")) + '</i></div>' +
        '<i class="sched__line"></i>' +
        '<div class="sched__step ' + ((SIG.status === "due" || SIG.status === "today") ? "is-due" : "is-next") + '"><span>下次换仓</span><b>' + esc(SIG.next_reb || "—") + '</b><i>' +
          (SIG.status === "today" ? "就是今天"
            : SIG.status === "due" ? "已过期，请重算"
            : (SIG.days_to_next != null ? "还有 " + SIG.days_to_next + " 个交易日" : "—")) + '</i></div>' +
      '</div>' +
      /* 强弱分只占一行：原来用一个整块 KPI 盒，右侧一半是空白 */
      '<div class="strength">' +
        '<span class="strength__label">大盘强弱' + qmark("regime") + '</span>' +
        '<b class="' + stateCls + '">' + (regime == null ? "—" : regime.toFixed(1)) + '</b>' +
        '<span class="dim">/ 1.0</span>' +
      '</div>' +
      '<div class="posbar-wrap">' +
        '<div class="posbar">' +
          '<i class="posbar__eq" style="width:' + eqPct + '%"></i>' +
          '<i class="posbar__bond" style="width:' + bondPct + '%"></i>' +
          '<i class="posbar__cash" style="width:' + cashPct + '%"></i>' +
        '</div>' +
        '<div class="posbar__legend">' +
          '<span><i class="sw sw--eq"></i>权益 ' + eqPct + '%</span>' +
          '<span><i class="sw sw--bond"></i>债券 ' + bondPct + '%</span>' +
          (cashPct > 0 ? '<span><i class="sw sw--cash"></i>现金 ' + cashPct + '%</span>' : '') +
        '</div>' +
      '</div></div>';

    /* 资金输入 */
    html += '<div class="section-label">按你的资金算买多少</div>';
    html += '<div class="quick" id="quick">' +
      [50000, 100000, 200000, 500000].map(function (v) {
        return '<button class="chip' + (cash === v ? " is-on" : "") + '" data-cash="' + v + '">' +
          (v >= 10000 ? v / 10000 + " 万" : v) + '</button>';
      }).join("") + '</div>';

    /* 订单列表 */
    html += '<div class="card mt12" id="orderCard"><div class="card__head"><div>' +
      '<div class="card__title">买入清单</div></div>' +
      '<span class="card__sub num" id="orderCount"></span></div><div id="orders"></div>' +
      '<div id="orderFoot" class="mt8"></div></div>';

    /* 资金条 */
    html += '<div class="cashbar"><span class="unit">本金</span>' +
      '<input id="cash" inputmode="numeric" value="' + cash + '" aria-label="投入本金">' +
      '<span class="unit">元</span></div>';

    /* 操作 */
    html += '<div class="mt12"><button class="btn btn--primary btn--block" id="copy">' +
      '<svg width="17" height="17" viewBox="0 0 24 24" fill="none"><rect x="9" y="9" width="11" height="11" rx="2.4" stroke="currentColor" stroke-width="1.9"/><path d="M15 5.6A2.6 2.6 0 0012.4 4H6.6A2.6 2.6 0 004 6.6v5.8c0 1.1.7 2 1.7 2.4" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></svg>' +
      '复制清单</button></div>';

    /* 提示 */
    html += '<div class="section-label">需要留意</div><div class="card" style="padding:4px 16px">' +
      '<div class="row"><div class="row__main"><div class="row__name" style="font-size:13px">跨境 ETF 溢价</div>' +
      '<div class="row__meta" style="white-space:normal;line-height:1.55">' +
      '纳指类常有 1%~3% 场内溢价，溢价高时改用场外联接，或这一期先跳过。</div></div></div></div>';

    html += '<div class="notice mt12">' +
      '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.7"/><path d="M12 8.4v5.2M12 16.4v.1" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></svg>' +
      '<div>参考价 ' + esc(SIG.date) + ' 收盘 · <b>不构成投资建议</b></div></div>';

    return html;
  }

  function renderOrders() {
    var rows = (SIG.rows || []).slice().sort(function (a, b) { return b.weight - a.weight; });
    var tw = rows.reduce(function (a, r) { return a + r.weight; }, 0) || 1;
    var total = 0, list = "";

    rows.forEach(function (r) {
      var w = r.weight / tw;
      var target = cash * w;
      var px = priceOf(r);              // 有实时价就用实时价
      var lotCost = px * 100;
      var lots = Math.floor(target / lotCost);
      var shares = lots * 100;
      var actual = shares * px;
      total += actual;
      var short = lots === 0;

      list += '<div class="order"' + (short ? ' style="opacity:.62"' : "") + '>' +
        '<div class="order__name">' + esc(r.name) +
          (r.price_stale ? ' <span class="up" title="行情未更新">⚠</span>' : "") + '</div>' +
        '<div class="order__amt"><b>' + (short ? '<span class="up">不足1手</span>' : money(actual)) + '</b>' +
          '<small>' + (short ? "资金不够" : lots + " 手") + '</small></div>' +
        '<div class="order__sub">' +
          '<span class="mono">' + esc(r.code) + '</span>' +
          '<span class="tag t-' + esc(r.category) + '">' + esc(r.category) + '</span>' +
          '<span>' + (w * 100).toFixed(1) + '%</span>' +
          '<span' + (liveOf(r.code) ? ' style="color:var(--accent)"' : '') + '>' +
            num(px, 3) + (liveOf(r.code) ? ' 实时' : '') + '</span>' +
        '</div></div>';
    });

    var left = cash - total;
    var used = cash > 0 ? (total / cash) * 100 : 0;

    var foot = '<div class="kv"><span>合计投入</span><b>' + money(total) +
      ' <span class="dim" style="font-weight:500">· 已用 ' + used.toFixed(1) + '%</span></b></div>' +
      '<div class="kv"><span>剩余现金</span><b>' + money(left) + '</b></div>';

    var minCash = Math.max.apply(null, rows.map(function (r) {
      return priceOf(r) * 100 / (r.weight / tw || 1);
    }));
    if (cash > 0 && cash < minCash) {
      foot += '<div class="notice notice--warn mt8"><svg viewBox="0 0 24 24" fill="none">' +
        '<path d="M12 8v5M12 16.6v.1" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/>' +
        '<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.6"/></svg>' +
        '<div><b>资金不足</b>：这套配置至少需要 ' + money(minCash) +
        ' 才能每只买满 1 手，当前会导致部分标的买不进。</div></div>';
    }

    var cnt = document.getElementById("orders");
    if (cnt) cnt.innerHTML = list;
    var f = document.getElementById("orderFoot");
    if (f) f.innerHTML = foot;
    var c = document.getElementById("orderCount");
    if (c) c.textContent = rows.length + " 只";
  }

  function copyList() {
    var rows = (SIG.rows || []).slice().sort(function (a, b) { return b.weight - a.weight; });
    var tw = rows.reduce(function (a, r) { return a + r.weight; }, 0) || 1;
    var lines = [];
    var total = 0;
    rows.forEach(function (r, i) {
      var w = r.weight / tw;
      var px = priceOf(r);
      var lots = Math.floor((cash * w) / (px * 100));
      total += lots * 100 * px;
      lines.push((i + 1) + ". " + r.name + " (" + r.code + ")  " + (w * 100).toFixed(1) +
        "%  买 " + lots + " 手 / " + lots * 100 + " 份  参考价 " + px.toFixed(3) +
        (liveOf(r.code) ? "（实时）" : ""));
    });
    var text = [
      "ETF 轮动策略 · " + (SIG.reb_date || SIG.date) + " 收盘信号",
      "执行日：" + SIG.exec_date + "（周" + SIG.exec_weekday + "）开盘",
      "下次换仓：" + (SIG.next_reb || "—"),
      "本金：" + money(cash),
      "----------------------------------------",
    ].concat(lines).concat([
      "----------------------------------------",
      "合计约 " + money(total) + "，剩余 " + money(cash - total),
    ]).join("\n");

    if (window.AndroidBridge && AndroidBridge.copyText) {
      try { AndroidBridge.copyText(text); toast("已复制，可直接对照下单"); return; } catch (e) { /* 回落 */ }
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { toast("已复制"); },
        function () { legacyCopy(text); });
      return;
    }
    legacyCopy(text);
  }

  function legacyCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); toast("已复制"); }
    catch (e) { toast("复制失败，请手动截图"); }
    document.body.removeChild(ta);
  }

  /* ------------------------------------------------------------ 页面：持仓 */
  var contribOpen = false;

  /* ================= 我的持仓（本地，不复位） =================
   * 与回测持仓完全分开：
   *   - 存 localStorage 的 etfq-hold，**不复用** etfq-store。
   *     etfq-store 里有 ENGINE_VER，引擎口径一变就会被丢弃；
   *     用户手抄的持仓被顺手清掉是不可接受的。
   *   - 价格优先取盘中实时价，没有则用内置历史的最后收盘价。
   */
  var HOLD_KEY = "etfq-hold";
  var holdTab = "mine";          // mine / backtest

  function loadHold() {
    try {
      var raw = localStorage.getItem(HOLD_KEY);
      if (!raw) return { cash: 0, items: [] };
      var o = JSON.parse(raw);
      if (!o || typeof o !== "object") return { cash: 0, items: [] };
      return {
        cash: isNum(+o.cash) ? +o.cash : 0,
        items: Array.isArray(o.items) ? o.items.filter(function (i) { return i && i.code; }) : []
      };
    } catch (e) { return { cash: 0, items: [] }; }
  }
  function saveHold(h) {
    try { localStorage.setItem(HOLD_KEY, JSON.stringify(h)); } catch (e) { /* 隐私模式 */ }
  }

  /** 某只标的的现价与昨收：实时快照优先，回落到内置历史最后两根。 */
  function holdQuote(code) {
    var out = { price: 0, prev: 0, date: "", live: false };
    var i = HIST.codes.indexOf(code);
    if (i >= 0) {
      var c = HIST.close[i], n = HIST.dates.length, k;
      for (k = n - 1; k >= 0; k--) {
        if (isNum(c[k])) { out.price = c[k]; out.date = HIST.dates[k]; break; }
      }
      for (k = k - 1; k >= 0; k--) {
        if (isNum(c[k])) { out.prev = c[k]; break; }
      }
    }
    var live = LIVE.prices[code];
    if (live && isNum(live.price) && live.price > 0) {
      out.price = live.price;
      out.prev = isNum(live.prev) && live.prev > 0 ? live.prev : out.prev;
      out.date = localDate();
      out.live = true;
    }
    return out;
  }

  function holdValue() {
    var h = loadHold();
    var today = localDate();
    var rows = [], mv = 0, costSum = 0, daySum = 0, missing = [];

    h.items.forEach(function (it) {
      var shares = +it.shares || 0;
      var cost = +it.cost || 0;
      var cv = shares * cost;
      costSum += cv;
      var q = holdQuote(it.code);
      if (!(q.price > 0)) {
        missing.push(it.name || it.code);
        rows.push({ code: it.code, name: it.name || it.code, shares: shares, cost: cost,
          buy_date: it.buy_date || "", note: it.note || "",
          price: null, mv: null, pnl: null, pnl_pct: null, day: null, pos: null });
        return;
      }
      var m = shares * q.price;
      mv += m;
      var dpl = null;
      if (it.buy_date && it.buy_date >= today) dpl = shares * (q.price - cost);
      else if (q.prev > 0) dpl = shares * (q.price - q.prev);
      if (dpl !== null) daySum += dpl;
      rows.push({
        code: it.code, name: it.name || it.code, shares: shares, cost: cost,
        buy_date: it.buy_date || "", note: it.note || "",
        price: q.price, prev: q.prev, date: q.date, live: q.live,
        mv: m, pnl: m - cv, pnl_pct: cv > 0 ? (m - cv) / cv : null,
        day: dpl, pos: null
      });
    });

    var total = mv + h.cash;
    rows.forEach(function (r) { if (r.mv !== null && total > 0) r.pos = r.mv / total; });
    rows.sort(function (a, b) { return (b.mv || -1) - (a.mv || -1); });
    return { rows: rows, cash: h.cash, mv: mv, cost: costSum, pnl: mv - costSum,
      day: daySum, total: total, missing: missing, count: h.items.length };
  }

  /** 与今日信号对比，和桌面端 holdings.advise 同一口径（阈值 2 个百分点）。 */
  function holdAdvice(v) {
    var sigRows = (D.signal && D.signal.rows) || [];
    var target = {}, held = {};
    sigRows.forEach(function (r) {
      if (+r.weight > 0) target[r.code] = { weight: +r.weight, name: r.name, price: +r.price || 0 };
    });
    v.rows.forEach(function (r) { held[r.code] = r; });

    var plan = [], THRESH = 0.02;
    Object.keys(target).forEach(function (code) {
      var t = target[code], h = held[code];
      var aw = h && h.pos ? h.pos : 0;
      var diff = t.weight - aw;
      var px = (h && h.price) || t.price;
      var delta = diff * v.total;
      var lots = px > 0 ? Math.floor(Math.abs(delta) / (px * 100)) : 0;
      var action = "hold";
      if (Math.abs(diff) < THRESH || lots === 0) action = h ? "hold" : "skip";
      else action = diff > 0 ? "add" : "cut";
      if (action !== "hold" && action !== "skip") {
        plan.push({ code: code, name: (h && h.name) || t.name, tgt: t.weight, act: aw,
          diff: diff, delta: delta, lots: lots, px: px, action: action });
      }
    });
    v.rows.forEach(function (h) {
      if (target[h.code]) return;
      plan.push({ code: h.code, name: h.name, tgt: 0, act: h.pos || 0,
        diff: -(h.pos || 0), delta: -(h.mv || 0), lots: Math.round(h.shares / 100),
        px: h.price, action: "exit" });
    });
    plan.sort(function (a, b) { return Math.abs(b.diff) - Math.abs(a.diff); });
    return plan;
  }

  /* 带正负号的金额。注意不能叫 money()——文件开头的 money() 是无符号的，
     同名函数声明会互相覆盖（后定义的赢），会把下单页的金额也改成带 + 号。 */
  function signedMoney(v) {
    if (!isNum(v)) return "—";
    return (v > 0 ? "+" : v < 0 ? "−" : "") + "¥" + Math.round(Math.abs(v)).toLocaleString("en-US");
  }

  var ACT_CN = { add: "加仓", cut: "减仓", exit: "清仓" };

  function myHoldHtml() {
    var v = holdValue();
    var plan = holdAdvice(v);
    var html = "";

    if (!v.count) {
      html += '<div class="card" style="padding:20px 16px">' +
        '<div style="font-size:15px;font-weight:700;margin-bottom:6px">还没有持仓</div>' +
        '<div style="font-size:12.5px;color:var(--txt-3);line-height:1.75">' +
        '记一次即可，之后自动按最新价算盈亏；数据只存本地。</div>' +
        '<div class="mt12" style="display:flex;gap:8px">' +
        '<button class="btn btn--block" id="btnHoldAdd" style="flex:1;height:42px">手动添加</button>' +
        '<button class="btn btn--block btn--primary" id="btnHoldBuild" style="flex:1;height:42px">按信号建仓</button>' +
        '</div></div>';
      return html;
    }

    /* 概览 */
    html += '<div class="kpi-grid">' +
      kpi("总资产", "¥" + Math.round(v.total).toLocaleString("en-US"), "现金 ¥" + Math.round(v.cash).toLocaleString("en-US"), "", "") +
      kpi("浮动盈亏", signedMoney(v.pnl), v.cost > 0 ? pct(v.pnl / v.cost, 1) : "—", "", cls(v.pnl)) +
      kpi("今日盈亏", signedMoney(v.day), v.total > 0 ? pct(v.day / (v.total - v.day), 1) : "—", "", cls(v.day)) +
      kpi("仓位", pct(v.total > 0 ? v.mv / v.total : 0, 1), v.count + " 只 · 成本 ¥" + Math.round(v.cost).toLocaleString("en-US"), "", "") +
      '</div>';

    /* 明细 */
    html += '<div class="section-label">我的持仓<span class="legend">按最新价估值</span></div>' +
      '<div class="card" style="padding:4px 16px"><div class="rows">';
    v.rows.forEach(function (r) {
      html += '<div class="row" data-hold="' + esc(r.code) + '">' +
        '<div class="row__main">' +
        '<div class="row__name">' + esc(r.name) + (r.live ? ' <span style="color:var(--up);font-size:10px">实时</span>' : "") + '</div>' +
        '<div class="row__meta"><span class="mono">' + esc(r.code) + '</span>' +
        (r.price === null
          ? '<span style="color:var(--gold)">暂无行情</span>'
          : '<span>' + num(r.cost, 3) + ' → ' + num(r.price, 3) + '</span>') +
        (r.buy_date ? '<span>' + esc(r.buy_date) + '</span>' : '') +
        '</div></div>' +
        '<div class="row__value"><b class="' + cls(r.pnl) + '">' + (r.pnl === null ? "—" : signedMoney(r.pnl)) + '</b>' +
        '<small class="' + cls(r.pnl_pct) + '">' + (r.pnl_pct === null ? "—" : spct(r.pnl_pct, 1)) + '</small></div>' +
        '</div>';
    });
    html += '</div></div>';

    if (v.missing.length) {
      html += '<div class="notice" style="margin-top:10px"><div>' +
        '<b>' + esc(v.missing.join("、")) + '</b> 没有行情数据，未计入市值与盈亏。' +
        '可能是新上市或代码不在内置清单里，可在电脑端查看。</div></div>';
    }

    /* 建议 */
    html += '<div class="section-label">按今日信号<span class="legend">差额 2% 以内不动</span></div>';
    if (!plan.length) {
      html += '<div class="card" style="padding:16px"><div style="font-size:12.5px;color:var(--txt-3);line-height:1.75">' +
        '<b style="color:var(--down)">与今日信号一致</b>，今天不用动。差额太小时买卖只会给券商交手续费。</div></div>';
    } else {
      html += '<div class="card" style="padding:4px 16px"><div class="rows">';
      plan.forEach(function (p) {
        html += '<div class="row">' +
          '<div class="row__main"><div class="row__name">' + esc(p.name) + '</div>' +
          '<div class="row__meta"><span>目标 ' + pct(p.tgt, 1) + ' · 现在 ' + pct(p.act, 1) + '</span>' +
          '<span class="' + cls(p.diff) + '">' + spct(p.diff, 1) + '</span></div></div>' +
          '<div class="row__value"><b class="' + cls(p.delta) + '">' + signedMoney(p.delta) + '</b>' +
          '<small>' + (p.lots ? p.lots + " 手 · " : "") +
          '<span class="pill ' + (p.action === "add" ? "pill--buy" : "pill--sell") + '">' + ACT_CN[p.action] + '</span></small></div>' +
          '</div>';
      });
      html += '</div></div>';
    }

    html += '<div class="mt12" style="display:flex;gap:8px">' +
      '<button class="btn btn--block" id="btnHoldAdd" style="flex:1;height:42px">添加 / 修改</button>' +
      '<button class="btn btn--block" id="btnHoldBuild" style="flex:1;height:42px">按信号建仓</button>' +
      '</div>';
    return html;
  }

  /* ---------- 持仓录入弹层 ---------- */
  function holdSheet(item) {
    var isEdit = !!item;
    var it = item || { code: "", shares: "", cost: "", buy_date: localDate(), note: "" };
    var html = '<form id="holdForm2" autocomplete="off">' +
      '<label class="hfield"><span>代码</span><input id="fCode" value="' + esc(it.code) + '" ' +
        (isEdit ? "readonly" : "") + ' placeholder="sh511090"></label>' +
      '<label class="hfield"><span>份额（份）</span><input id="fShares" inputmode="decimal" value="' + esc(it.shares) + '" placeholder="1000"></label>' +
      '<label class="hfield"><span>成本价（元）</span><input id="fCost" inputmode="decimal" value="' + esc(it.cost) + '" placeholder="120.576"></label>' +
      '<label class="hfield"><span>买入日</span><input id="fDate" type="date" value="' + esc(it.buy_date || "") + '"></label>' +
      '<label class="hfield"><span>备注</span><input id="fNote" maxlength="60" value="' + esc(it.note || "") + '" placeholder="可选"></label>' +
      '<div class="hint" id="fMsg"></div>' +
      '<button class="btn btn--block btn--primary mt12" type="submit" style="height:44px">' + (isEdit ? "保存修改" : "添加") + '</button>' +
      (isEdit ? '<button class="btn btn--block mt8" type="button" id="fDel" style="height:40px;color:var(--down)">删除这笔持仓</button>' : "") +
      '</form>';
    openSheet(isEdit ? "编辑持仓" : "添加持仓", html);

    $("#holdForm2").addEventListener("submit", function (e) {
      e.preventDefault();
      var code = $("#fCode").value.trim().toLowerCase();
      var shares = parseFloat($("#fShares").value);
      var cost = parseFloat($("#fCost").value);
      if (!/^(sh|sz)\d{6}$/.test(code)) { $("#fMsg").textContent = "代码格式应为 sh510300 / sz159915"; return; }
      if (!(shares > 0)) { $("#fMsg").textContent = "份额必须大于 0"; return; }
      if (!(cost > 0)) { $("#fMsg").textContent = "成本价必须大于 0"; return; }
      var h = loadHold();
      h.items = h.items.filter(function (x) { return x.code !== code; });
      h.items.push({
        code: code,
        name: nameOfCode(code),
        shares: shares,
        cost: cost,
        buy_date: $("#fDate").value || "",
        note: $("#fNote").value.trim().slice(0, 60)
      });
      saveHold(h);
      closeSheet();
      render();
      toast(isEdit ? "已更新" : "已添加");
    });

    var del = $("#fDel");
    if (del) del.addEventListener("click", function () {
      if (!window.confirm("删除这笔持仓？")) return;
      var h = loadHold();
      h.items = h.items.filter(function (x) { return x.code !== it.code; });
      saveHold(h);
      closeSheet();
      render();
      toast("已删除");
    });
  }

  function nameOfCode(code) {
    /* 必须查 D.history.meta：mergeBars 之后 HIST 被重建成
       {dates, codes, close, vol}，meta 已经不在里面了。 */
    var m = (D.history && D.history.meta && D.history.meta[code]) || null;
    if (m && m.name) return m.name;
    var sig = (D.signal && D.signal.rows) || [];
    for (var i = 0; i < sig.length; i++) {
      if (sig[i].code === code && sig[i].name) return sig[i].name;
    }
    return code;
  }

  function buildBySignalSheet() {
    var sigRows = (D.signal && D.signal.rows) || [];
    var html = '<form id="buildForm" autocomplete="off">' +
      '<label class="hfield"><span>准备投入的金额（元）</span>' +
      '<input id="fAmount" inputmode="numeric" value="100000" placeholder="100000"></label>' +
      '<div class="hint">按今日清单的占比换算成整手买入，买不满的留作现金。' +
      '会<b>覆盖</b>现有持仓，成本按最新收盘价计。</div>' +
      '<button class="btn btn--block btn--primary mt12" type="submit" style="height:44px">建仓</button>' +
      '</form>';
    openSheet("按今日信号建仓", html);

    $("#buildForm").addEventListener("submit", function (e) {
      e.preventDefault();
      var amount = parseFloat($("#fAmount").value);
      if (!(amount > 0)) { return; }
      if (!window.confirm("确认用 ¥" + Math.round(amount).toLocaleString("en-US") + " 建仓？会覆盖现有持仓。")) return;
      var items = [], spent = 0, notes = [];
      sigRows.forEach(function (r) {
        var w = +r.weight, px = +r.price;
        if (!(w > 0) || !(px > 0)) return;
        var budget = amount * w, lotCost = px * 100;
        var lots = Math.floor(budget / lotCost);
        if (lots <= 0) {
          notes.push({
            kind: "lot_too_big", name: r.name, code: r.code,
            msg: "一手需 ¥" + Math.round(lotCost).toLocaleString("en-US") +
                 "，超过该只预算 ¥" + Math.round(budget).toLocaleString("en-US") +
                 "，整只未买入（目标 " + (w * 100).toFixed(1) + "%）"
          });
          return;
        }
        var sh = lots * 100;
        spent += sh * px;
        items.push({ code: r.code, name: r.name, shares: sh, cost: px, buy_date: localDate(), note: "按信号建仓" });
        var actualW = (sh * px) / amount;
        if (w - actualW > 0.01) {
          notes.push({
            kind: "underweight", name: r.name, code: r.code,
            msg: "取整后实际权重 " + (actualW * 100).toFixed(1) + "%，低于目标 " +
                 (w * 100).toFixed(1) + "%（少配 " + ((w - actualW) * 100).toFixed(1) + "pp）"
          });
        }
      });
      if (!items.length) {
        toast("这个金额买不到一手" + (notes.length ? "：" + notes[0].msg : ""));
        return;
      }
      saveHold({ cash: Math.max(0, amount - spent), items: items });
      closeSheet();
      holdTab = "mine";
      render();
      if (notes.length) {
        var miss = notes.filter(function (n) { return n.kind === "lot_too_big"; }).length;
        var under = notes.length - miss;
        toast("已建仓 " + items.length + " 只，但 " +
              [miss ? miss + " 只未买入" : "", under ? under + " 只欠配" : ""]
                .filter(Boolean).join("、"));
        setTimeout(function () {
          alert("整手约束提示（回测按份额成交，实盘按 100 份一手）\n\n" +
                notes.map(function (n) { return "· " + n.name + "：" + n.msg; }).join("\n") +
                "\n\n加大投入资金或减少持仓只数可缩小缺口。");
        }, 320);
      } else {
        toast("已建仓 " + items.length + " 只");
      }
    });
  }

  function afterMyHold() {
    var add = $("#btnHoldAdd");
    if (add) add.addEventListener("click", function () {
      var v = holdValue();
      // 0 或 1 笔时没有"选哪只"的必要，直接进表单
      if (v.count <= 1) { holdSheet(v.count ? v.rows[0] : null); return; }
      openHoldPicker();
    });
    var build = $("#btnHoldBuild");
    if (build) build.addEventListener("click", buildBySignalSheet);

    document.querySelectorAll("[data-hold]").forEach(function (el) {
      el.addEventListener("click", function () {
        var v = holdValue();
        var r = null;
        v.rows.forEach(function (x) { if (x.code === el.getAttribute("data-hold")) r = x; });
        if (r) holdSheet(r);
      });
    });
  }

  /** 有持仓时「添加/修改」先让用户选是改哪只，还是新加一笔。 */
  function openHoldPicker() {
    var v = holdValue();
    var html = '<div class="rows">';
    v.rows.forEach(function (r) {
      html += '<div class="row" data-pick="' + esc(r.code) + '" style="cursor:pointer">' +
        '<div class="row__main"><div class="row__name">' + esc(r.name) + '</div>' +
        '<div class="row__meta"><span class="mono">' + esc(r.code) + '</span>' +
        '<span>' + num(r.shares, 0) + ' 份 · 成本 ' + num(r.cost, 3) + '</span></div></div>' +
        '<div class="row__value"><b style="font-size:13px;color:var(--txt-3)">修改 ›</b></div></div>';
    });
    html += '</div><button class="btn btn--block btn--primary mt12" id="pickNew" style="height:42px">+ 新增一笔</button>';
    openSheet("选择要修改的持仓", html);
    document.querySelectorAll("[data-pick]").forEach(function (el) {
      el.addEventListener("click", function () {
        var code = el.getAttribute("data-pick");
        var r = null;
        v.rows.forEach(function (x) { if (x.code === code) r = x; });
        closeSheet();
        setTimeout(function () { holdSheet(r); }, 180);
      });
    });
    $("#pickNew").addEventListener("click", function () {
      closeSheet();
      setTimeout(function () { holdSheet(null); }, 180);
    });
  }

  function pageHoldings() {
    var seg = '<div class="segmented" id="holdSeg" style="margin-bottom:12px">' +
      '<button data-hseg="mine" class="' + (holdTab === "mine" ? "is-active" : "") + '">我的持仓</button>' +
      '<button data-hseg="backtest" class="' + (holdTab === "backtest" ? "is-active" : "") + '">回测账户</button>' +
      '</div>';
    return seg + (holdTab === "mine" ? myHoldHtml() : backtestHoldHtml());
  }

  function afterHoldings() {
    var seg = $("#holdSeg");
    if (seg) {
      seg.querySelectorAll("[data-hseg]").forEach(function (b) {
        b.addEventListener("click", function () {
          holdTab = b.getAttribute("data-hseg");
          render();
        });
      });
    }
    if (holdTab === "mine") { afterMyHold(); return; }

    var cur = (PF.current || []).slice().sort(function (a, b) { return b.weight - a.weight; });
    drawDonut($("#donut"), cur.map(function (r, i) {
      return { value: r.weight, color: pieColor(i) };
    }));
    var more = $("#moreContrib");
    if (more) more.addEventListener("click", function () { contribOpen = !contribOpen; render(); });
  }

  function backtestHoldHtml() {
    var cur = (PF.current || []).slice();
    var st = PF.stats || {};
    var contrib = (PF.contrib || []).slice();
    var trades = PF.trades || [];

    var totalW = cur.reduce(function (a, r) { return a + r.weight; }, 0) || 1;

    var html = '<div class="notice" style="margin-bottom:12px">' +
      '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.7"/><path d="M12 8.4v5.2M12 16.4v.1" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></svg>' +
      '<div>以下为<b>历史回测的模拟账户</b>（截至 ' + esc(PF.as_of) + '），用于验证策略，不是你的实盘。' +
      '要按今天信号下单请到「今日」页。</div></div>';

    /* 环形 + 图例 */
    html += '<div class="card"><div class="card__head"><div>' +
      '<div class="card__title">模拟账户最后持仓</div>' +
      '<div class="card__sub">共 ' + cur.length + ' 只 · 合计 ' + (totalW * 100).toFixed(0) + '% 仓位</div>' +
      '</div></div><div class="donut"><canvas id="donut"></canvas><div class="donut__legend">';
    cur.forEach(function (r, i) {
      html += '<div class="donut__item"><i style="background:' + pieColor(i) + '"></i>' +
        '<span>' + esc(r.name) + '</span><b>' + (r.weight * 100).toFixed(1) + '%</b></div>';
    });
    html += '</div></div></div>';

    /* 持仓明细 */
    html += '<div class="section-label">持有明细</div><div class="card" style="padding:4px 16px"><div class="rows">';
    cur.slice().sort(function (a, b) { return b.weight - a.weight; }).forEach(function (r) {
      html += '<div class="row"><div class="row__main">' +
        '<div class="row__name">' + esc(r.name) + '</div>' +
        '<div class="row__meta"><span class="mono">' + esc(r.code) + '</span>' +
        '<span class="tag t-' + esc(r.category) + '">' + esc(r.category) + '</span>' +
        '<span>' + esc(r.since) + ' 买入 · 已持 ' + r.days + ' 个交易日</span></div>' +
        '<div class="wbar mt8"><i style="width:' + (r.weight / totalW * 100).toFixed(1) + '%"></i></div>' +
        '</div><div class="row__value"><b>' + (r.weight * 100).toFixed(1) + '%</b>' +
        '<small class="' + cls(r.ret_since) + '">' + spct(r.ret_since, 1) + '</small></div></div>';
    });
    html += '</div></div>';

    /* 统计 */
    html += '<div class="kpi-grid mt12">' +
      kpi("年均换手", num(st.avg_turnover, 1) + " 倍", "每次约 " + Math.round(st.per_reb_turn * 100) + "%", "turnover", "") +
      kpi("年化成本", pct(st.cost_annual), "双边 " + num(st.fee_side * 100, 2) + "%", "cost", "down") +
      kpi("累计调仓", String(st.rebalances), "次 · 每 5 交易日", "", "") +
      kpi("平均持仓", num(st.avg_holdings, 1) + " 只", "曾持有 " + st.symbols + " 只", "", "") +
    '</div>';

    /* 贡献 */
    var top = contrib.slice(0, 6);
    var bottom = contrib.slice(-3).filter(function (r) { return top.indexOf(r) < 0; });
    var all = contribOpen ? contrib.slice(0, 12).concat(bottom) : top.concat(bottom);
    var maxAbs = 0.01;
    all.forEach(function (r) { maxAbs = Math.max(maxAbs, Math.abs(r.share)); });

    html += '<div class="section-label">收益贡献排行<span class="legend">合计 100%</span></div>';
    html += '<div class="card">';
    all.forEach(function (r, i) {
      var positive = r.share >= 0;
      var w = (Math.abs(r.share) / maxAbs) * 50;
      html += '<div class="contrib">' +
        '<div class="contrib__name">' + (r.now ? '<span style="color:var(--accent)">●</span> ' : "") + esc(r.name) + '</div>' +
        '<div class="contrib__track"><i class="contrib__fill" style="' +
          (positive ? "left:50%;width:" + w + "%" : "right:50%;width:" + w + "%") +
          ';background:' + (positive ? "var(--up)" : "var(--down)") + '"></i></div>' +
        '<div class="contrib__val ' + cls(r.share) + '">' + (r.share * 100).toFixed(1) + '%</div>' +
      '</div>';
    });
    html += '<button class="btn btn--block mt12" id="moreContrib" style="height:38px;font-size:13px">' +
      (contribOpen ? "收起" : "展开前 12 名") + '</button></div>';

    /* 调仓时间线 */
    html += '<div class="section-label">最近调仓</div><div class="card"><div class="tl">';
    trades.slice(0, 5).forEach(function (t) {
      var buys = t.buys.filter(function (b) { return b.to - b.from > 0.005; })
        .map(function (b) { return '<span class="pill pill--buy">' + esc(b.name) + ' +' + ((b.to - b.from) * 100).toFixed(1) + '%</span>'; }).join("");
      var sells = t.sells.filter(function (b) { return b.from - b.to > 0.005; })
        .map(function (b) { return '<span class="pill pill--sell">' + esc(b.name) + ' −' + ((b.from - b.to) * 100).toFixed(1) + '%</span>'; }).join("");
      html += '<div class="tl__item"><div class="tl__head"><span class="tl__date">' + esc(t.date) + '</span>' +
        '<span class="tl__turn">换手 ' + (t.turnover * 100).toFixed(1) + '%</span></div>' +
        '<div class="tl__chips">' + (buys + sells || '<span class="pill">仅再平衡</span>') + '</div></div>';
    });
    html += '</div></div>';

    return html;
  }

  /* ------------------------------------------------------------ 页面：验证 */
  var rsTab = "ladder";

  function tableSpec(key) {
    if (key === "ladder") {
      return {
        data: RS.ladder, cols: ["年化收益(CAGR)", "夏普比率", "最大回撤", "年均换手"],
        title: "增量归因", desc: "从最朴素的「只按 20 日涨幅排序、满仓、不过滤」起步，每加一个模块看收益与回撤怎么变。",
        hl: "仅降半仓",
        note: "结论：缓冲带同时改善收益与夏普，闸门是拿收益换回撤，熔断与多因子均为负贡献。",
      };
    }
    if (key === "ablation") {
      return {
        data: RS.ablation, cols: ["年化收益(CAGR)", "夏普比率", "最大回撤", "年均换手"],
        title: "模块消融", desc: "从功能全开的模型出发，每次关掉一个模块，对比谁在拖后腿。",
        note: "结论：回撤熔断与排名缓冲带影响最大；关闭趋势过滤会略微提高收益但回撤加深。",
      };
    }
    return {
      data: RS.subsample, cols: ["基准CAGR", "纯动量_CAGR", "最终模型_CAGR", "最终模型_MaxDD"],
      title: "子样本", desc: "把区间切成前后两段，检验收益是否只靠某一段行情。",
      note: "结论：2019-2022 跑输基准但回撤浅得多；超额几乎全部来自 2023-2026。",
    };
  }

  /* 口径卡的脚注：光写"样本内 X 视为上限"不够——那个区间建立在 alpha 之上，
     而 alpha 的 t 值只有 1.38。页面比 README 乐观，就是在骗看页面的人。 */
  function expectFoot(exp) {
    if (exp.sample == null) { return ""; }
    var s = "样本内 " + pct(exp.sample, 1);
    if (isNum(exp.alpha_ann) && isNum(exp.alpha_t)) {
      s += " · α " + pct(exp.alpha_ann, 1) + "(t=" + num(exp.alpha_t, 1) + ")";
    }
    return s;
  }

  function pageResearch() {
    var spec = tableSpec(rsTab);
    var d = spec.data;
    var exp = OV.expect || null;

    var html = "";
    /* 口径放在最前面：下面的表全是"在同一段历史上比大小"，
       不先说清 17.4% 是样本内上限，读者会把最好看的那个数当预期。 */
    if (exp && exp.label) {
      html += '<div class="kpi-grid">' +
        kpi("对外预期", exp.label, expectFoot(exp), "expect", "") +
        (exp.pbo == null ? "" : kpi("PBO 过拟合概率", pct(exp.pbo, 1), "0 理想 · 超 50% 严重", "pbo", "down")) +
        '</div>';
    }

    html += '<div class="tabs-inline" id="rsTabs">' +
      ["ladder", "ablation", "subsample"].map(function (k) {
        return '<button data-rs="' + k + '" class="' + (rsTab === k ? "is-active" : "") + '">' +
          tableSpec(k).title + '</button>';
      }).join("") + '</div>';

    html += '<div class="card"><div class="card__head"><div>' +
      '<div class="card__title">' + esc(spec.title) + '</div>' +
      '<div class="card__sub">' + esc(spec.desc) + '</div></div></div>';

    if (!d || !d.rows || !d.rows.length) {
      html += '<div class="empty">暂无数据</div>';
    } else {
      html += '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>配置</th>';
      spec.cols.forEach(function (c) { html += '<th>' + esc(shortCol(c)) + '</th>'; });
      html += '</tr></thead><tbody>';
      d.rows.forEach(function (row, i) {
        var name = d.index[i] || "";
        var hl = spec.hl && name.indexOf(spec.hl) >= 0;
        html += '<tr class="' + (hl ? "is-hl" : "") + '"><td>' + esc(name.replace(/^\d+\.\s*/, "")) + '</td>';
        spec.cols.forEach(function (c) {
          var ci = d.columns.indexOf(c);
          var v = row[ci];
          html += '<td class="' + (isPctCol(c) ? cls(v) : "") + '">' + fmtCell(c, v) + '</td>';
        });
        html += '</tr>';
      });
      html += '</tbody></table></div>';
    }
    html += '<div class="notice mt12"><svg viewBox="0 0 24 24" fill="none">' +
      '<path d="M12 8v5M12 16.6v.1" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/>' +
      '<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.6"/></svg>' +
      '<div>' + esc(spec.note) + '</div></div></div>';

    html += '<div class="section-label">年度收益明细</div><div class="card"><div class="rows">' +
      yearRows() + '</div></div>';

    return html;
  }

  function yearRows() {
    var annual = OV.annual || {};
    var yrs = Object.keys(annual[SELF] || {}).sort().reverse();
    var out = "";
    yrs.forEach(function (y) {
      var a = annual[SELF][y], b = annual[BENCH] ? annual[BENCH][y] : null;
      out += '<div class="row"><div class="row__main"><div class="row__name">' + y + ' 年</div>' +
        '<div class="row__meta">沪深300 ' + (isNum(b) ? b.toFixed(1) + "%" : "—") + '</div></div>' +
        '<div class="row__value"><b class="' + cls(a) + '">' + (isNum(a) ? (a > 0 ? "+" : "") + a.toFixed(1) + "%" : "—") + '</b>' +
        '<small>TGAR</small></div></div>';
    });
    return out;
  }

  function shortCol(c) {
    return c.replace("年化收益(CAGR)", "年化").replace("_CAGR", "_年化")
      .replace("最终模型", "TGAR").replace("纯动量", "纯动量")
      .replace("最大回撤", "回撤").replace("_MaxDD", "_回撤").replace("基准", "基准");
  }
  function isPctCol(c) { return /收益|回撤|MaxDD|波动|胜率|CAGR/.test(c); }
  function fmtCell(c, v) {
    if (!isNum(v)) return "—";
    if (/夏普|卡玛|索提诺|信息比率|换手|Beta|Alpha/.test(c)) return v.toFixed(2);
    if (/交易日/.test(c)) return String(Math.round(v));
    if (isPctCol(c)) return (v * 100).toFixed(2) + "%";
    return v.toFixed(2);
  }

  /* ------------------------------------------------------------ 页面：更多 */
  function pageMore() {
    var html = '';

    /* ---- 数据与计算 ---- */
    var bars = HIST.dates.length;
    var store = loadStore();
    var added = store && store.bars ? Object.keys(store.bars).sort() : [];
    var lastD = HIST.dates[HIST.dates.length - 1] || "—";
    var engineOk = typeof EtfEngine !== "undefined";
    html += '<div class="card">' +
      '<div class="card__head"><div>' +
        '<div class="card__title">数据与计算</div>' +
        '<div class="card__sub">行情在本机抓取、信号在本机计算，不经过任何服务器</div>' +
      '</div>' +
      '<span class="tag ' + (engineOk ? "t-行业" : "t-债券") + '">' +
        (engineOk ? "引擎就绪" : "引擎缺失") + '</span></div>' +
      kv("行情截至", lastD) +
      kv("K 线根数", bars + " 根" + (added.length ? "（含同步 " + added.length + " 天）" : "")) +
      kv("标的数量", (HIST.codes || []).length + " 只") +
      kv("信号日期", SIG.date || "—") +
      kv("上次更新", SYNC.lastAt ? fmtTime(SYNC.lastAt) : (store && store.at ? fmtTime(store.at) : "—")) +
      kv("更新状态", SYNC.lastMsg || (SYNC.lastAt ? "正常" : "尚未更新")) +
      kv("行情来源", "腾讯财经 · 前复权") +
      '<button class="btn btn--primary" id="btnSync" style="width:100%;margin-top:12px">更新行情并重算</button>' +
      '<p class="dim" style="font-size:11.5px;margin-top:9px;line-height:1.7">' +
        '每次约 5 个请求（260 只快照）。长时间没打开会自动补齐缺失交易日。' +
        '断网也能用——显示的是上一次算好的结果。</p>' +
    '</div>';

    html += '<div class="card" style="padding:4px 16px;margin-top:12px">';

    var items = [
      ["模型在做什么", "<p>五步筛选，每步只解决一个问题：</p>" +
        "<p><b>1 判断大盘</b> · 沪深300 站上 200 日线得 0.4 分、站上 60 日线得 0.3 分、近 20 日上涨得 0.3 分。分数 ≥0.7 权益满仓，否则半仓（不空仓）。</p>" +
        "<p><b>2 剔除不合格</b> · 上市满 250 日、20 日均成交额 ≥2000 万、收盘价在 60 日线上方、量比 &lt;3。</p>" +
        "<p><b>3 打分</b> · 只用「近 20 日涨幅（剔除最近 5 日）」。实验证明加中期动量与低波动因子反而更差。</p>" +
        "<p><b>4 建仓</b> · 取前 3 名等权，单只上限 45%。</p>" +
        "<p><b>5 控换手</b> · 现有持仓仍在前 6 名就不卖；每 5 个交易日调仓，次日开盘执行。</p>"],
      ["已被证伪的设计", "<p>这三个都是先做出来、再用数据否掉的，保留在代码里可复现：</p>" +
        "<p><b>多因子合成</b> · 四因子打分年化 14.1%，不如纯 20 日动量的 18.3%。A 股 ETF 轮动周期只有 3-8 周，慢因子会系统性错过刚启动的品种。</p>" +
        "<p><b>回撤熔断</b> · 净值从高点回撤 10% 就减半仓，结果收益从 18.3% 降到 15.4%，回撤还没改善——它在底部减仓，等于追跌。</p>" +
        "<p><b>波动率倒数加权</b> · 略优于等权但极不稳定，切一段数据就翻向，判为噪声。</p>"],
      ["换手成本（重要）", "<p>每 5 个交易日调仓一次，<b>年均单边换手 24.1 倍</b>，每年付出约 <b>4.8%</b> 的摩擦成本（24.1 × 2 × 0.10%）。</p>" +
        "<p>这是收益的最大漏损来源。把调仓周期放宽到 10 日，换手降到 16 倍、成本降到 3.2%，但年化也从 18.3% 掉到 14.4%——<b>提高频率带来的 alpha 与增加的成本几乎互相抵消</b>。</p>" +
        "<p>若按实盘更保守的 0.15% 单边成本，年化成本升至 7.2%，收益还要再降约 2.5pct。</p>"],
      ["风险与局限", "<p><b>样本内优化</b> · 参数基于 2019-2026 同一段数据选定，真实样本外表现应显著低于 18.3%。</p>" +
        "<p><b>幸存者偏差</b> · 标的池是当前在市 ETF，期间清盘未纳入。</p>" +
        "<p><b>跨境敞口错配（已知缺陷）</b> · 闸门用沪深300 判断状态，但池内有 62 只跨境 ETF。A 股走弱而美股走强时，跨境仓位会被误砍。实盘应改为按资产类别分别判定。</p>" +
        "<p><b>容量</b> · Top3 集中 + 中小主题 ETF 流动性有限，估计容量在数亿元量级。</p>"],
    ];
    items.forEach(function (it, i) {
      html += '<div class="acc" data-acc="' + i + '"><div class="acc__head">' + esc(it[0]) +
        '<svg viewBox="0 0 24 24" fill="none"><path d="M6 9.5l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>' +
        '</div><div class="acc__body">' + it[1] + '</div></div>';
    });
    html += '</div>';

    /* 数据信息 */
    html += '<div class="section-label">数据</div><div class="card">' +
      kv("回测区间", (META.start || "") + " ~ " + (META.end || "")) +
      kv("交易日", META.days + " 个 · 约 " + META.years + " 年") +
      kv("标的池", (UN.total || 0) + " 只 · 缓存 " + (UN.cache || 0) + " 只") +
      kv("行情截止", UN.latest || META.end) +
      kv("调仓日", (SIG.reb_date || SIG.date) + "（执行 " + SIG.exec_date + "）") +
      kv("下次换仓", (SIG.next_reb || "—") + (SIG.next_est ? "（估算）" : "")) +
      kv("数据生成于", D.meta ? D.meta.generated_at : "—") +
      '</div>';

    /* 设置 */
    html += '<div class="section-label">设置</div><div class="card" style="padding:4px 16px">' +
      '<div class="row" id="themeRow"><div class="row__main"><div class="row__name" style="font-size:14px">深色模式</div>' +
      '<div class="row__meta">跟随你的选择，不跟随系统</div></div>' +
      '<div class="row__value"><b>' + (theme() === "dark" ? "已开启" : "已关闭") + '</b></div></div>' +
      '<div class="row" id="remindRow"><div class="row__main"><div class="row__name" style="font-size:14px">调仓日提醒</div>' +
      '<div class="row__meta">调仓日与买入日各提醒一次</div></div>' +
      '<div class="row__value"><b>' + (remindOn() ? "已开启" : "已关闭") + '</b></div></div>' +
      '<div class="row" id="aboutRow"><div class="row__main"><div class="row__name" style="font-size:14px">关于</div>' +
      '<div class="row__meta">ETF Quant · 版本 ' + ((D.meta && D.meta.version) || "—") + '</div></div>' +
      '<div class="row__value"><b class="dim" style="font-size:13px">›</b></div></div>' +
      '</div>';

    html += '<div class="notice mt12"><svg viewBox="0 0 24 24" fill="none">' +
      '<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.7"/>' +
      '<path d="M12 8.4v5.2M12 16.4v.1" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></svg>' +
      '<div>本应用为个人研究工具，全部计算基于历史数据，<b>不构成投资建议</b>。' +
      '离线运行，不联网、不上传任何数据。</div></div>';

    html += '<div style="height:12px"></div>';
    return html;
  }

  function kv(k, v) {
    return '<div class="kv"><span>' + esc(k) + '</span><b>' + esc(v) + '</b></div>';
  }

  /* ------------------------------------------------------------ 路由 */
  var PAGES = {
    overview: { title: "概览", render: pageOverview, after: afterOverview },
    today: { title: "调仓清单", render: pageToday, after: afterToday },
    holdings: { title: "持仓", render: pageHoldings, after: afterHoldings },
    research: { title: "效果验证", render: pageResearch, after: afterResearch },
    more: { title: "更多", render: pageMore, after: afterMore },
  };
  var current = "overview";

  function render() {
    var p = PAGES[current];
    var view = $("#view");
    view.innerHTML = p.render();
    $("#pageTitle").textContent = p.title;
    if (p.after) p.after();
    window.scrollTo({ top: 0, behavior: "instant" in window ? "instant" : "auto" });
  }

  function go(tab, push) {
    if (!PAGES[tab]) return;
    if (tab === current) return;
    if (push !== false) {
      try { history.pushState({ tab: tab }, "", "#" + tab); } catch (e) { /* file:// 下可能受限 */ }
    }
    current = tab;
    $$(".tab").forEach(function (b) { b.classList.toggle("is-active", b.dataset.tab === tab); });
    var view = $("#view");
    view.style.animation = "none";
    void view.offsetWidth;
    view.style.animation = "";
    render();
  }

  /* 返回键：由 Android 侧调用；没有历史时返回 false 让 Activity 退出 */
  window.__maBack = function () {
    if (current !== "overview") { go("overview", false); return true; }
    return false;
  };
  window.addEventListener("popstate", function (e) {
    var tab = (e.state && e.state.tab) || "overview";
    current = "";
    go(tab, false);
  });

  $$(".tab").forEach(function (b) {
    b.addEventListener("click", function () { go(b.dataset.tab); });
  });

  /* 左右滑动切换（阈值 60px，竖向滑动不拦截） */
  (function swipe() {
    var x0 = 0, y0 = 0, t0 = 0, active = false;
    var order = ["overview", "today", "holdings", "research", "more"];
    document.addEventListener("touchstart", function (e) {
      if (e.touches.length !== 1) return;
      var el = e.target.closest("input, .tbl-wrap, .quick, .tabs-inline");
      if (el) { active = false; return; }
      x0 = e.touches[0].clientX; y0 = e.touches[0].clientY;
      t0 = Date.now(); active = true;
    }, { passive: true });
    document.addEventListener("touchend", function (e) {
      if (!active) return;
      active = false;
      var dx = e.changedTouches[0].clientX - x0;
      var dy = e.changedTouches[0].clientY - y0;
      if (Date.now() - t0 > 600) return;
      if (Math.abs(dx) < 60 || Math.abs(dx) < Math.abs(dy) * 1.8) return;
      var i = order.indexOf(current);
      var next = order[i + (dx < 0 ? 1 : -1)];
      if (next) go(next);
    }, { passive: true });
  })();

  /* ------------------------------------------------------------ 页面渲染后的绑定 */
  function afterOverview() {
    drawSpark($("#spark"), [
      { points: sliceFrom(seriesFor(SELF)), color: "#7aa2ff" },
      { points: sliceFrom(seriesFor(BENCH)), color: "rgba(255,255,255,0.42)" },
    ], {});

    var seg = $("#period");
    if (seg) {
      seg.addEventListener("click", function (e) {
        var b = e.target.closest("[data-p]");
        if (!b) return;
        period = b.dataset.p;
        render();
      });
    }

    $$("[data-strat]").forEach(function (row) {
      row.addEventListener("click", function () {
        var nm = row.dataset.strat;
        var p = (OV.perf || []).filter(function (x) { return x.name === nm; })[0];
        if (!p) return;
        openSheet(nm,
          '<p>累计收益 <b>' + pct(p.cum, 1) + '</b>　年化 <b>' + pct(p.cagr) + '</b></p>' +
          '<p>年化波动 ' + pct(p.vol) + '　夏普 ' + num(p.sharpe) + '　卡玛 ' + num(p.calmar) + '</p>' +
          '<p>最大回撤 ' + pct(p.mdd) + '　超额 ' + pct(p.excess, 1) + '</p>' +
          '<p>Beta ' + num(p.beta) + '　信息比率 ' + num(p.ir) + '　年均换手 ' + num(p.turnover, 1) + ' 倍</p>');
      });
    });
  }

  function afterToday() {
    renderOrders();

    var input = $("#cash");
    if (input) {
      input.addEventListener("input", function () {
        var v = input.value.replace(/[^\d]/g, "");
        cash = parseInt(v || "0", 10);
        renderOrders();
      });
    }
    var quick = $("#quick");
    if (quick) {
      quick.addEventListener("click", function (e) {
        var b = e.target.closest("[data-cash]");
        if (!b) return;
        cash = parseInt(b.dataset.cash, 10);
        $$("#quick .chip").forEach(function (c) { c.classList.toggle("is-on", c === b); });
        if (input) input.value = cash;
        renderOrders();
      });
    }
    var btn = $("#copy");
    if (btn) btn.addEventListener("click", copyList);

    var syncTop = $("#btnSyncTop");
    if (syncTop) {
      syncTop.addEventListener("click", function () {
        if (SYNC.busy) return;
        syncTop.classList.add("is-busy");
        syncMarket(true);   // 完成后页面会重渲染，按钮自然恢复
      });
    }
  }

  function afterResearch() {
    var tabs = $("#rsTabs");
    if (tabs) {
      tabs.addEventListener("click", function (e) {
        var b = e.target.closest("[data-rs]");
        if (!b) return;
        rsTab = b.dataset.rs;
        render();
      });
    }
  }

  function fmtTime(ms) {
    var d = new Date(ms);
    if (isNaN(d.getTime())) return "—";
    var p2 = function (n) { return String(n).padStart(2, "0"); };
    return (d.getMonth() + 1) + "-" + p2(d.getDate()) + " " + p2(d.getHours()) + ":" + p2(d.getMinutes());
  }

  function afterMore() {
    var bs = $("#btnSync");
    if (bs) {
      bs.addEventListener("click", function () {
        if (SYNC.busy) return;
        bs.disabled = true;
        bs.textContent = "更新中…";
        syncMarket(true);   // 完成后重渲染，按钮恢复
      });
    }

    $$(".acc").forEach(function (a) {
      a.querySelector(".acc__head").addEventListener("click", function () {
        a.classList.toggle("is-open");
      });
    });
    var tr = $("#themeRow");
    if (tr) tr.addEventListener("click", function () { applyTheme(theme() === "dark" ? "light" : "dark"); });
    var rr = $("#remindRow");
    if (rr) rr.addEventListener("click", function () {
      var on = !remindOn();
      setRemind(on);
      var b = rr.querySelector(".row__value b");
      if (b) b.textContent = on ? "已开启" : "已关闭";
      // 开启时如果当前正好命中调仓日，立刻演示一次，让用户知道提醒长什么样
      if (on) maybeRemind(); else toast("已关闭调仓日提醒");
    });
    var ab = $("#aboutRow");
    if (ab) ab.addEventListener("click", function () {
      openSheet("关于 ETF Quant",
        '<p>版本 <b>' + (D.meta ? D.meta.version : "1.2.0") + '</b>　数据生成于 ' + (D.meta ? D.meta.generated_at : "") + '</p>' +
        '<p>全部计算在本地完成：回测引擎按 T 日收盘出信号、T+1 开盘成交，含 0.10%/边交易成本；' +
        '绩效口径为日历天数年化、252 交易日波动率年化。</p>' +
        '<p>除拉取行情与发送调仓提醒外不联网，不收集任何数据。</p>');
    });
  }

  /* ------------------------------------------------------------ 启动 */
  function boot() {
    if (!D.overview) {
      $("#view").innerHTML = '<div class="empty">数据缺失<br>请重新生成 data.js</div>';
      return;
    }
    var m = document.querySelector('meta[name="theme-color"]');
    if (m) m.setAttribute("content", theme() === "light" ? "#f5f6f8" : "#0a0d13");
    if (window.AndroidBridge && AndroidBridge.setTheme) {
      try { AndroidBridge.setTheme(theme()); } catch (e) { /* 非 Android */ }
    }

    /* 先把上次同步的 bars 合并进来，再用本地引擎重算一次，
       这样即使离线、即使内置数据已过期，显示的也是最新算过的结果。 */
    var cached = loadStore();
    if (cached) {
      mergeBars(cached);
      SYNC.lastAt = cached.at || 0;
      SYNC.lastMsg = cached.msg || "";
      if (cached.sig) setSignal(cached.sig);
    }
    if (typeof EtfEngine !== "undefined") {
      try {
        var r = recompute(cached);
        setSignal(r.api);
      } catch (e) { /* 引擎异常时退回内置信号，至少页面能开 */ }
    }

    setTimeout(maybeRemind, 1500);   // 等首屏渲染与自动同步落定，避免提醒被重绘覆盖

    var hash = (location.hash || "").replace("#", "");
    current = PAGES[hash] ? hash : "overview";
    $$(".tab").forEach(function (b) { b.classList.toggle("is-active", b.dataset.tab === current); });
    render();

    window.addEventListener("scroll", function () {
      $("#appbar").classList.toggle("is-scrolled", window.scrollY > 4);
    }, { passive: true });

    /* 自动更新：先恢复上次缓存的结果（保证断网也有东西看），
       再后台拉行情重算。延迟 700ms 避免和首屏渲染抢主线程。
       失败静默——地铁里打开 App 不该弹一堆报错。 */
    // ?nosync=1 跳过自动同步：预览/截图/纯离线场景用，避免每次打开都打网络请求
    if (!/[?&]nosync=1/.test(location.search)) {
      setTimeout(function () {
        syncMarket(false).then(function (ok) {
          if (!ok) SYNC.lastMsg = "离线 · 显示上次结果";
        });
      }, 700);
    }

    window.addEventListener("resize", function () {
      // 旋转屏幕后 canvas 需要按新宽度重画
      if (current === "overview") {
        drawSpark($("#spark"), [
          { points: sliceFrom(seriesFor(SELF)), color: "#7aa2ff" },
          { points: sliceFrom(seriesFor(BENCH)), color: "rgba(255,255,255,0.42)" },
        ], {});
      }
    });
  }

  boot();
})();
