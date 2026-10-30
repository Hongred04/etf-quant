# ETF Quant · Android 版

把桌面版 ETF 量化工具做成一个**离线手机 App**：数据在构建时导出，运行时零网络、零权限。

产物：`dist/etf-quant.apk`（约 81 KB，minSdk 24 / targetSdk 34，v1.1.0）

---

## 一、为什么是「WebView + 静态资源」而不是原生或 Python 打包

桌面版是「Python 后端 + 静态前端」。要在手机上跑，三条路：

| 方案 | 可行性 | 结论 |
|---|---|---|
| Kivy / Buildozer（Python 打包） | 需 Linux 或 WSL + Docker，工具链过重，界面仍是自绘 | 放弃 |
| Chaquopy（Gradle 里嵌 CPython） | 需引入 AGP + 上百 MB 依赖，pandas/numpy 的 Android wheel 体积大且易崩 | 放弃 |
| **WebView + 预导出数据** | 一次导出 JSON，全部计算在构建时完成；App 只做展示与交互 | **采用** |

关键判断：这个工具在手机上**不需要实时计算**——回测、选股、绩效都是离线产物，
手机上要的是「看结果 + 按资金算买几手」。把重计算留在 PC，手机端只做渲染，
APK 只有 84 KB，冷启动 instantaneous，且完全离线。

## 二、界面设计的参考项目与取舍

按要求只参考**近年活跃、高星**的项目，不参考老旧样板：

