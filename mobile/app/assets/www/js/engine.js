/*
 * ETF 量化策略引擎（手机端本地计算）
 * ==================================
 *
 * 这是 etfquant/strategy.py 里 TrendGatedAdaptive 的**逐行移植**，
 * 目的是让 APK 不依赖电脑：内置 300 天日线，每次打开自行拉最新行情、
 * 本地重算信号。
 *
 * 为什么值得重写一遍而不是查接口要结果
 * ----------------------------------
 * 查接口就必须有服务器，那就又回到"依赖电脑"。移植的代价是 200 行，
 * 换来的是装上就能用、断网也能看最后一次结果。
 *
 * 一致性的保证方式
 * --------------
 * 每个函数的注释都标注了它在 Python 里的对应写法（含 min_periods、ddof 等
 * 容易被忽略的细节）。移植完成后用 scripts/verify_mobile_engine.py
 * 与 Python 逐项对拍，不允许"看起来差不多"。
 *
 * 与桌面端的已知差异（有意为之）
 * ----------------------------
 * 1. 回测序列不支持。8 年 × 4 策略的逐日记账放不进手机，也没必要——
 *    回测结果是静态历史，用打包时的快照即可。
 * 2. 缓冲带依赖"上次持仓"。桌面端从 2019 年开始累积，手机上首次运行
 *    没有历史，用内置的期末持仓作为初始值，此后每次调仓自行记录。
 */