| 项目 | 借鉴点 | 落到本项目的做法 |
|---|---|---|
| [ghostfolio/ghostfolio](https://github.com/ghostfolio/ghostfolio)（2025 仍高频迭代，GitHub 6.5k★，官方博客自述） | mobile-first PWA、深色优先、单一强调色、卡片化信息分层 | 全局只用 1 个强调色（蓝）+ 红涨绿跌；卡片统一 20px 圆角、1px 描边、极弱阴影 |
| [shadcn-ui/ui](https://github.com/shadcn-ui/ui)（2023 起，社区最主流的组件规范之一） | 边框/圆角/阴影的克制尺度，语义化 CSS 变量 | 全部颜色走 `--bg/--surface/--txt/--line` 语义 token，深浅主题各一套，组件不写死颜色 |
| Material Design 3（`material-components-android`） | 底部导航的 active 指示、系统栏与内容融合 | 5 栏底部导航（图标 + 10.5px 标签），状态栏/导航栏颜色由 JS 桥回调原生层同步 |
| [maybe-finance/maybe](https://github.com/maybe-finance/maybe)（2024，净值类产品） | Hero 大数字 + 迷你走势 + 分段时间筛选 | 概览首屏：40px 累计收益 + 面积折线 + 「全部/近3年/近1年」分段控件 |
| [ant-design/ant-design-mobile](https://github.com/ant-design/ant-design-mobile) | 列表行「左标题右数值」、紧凑表格、横向滚动 | 持仓/订单/贡献榜统一为行式布局，数值右对齐 + `tabular-nums`；实验表格首列吸附 |

**没有照搬的部分**：这些库都需要 npm 构建链。本项目为了「离线、单文件、可 diff」，
用原生 CSS/JS 重写了同等视觉语言（约 900 行 CSS + 950 行 JS），没有任何运行时依赖。

## 三、手机端特有的设计决策

1. **底部五栏**：概览 / 今日 / 持仓 / 验证 / 更多。日常高频的是「今日」，但首屏给概览，
   符合「打开先看结果」的使用习惯。
2. **术语改为点按**：桌面版是 hover 提示，触屏没有 hover。所有指标名后的 `?` 点开是底部弹层。
3. **左右滑动切页**：阈值 60px，且竖向位移大于水平位移的 1.8 倍时不拦截，
   避免和页面滚动打架；输入框、横向滚动表格内不触发。
4. **数字优先**：所有数值 `tabular-nums`，大号数字用 `letter-spacing:-0.03em` 收紧，
   保证「270.1%」这类值在一行内不换行。
5. **年度柱状图从零线双向生长**：正值向上（红）、负值向下（绿），基准用 42% 透明度的灰柱并排。
   这里踩过一个坑——柱形容器用 `align-items:flex-end` 会导致高度百分比失效（全部塌成 2px），
   必须 `stretch` + 绝对定位在零线两侧。
6. **环形图用独立色板**：持仓里常出现两只同类资产（如两只纳指 ETF），按类别取色会相邻同色，
   改为固定 6 色序列。
7. **安全区**：`viewport-fit=cover` + `env(safe-area-inset-*)`，刘海屏与手势条都不遮挡内容。

## 四、原生壳做了什么

`android/src/com/etfquant/mobile/MainActivity.java`，约 150 行：

- **零权限**：不申请 INTERNET，数据全在 `assets/www`
- `setAllowFileAccess(true)`：targetSdk ≥ 30 时默认关闭，不显式打开会加载失败
- `setTextZoom(100)`：跟随系统字体放大会撑破 KPI 版式
- **返回键**：先 `evaluateJavascript("__maBack()")` 交给页面切回上一栏，页面说「退无可退」才 `finish()`
- **状态栏/导航栏换色**：JS 切主题时通过 `AndroidBridge.setTheme()` 回调原生层
- **剪贴板**：`AndroidBridge.copyText()` 走系统剪贴板 + Toast；
  `file://` 下 `navigator.clipboard` 不可用（非安全上下文），必须走原生桥

## 五、目录

```
mobile/
├── app/assets/www/          前端源码（单一数据源）
│   ├── index.html           壳 + 5 个 Tab 的 SVG 图标
│   ├── css/app.css          设计 token + 组件样式
│   ├── js/app.js            路由、渲染、Canvas 图表
│   └── data.js              构建产物：window.__DATA__ = {...}
├── preview.py               用 390px iframe 渲染真机尺寸截图
└── shots/                   各页截图（深浅两套）

android/
├── AndroidManifest.xml
├── src/com/etfquant/mobile/MainActivity.java
├── res/                     主题、颜色、自适应图标 + 5 档 PNG 图标
├── build.sh                 aapt2 → javac → d8 → zipalign → apksigner
└── etfquant.keystore        本地签名密钥（口令 etfquant，仅自用）

scripts/export_mobile_data.py   从 output/*.csv 生成 data.js
```

## 六、构建与预览

```bash
# 1. 更新数据（行情变化后）
python scripts/download_history.py --top 260
python scripts/run_backtest.py --tag main
python scripts/run_signal.py
python scripts/export_mobile_data.py     # → mobile/app/assets/www/data.js

# 2. 浏览器预览（真机 390×844 尺寸）
python mobile/preview.py                 # 输出到 mobile/shots/

# 3. 打 APK
bash android/build.sh                    # → dist/etf-quant.apk
```

构建链依赖：JDK 17 + Android SDK（build-tools 34 / platforms android-34）。
本机安装在 `C:/Users/macro/android-build/`，`build.sh` 里可用 `ANDROID_SDK_ROOT` / `JAVA_HOME` 覆盖。

## 七、已知限制

1. **未在真机/模拟器上运行过**。APK 已通过 `aapt2 dump badging`（包名、minSdk、启动 Activity、
   图标齐全）、`apksigner verify`、dex 类清单校验，并把 APK 内的 `assets/www` 解出来
   在 390px 视口渲染验证过；但本机没有 Android 设备也没有可用的模拟器虚拟化，
   安装与启动的最后一步需要你在手机上确认。
2. **数据仍需在 PC 上生成**。App 可以自动拉取，但"算"这一步留在电脑
   （回测/选股是重计算）。详见第八节。
3. **执行日不跳过法定节假日**，与桌面版一致，长假前后以交易所实际开市日为准。
4. 签名用的是自用密钥，未上架应用商店；若日后要发布，需要换正式签名并保证密钥不丢。

## 八、独立运行：APK 不依赖电脑

v1.0/v1.1 的架构是"手机上只做展示，数据由 PC 导出后同步过来"。那要求电脑开机、
同一 Wi-Fi、防火墙放行——本质上还是个联网客户端，只是服务器换成了自己的 PC。

v1.2 改成真正的独立应用：

```
APK 内置 300 天日线  →  启动时拉最新快照  →  本地引擎重算  →  渲染
      1.1 MB              5 个请求/1 秒        ES5 代码/16 ms
```

### 为什么要移植策略而不是让手机请求结果

请求结果就必须有服务器，那就又回到依赖电脑。移植的代价是 `js/engine.js` 约 380 行，
换来的是"装上就能用、断网也能看"。

### 三个必须对齐的细节（都在移植时踩过）

1. **横截面缩尾必须在全池上算**，不能只在候选集上算。
   Python 的 `FactorBank` 是对整个 close 矩阵预计算 `winsorize + zscore`。只在候选中算
   会得到完全不同的 2% / 98% 分位点，进而改变排序。第一版就是这么写错的，
   导致两个时点的选股不一致。
2. **`volume` 用 `fillna(0)` 而不是 ffill**。`pipeline.load_panel` 里 close 是 ffill、
   volume 是 fillna(0)。停牌日 volume 记 0 会拉低均量，这影响量比与成交额筛选。
3. **`ma()` 的 `min_periods = max(5, window//3)`**，不是默认的 window。
   MA200 只要 66 个点就出值，MA60 要 20 个点。

### 对拍方式

`scripts/verify_mobile_engine.py`：同一份历史分别喂给 Python 和 Node，
比对 5 个时点的 regime / 敞口 / 持仓集合 / 单只权重，再抽查候选的 R20。
任何一项不一致就退出码 1。

### 一个顺带查出的桌面端缺陷

对拍时发现同一份数据两次运行可能选出不同标的。根因是缩尾把横截面顶端的几只
压成同一个 z 值（并列），而 `sort_values` 的 quicksort 对同分元素的顺序是
实现相关且不稳定的。

已改为双键排序（z 降序，同分再按原始 R20 降序）。**代价是回测数字变化**：

| 指标 | 修正前 | 修正后 |
|---|---|---|
| CAGR | 18.25% | **17.28%** |
| 最大回撤 | -26.28% | **-28.86%** |
| 夏普 | 0.72 | **0.69** |

也就是说，旧的 18.25% 里有约 1pct 来自"并列时的特定顺序恰好有利"。
换成任何确定性规则都回落到 17.28%（不缩尾）或 16.83%（按流动性打破并列）。
对一个每天要出实盘信号的工具，可复现比多 1pct 更重要。

### 数据更新机制

| 场景 | 行为 |
|---|---|
| 打开 App | 拉 260 只快照（5 个请求），若日期比内置新则追加一根 bar，重算 |
| 缺口 1 个交易日 | 同上，快照即可覆盖 |
| 缺口 > 1 天 | 自动用日线接口补齐（260 次请求，6 路并发，约 8 秒，有进度提示） |
| 缺口 > 40 天 | 不补，提示更新 APK（避免长时间等待） |
| 断网 | 静默失败，显示上次算好的结果 |

增量 bar 存在 `localStorage`（一天约 4 KB），不存整包快照。

### 权限

| 权限 | 用途 |
|---|---|
| `INTERNET` | 拉腾讯行情快照与日线 |
| `ACCESS_NETWORK_STATE` | 判断网络可用性 |

**不上传任何数据，不接第三方统计，不申请存储、位置、通讯录权限。**

## 九、更新过程的反馈：只有一个圆

行情更新全程**不弹任何文字提示**，只在标题栏右侧（主题按钮左边）显示一个 40px 的圆环：

| 阶段 | 圆环表现 |
|---|---|
| 请求快照 / 本地重算 | 一小段圆弧旋转（不确定进度） |
| 补齐缺失交易日 | 圆弧按完成比例推进（确定进度） |
| 成功 | 变绿 + 对勾，停留 0.5 秒后淡出 |
| 失败（仅手动刷新时） | 变红 + 叉，停留 1.5 秒后淡出 |
| 失败（自动同步时） | 直接消失，不打扰 |

为什么这样做：自动同步在每次打开 App 时都会跑，用 toast 报「正在获取行情…」「补齐中 40/260」
属于噪音；手动点刷新时用户也不需要读文字，看一眼环就知道成没成。失败原因不弹窗，
写进「更多 → 数据与计算 → 更新状态」，需要时查。

细节：
- 圆环放在标题栏内部，不占据内容高度，也不遮挡任何卡片（早先放在标题栏下方会压住首屏卡片）
- `pointer-events: none`，永远不会挡点击
- 圆环颜色跟随主题（浅色主题下用深蓝，避免在白底上看不清）
- 系统开启「减少动态效果」时，旋转改为透明度呼吸

## 十、盘中数据：为什么不能直接落盘

这一条关系到信号正确性，比 UI 重要得多。

**问题**：腾讯的日线与快照接口在交易时段（9:30–15:00）会返回当天**未完成**的 K 线。
如果直接写进历史，会同时踩三个坑：

1. `close` 是盘中价，10:00 与 14:50 能差 1%；
2. `volume` 只有半天的量，量比、20 日均额全部偏小；
3. 更糟的是**只覆盖部分标的**——谁先返回谁有。于是同一天里一部分是真数据、
   一部分是前一天的 ffill 值，横截面比较彻底失真。

**手机端**：判定「快照日期 = 今天 且 快照时间 < 15:00」为盘中，此时只更新实时价
（给下单表算手数用），**不写 bar**，信号继续基于上一个完整收盘日。界面标注「盘中 / 未收盘」。

**桌面端**：`etfquant/datafeed.py` 增加 `is_market_closed()` 与 `drop_unfinished_today()`，
在写缓存前剔除当日未收盘的 bar；缓存新鲜度判定在盘中也回退一档，
避免「今天还没收盘 → 永远判定缓存不新鲜 → 反复全量重下」。

这不是理论风险。改动前实测：某次盘中运行下载脚本后，**261 只里 162 只**的缓存被写入了
当日盘中价，99 只仍是前一交易日，池内出现「半天数据 + ffill 数据」混排，
并据此生成了信号。已回滚这 162 行并重新生成。