var EtfEngine = (function () {
  "use strict";

  /* ---------------------------------------------------------------- 参数 */
  /* 与 strategy.py 的默认值逐一对应，改动这里等于改策略，必须同步对拍。 */
  var P = {
    topN: 3,           // top_n
    minHist: 250,      // min_hist：上市满 250 个交易日
    minAmount: 2e7,    // min_amount：20 日均成交额 2000 万元
    maxVr: 3.0,        // max_vr：量比上限，剔除成交异常放大
    regimeMid: 0.7,    // regime_mid
    regimeLow: null,   // regime_low：null = 不启用第三档（默认只有两档）
    expoHigh: 1.0,     // expo_high
    expoMid: 0.5,      // expo_mid
    expoLow: null,     // expo_low：null = 与 expo_mid 相同（"只降半仓、不空仓"）
    maxWeight: 0.45,   // max_weight
    bufferMult: 2,     // buffer_mult：缓冲带 = top(2N) = top6
    benchmark: "sh510300",
    skip: 5            // momentum 的 skip：剔除最近 5 个交易日
  };

  /* ---------------------------------------------------------------- 数值工具 */

  /** 区间均值，跳过 null。对应 pandas rolling(window, min_periods=mp).mean() */
  function rollMean(arr, end, win, minP) {
    var sum = 0, cnt = 0, start = end - win + 1;
    if (start < 0) start = 0;
    for (var i = start; i <= end; i++) {
      var v = arr[i];
      if (v !== null && v !== undefined && isFinite(v)) { sum += v; cnt++; }
    }
    if (cnt < (minP || win)) return null;
    return sum / cnt;
  }

  /** 样本标准差（ddof=1）。对应 numpy/pandas 默认行为。 */
  function std(arr) {
    var n = arr.length;
    if (n < 2) return null;
    var m = 0, i;
    for (i = 0; i < n; i++) m += arr[i];
    m /= n;
    var s = 0;
    for (i = 0; i < n; i++) { var d = arr[i] - m; s += d * d; }
    return Math.sqrt(s / (n - 1));
  }

  /** 线性插值分位数。对应 pandas Series.quantile(q) 默认 interpolation='linear' */
  function quantile(sorted, q) {
    var n = sorted.length;
    if (!n) return null;
    if (n === 1) return sorted[0];
    var pos = (n - 1) * q;
    var lo = Math.floor(pos), hi = Math.ceil(pos);
    if (lo === hi) return sorted[lo];
    return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
  }

  /* ---------------------------------------------------------------- 预处理 */

  /**
   * 把导出数据整理成便于逐日计算的列式结构。
   *
   * 前向填充规则必须与 pipeline.py 完全一致：
   *   close  → ffill（停牌沿用前值，未上市仍为 null）
   *   volume → fillna(0)（注意不是 ffill，停牌日记 0 会拉低均量，这是桌面端行为）
   * 两者不一致会导致量比与成交额筛选出现系统性偏差。
   */
  function prepare(hist) {
    var n = hist.dates.length;
    var cols = hist.codes.map(function (code, i) {
      var rc = hist.close[i] || [];
      var rv = hist.vol[i] || [];
      var close = new Array(n);
      var vol = new Array(n);
      var amount = new Array(n);
      var last = null;
      for (var t = 0; t < n; t++) {
        if (rc[t] !== null && rc[t] !== undefined) last = rc[t];
        close[t] = last;                              // 未上市保持 null
        var v = rv[t];
        vol[t] = (v === null || v === undefined) ? 0 : v;   // fillna(0)
        amount[t] = (last === null) ? 0 : last * vol[t] * 100;  // Panel.amount
      }
      return { code: code, close: close, vol: vol, amount: amount };
    });
    // meta 一并挂在返回值上，调用方不必再单独传一次
    return { n: n, dates: hist.dates, cols: cols, meta: hist.meta || {} };
  }

  /* ---------------------------------------------------------------- 单只指标 */

  /**
   * 计算某只在第 t 日的全部入选所需指标。
   * 返回 null 表示该日无行情（未上市）。
   */
  function metricsAt(col, t) {
    var c = col.close;
    var px = c[t];
    if (px === null || px === undefined) return null;

    // count = close.notna().cumsum()，ffill 后从首个有效值开始计数
    var count = 0;
    for (var i = 0; i <= t; i++) if (c[i] !== null && c[i] !== undefined) count++;

    // momentum(close, 20, skip=5) = close[t-5] / close[t-25] - 1
    var r20 = null;
    if (t - P.skip - 20 >= 0) {
      var a = c[t - P.skip], b = c[t - P.skip - 20];
      if (a !== null && b !== null && b !== 0) r20 = a / b - 1;
    }

    // momentum(close, 20) = close[t] / close[t-20] - 1（用于防守标的挑选）
    var r20raw = null;
    if (t - 20 >= 0 && c[t - 20] !== null && c[t - 20] !== 0) r20raw = px / c[t - 20] - 1;

    // ma(60): min_periods = max(5, 60//3) = 20
    var ma60 = rollMean(c, t, 60, 20);
    // ma(200): min_periods = max(5, 200//3) = 66
    var ma200 = rollMean(c, t, 200, 66);

    // amt20: rolling(20, min_periods=10)
    var amt20 = rollMean(col.amount, t, 20, 10);

    // volume_ratio: mean(vol,5;mp=2) / mean(vol,60;mp=10)
    var v5 = rollMean(col.vol, t, 5, 2);
    var v60 = rollMean(col.vol, t, 60, 10);
    var vr = (v5 !== null && v60 !== null && v60 > 0) ? v5 / v60 : null;

    return {
      close: px, count: count, r20: r20, r20raw: r20raw,
      ma60: ma60, ma200: ma200, amt20: amt20, vr: vr,
      above60: (ma60 !== null) ? px > ma60 : false
    };
  }

  /* ---------------------------------------------------------------- 市场状态 */

  /**
   * regime ∈ {0, 0.3, 0.4, 0.6, 0.7, 1.0}，三项条件加权。
   * 对应 TrendGatedAdaptive.regime_at()。
   *
   * 注意 r20 的分母是 hist.iloc[-21]，即 close[t-20]（含当日共 21 个点），
   * 不是 close[t-19]——差一位会让信号在临界点翻转。
   */
  function regimeAt(bench, t) {
    var c = bench.close;
    var hist = [];
    for (var i = 0; i <= t; i++) if (c[i] !== null && c[i] !== undefined) hist.push(c[i]);
    if (hist.length < 210) return 1.0;   // 与桌面端一致：历史不足视为满仓

    var px = hist[hist.length - 1];
    var ma200 = 0, ma60 = 0, i2;
    for (i2 = hist.length - 200; i2 < hist.length; i2++) ma200 += hist[i2];
    ma200 /= 200;
    for (i2 = hist.length - 60; i2 < hist.length; i2++) ma60 += hist[i2];
    ma60 /= 60;
    var r20 = hist.length > 21 ? px / hist[hist.length - 21] - 1 : 0;

    var score = 0;
    if (px > ma200) score += 0.4;
    if (px > ma60) score += 0.3;
    if (r20 > 0) score += 0.3;
    return score;
  }

  /* 与 strategy.py 的 _exposure 同口径：默认两档；regimeLow 非 null 才启用第三档 */
  function exposureOf(regime) {
    if (regime >= P.regimeMid) return P.expoHigh;
    if (P.regimeLow === null || regime >= P.regimeLow) return P.expoMid;
    return P.expoLow === null ? P.expoMid : P.expoLow;
  }

  /* ---------------------------------------------------------------- 横截面打分 */

  /**
   * winsorize 2%/98% + z-score。
   * 对应 F.winsorize_cs + F.zscore_cs。
   *
   * 严格来说 mom20 模式下排序等价于直接按 r20 排（z 是单调变换），
   * 但缩尾会制造并列，所以仍然实现完整流程，保证与回测完全一致。
   */
  function zscoreWinsor(values) {
    var clean = values.slice().sort(function (a, b) { return a - b; });
    if (clean.length < 2) return null;
    var lo = quantile(clean, 0.02), hi = quantile(clean, 0.98);
    var w = values.map(function (v) { return Math.min(hi, Math.max(lo, v)); });
    var m = 0, i;
    for (i = 0; i < w.length; i++) m += w[i];
    m /= w.length;
    var sd = std(w);
    if (!(sd > 0)) return null;
    return w.map(function (v) { return (v - m) / sd; });
  }

  /* ---------------------------------------------------------------- 主流程 */

  /**
   * 在时间轴第 t 日计算目标持仓。
   *
   * @param {object} prep   prepare() 的结果
   * @param {number} t      索引
   * @param {object} meta   { code: {name, category} }
   * @param {Array}  held   上一期持仓代码（用于缓冲带），可空
   * @returns {object|null} { date, regime, exposure, picks, weights, diag }
   */
  function signalAt(prep, t, meta, held) {
    meta = meta && Object.keys(meta).length ? meta : (prep.meta || {});
    var cat = function (code) { return (meta[code] && meta[code].category) || "\u4e3b\u9898"; };
    var nameOf = function (code) { return (meta[code] && meta[code].name) || code; };

    /* ---- 一次性算出全池指标，避免同一只被重复计算 ---- */
    var all = [];
    for (var i = 0; i < prep.cols.length; i++) {
      var m = metricsAt(prep.cols[i], t);
      if (m) all.push({ code: prep.cols[i].code, m: m });
    }

    var benchCol = null;
    for (var b = 0; b < prep.cols.length; b++) {
      if (prep.cols[b].code === P.benchmark) { benchCol = prep.cols[b]; break; }
    }
    var regime = benchCol ? regimeAt(benchCol, t) : 1.0;
    var expo = exposureOf(regime);

    /* ---- 第 2 层：初筛 ---- */
    var diag = { total: all.length, bondExcluded: 0, shortHist: 0, illiquid: 0, belowMa: 0, crowded: 0 };
    var pool = [];
    all.forEach(function (x) {
      if (cat(x.code) === "\u503a\u5238") { diag.bondExcluded++; return; }
      if (x.m.count < P.minHist) { diag.shortHist++; return; }
      if (!(x.m.amt20 >= P.minAmount)) { diag.illiquid++; return; }
      pool.push(x);
    });

    var equity = {};
    var picks = [];
    var rows = [];
    var used = 0;

    if (expo > 0 && pool.length) {
      /* ---- 趋势 + 拥挤度 ---- */
      var cand = pool.filter(function (x) {
        var vrOk = (x.m.vr === null) ? true : x.m.vr < P.maxVr;   // 等价 fillna(1.0)
        return vrOk && x.m.above60;
      });
      pool.forEach(function (x) {
        if (!x.m.above60) diag.belowMa++;
        else if (x.m.vr !== null && x.m.vr >= P.maxVr) diag.crowded++;
      });

      /* ---- 横截面打分 ----
       * 关键：z 值必须在**全池**上算，而不是候选集。
       * Python 的 FactorBank 是预先对整个 close 矩阵算 winsorize + zscore 的，
       * 只在候选集上算会得到完全不同的分位点与标准差，进而改变排序。
       * 这也是最容易在移植时想当然写错的一处。
       */
      var allR = [];
      all.forEach(function (x) { if (x.m.r20 !== null) allR.push(x.m.r20); });
      var zAll = zscoreWinsor(allR);
      var zMap = {};
      if (zAll) {
        var k = 0;
        for (var a2 = 0; a2 < all.length; a2++) {
          if (all[a2].m.r20 !== null) { zMap[all[a2].code] = zAll[k++]; }
        }
      }

      /* 候选不足 top_n 时放宽到全部合格标的（对应 Python 的 cols = elig） */
      var src = cand.length >= P.topN ? cand : pool;

      var scored = [];
      src.forEach(function (x) {
        var z = zMap[x.code];
        if (z === undefined || !isFinite(z)) return;
        scored.push({ code: x.code, m: x.m, s: z });
      });
      // 双键排序：z 降序，同分再按原始 R20 降序。
      // 缩尾会把顶端几只压成同一个 z，单键排序的顺序取决于引擎实现，
      // 同一份数据可能选出不同标的——桌面端已改成双键，这里必须一致。
      scored.sort(function (a3, b3) {
        if (b3.s !== a3.s) return b3.s - a3.s;
        var ra = a3.m.r20 === null ? -Infinity : a3.m.r20;
        var rb = b3.m.r20 === null ? -Infinity : b3.m.r20;
        return rb - ra;
      });

      /* ---- 缓冲带 ---- */
      var headSet = {};
      scored.slice(0, P.topN * P.bufferMult).forEach(function (x) { headSet[x.code] = 1; });
      (held || []).forEach(function (c) {
        if (headSet[c] && picks.length < P.topN && picks.indexOf(c) < 0) picks.push(c);
      });
      for (var r = 0; r < scored.length && picks.length < P.topN; r++) {
        if (picks.indexOf(scored[r].code) < 0) picks.push(scored[r].code);
      }

      /* ---- 等权 + 单只上限 ---- */
      if (picks.length) {
        var each = expo / picks.length;
        var cap = Math.min(P.maxWeight, expo);
        if (each > cap) each = cap;
        picks.forEach(function (c) { equity[c] = each; used += each; });
      }

      rows = scored.slice(0, 12).map(function (x) {
        return {
          code: x.code, name: nameOf(x.code), category: cat(x.code),
          score: x.s, r20: x.m.r20, r20raw: x.m.r20raw,
          amt20: x.m.amt20, vr: x.m.vr,
          above60: x.m.above60, close: x.m.close
        };
      });
    }

    /* ---- 未用敞口配债券 ----
     * 债券不参与打分（波动量级差一个数量级，R20 不可比），
     * 只作为「未用敞口」的去处，按 20 日涨幅选最强的一只。
     */
    var rest = Math.max(0, 1 - used);
    var defensive = null;
    if (rest > 1e-6) {
      var best = null;
      all.forEach(function (x) {
        if (cat(x.code) !== "\u503a\u5238") return;
        if (x.m.r20raw === null) return;
        if (!best || x.m.r20raw > best.m.r20raw) best = x;
      });
      if (best) {
        equity[best.code] = (equity[best.code] || 0) + rest;
        defensive = best.code;
      }
    }

    /* ---- 整理输出 ---- */
    var metricOf = {};
    all.forEach(function (x) { metricOf[x.code] = x.m; });
    var out = [];
    for (var code in equity) {
      var w = equity[code];
      if (w <= 1e-6) continue;
      var mm = metricOf[code];
      out.push({
        code: code,
        name: nameOf(code),
        category: cat(code),
        weight: w,
        price: mm ? mm.close : null,
        r20: mm ? mm.r20 : null,
        r20raw: mm ? mm.r20raw : null,
        isDefensive: code === defensive
      });
    }
    out.sort(function (a4, b4) { return b4.weight - a4.weight; });

    return {
      date: prep.dates[t],
      regime: regime,
      exposure: expo,
      rows: out,
      candidates: rows,
      defensive: defensive,
      diag: diag,
      equityWeight: used,
      bondWeight: 1 - used
    };
  }

  return {
    params: P,
    prepare: prepare,
    metricsAt: metricsAt,
    regimeAt: regimeAt,
    signalAt: signalAt,
    latest: function (prep, meta, held) { return signalAt(prep, prep.n - 1, meta, held); }
  };
})();

if (typeof module !== "undefined" && module.exports) module.exports = EtfEngine;
