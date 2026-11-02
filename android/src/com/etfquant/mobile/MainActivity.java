package com.etfquant.mobile;

import android.app.Activity;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.pm.PackageManager;
import android.util.Base64;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.view.Window;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 单 Activity 的 WebView 壳。
 *
 * 设计要点
 * --------
 * 1. 全部资源在 assets/www 内，除网络（拉行情）与通知（调仓提醒）外不申请其他权限；
 * 2. 状态栏 / 导航栏颜色随页面主题切换（通过 JS 桥回调）；
 * 3. 返回键先交给页面处理（页面内切 Tab 优先），页面无可退时才退出 Activity；
 * 4. 关闭 WebView 的缩放与文字缩放，避免系统字体放大撑破版式。
 */
public class MainActivity extends Activity {

    private static final String DARK_BG = "#0a0d13";
    private static final String LIGHT_BG = "#f5f6f8";

    private WebView web;
    /** 异步 HTTP 用的线程池；onDestroy 里关闭。 */
    private final ExecutorService httpPool = Executors.newFixedThreadPool(4);

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(true);
        s.setAllowContentAccess(false);
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);
        // 跟随系统字体放大会把 KPI 数字撑破版式，这里固定 100%
        s.setTextZoom(100);
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);

        web.setBackgroundColor(Color.parseColor(DARK_BG));
        web.setOverScrollMode(View.OVER_SCROLL_NEVER);
        web.addJavascriptInterface(new Bridge(), "AndroidBridge");
        web.setWebViewClient(new WebViewClient());
        web.setWebChromeClient(new WebChromeClient());

        setContentView(web);
        applyBars(true);
        web.loadUrl("file:///android_asset/www/index.html");
    }

    /** 状态栏与导航栏跟随页面主题。 */
    private void applyBars(final boolean dark) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                Window w = getWindow();
                int bg = Color.parseColor(dark ? DARK_BG : LIGHT_BG);
                w.setStatusBarColor(bg);
                w.setNavigationBarColor(bg);

                View decor = w.getDecorView();
                int flags = decor.getSystemUiVisibility();
                if (dark) {
                    flags &= ~View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
                    if (Build.VERSION.SDK_INT >= 26) {
                        flags &= ~View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
                    }
                } else {
                    flags |= View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
                    if (Build.VERSION.SDK_INT >= 26) {
                        flags |= View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
                    }
                }
                decor.setSystemUiVisibility(flags);
            }
        });
    }

    /** 暴露给页面的原生能力。 */
    public class Bridge {
        /**
         * 同步 GET，返回 **Base64 后的原始字节**。
         *
         * 为什么不直接返回字符串：腾讯行情是 GBK、data.js 是 UTF-8，
         * 由 JS 侧用 TextDecoder 指定编码解码最省事，Java 侧只负责搬运字节。
         * 也不用 JS 的 fetch：页面在 file:// 下属于 opaque origin，跨域请求会被直接拦掉。
         *
         * 失败返回空串，由 JS 侧降级提示，不抛异常（避免 WebView 打印 JS 错误）。
         */
        @JavascriptInterface
        public String httpGetBase64(String url) {
            HttpURLConnection conn = null;
            try {
                conn = (HttpURLConnection) new URL(url).openConnection();
                conn.setConnectTimeout(4000);
                conn.setReadTimeout(15000);
                conn.setRequestMethod("GET");
                conn.setRequestProperty("Accept", "*/*");
                conn.setInstanceFollowRedirects(true);
                int code = conn.getResponseCode();
                if (code < 200 || code >= 300) return "";
                InputStream in = conn.getInputStream();
                ByteArrayOutputStream bos = new ByteArrayOutputStream();
                byte[] buf = new byte[8192];
                int n;
                while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
                in.close();
                return Base64.encodeToString(bos.toByteArray(), Base64.NO_WRAP);
            } catch (Exception e) {
                return "";
            } finally {
                if (conn != null) conn.disconnect();
            }
        }

        @JavascriptInterface
        public void copyText(String text) {
            final ClipboardManager cm =
                    (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
            if (cm == null) return;
            cm.setPrimaryClip(ClipData.newPlainText("ETF Quant", text));
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    Toast.makeText(MainActivity.this, "已复制下单清单", Toast.LENGTH_SHORT).show();
                }
            });
        }

        @JavascriptInterface
        public void setTheme(String theme) {
            applyBars(!"light".equals(theme));
        }

        /**
         * 调仓日提醒：发一条系统通知。
         *
         * Android 13+ 需要运行时授权 POST_NOTIFICATIONS，这里先申请；用户还没授权时
         * 退化成 Toast —— 提醒这件事不该因为少一个权限就彻底静默。
         */
        @JavascriptInterface
        public void notify(final String title, final String text) {
            final String t = title == null ? "" : title;
            final String m = text == null ? "" : text;
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    if (Build.VERSION.SDK_INT >= 33
                            && checkSelfPermission("android.permission.POST_NOTIFICATIONS")
                               != PackageManager.PERMISSION_GRANTED) {
                        requestPermissions(
                                new String[]{"android.permission.POST_NOTIFICATIONS"}, 7001);
                        Toast.makeText(MainActivity.this, t + " · " + m, Toast.LENGTH_LONG).show();
                        return;
                    }
                    postNotification(t, m);
                }
            });
        }

        /**
         * 异步 GET：在后台线程池执行，完成后回调页面 window.__httpDone(cbId, b64)。
         *
         * 为什么必须有这个：同步版 httpGetBase64 虽然跑在 JavaBridge 线程上，
         * 但 JS 是**同步等返回**的——WebView 的 JS 线程在整个 HTTP 期间被阻塞，
         * 而它同时就是处理触摸事件的线程，表现为"点什么都没反应"。
         * 首装回补要打 260 个请求，过去整个加载期界面都是冻住的。
         * 页面侧的所有网络请求都应优先走本方法。
         */
        @JavascriptInterface
        public void httpGetBase64Async(final String url, final String cbId) {
            httpPool.execute(new Runnable() {
                @Override
                public void run() {
                    final String b64 = httpGetBase64(url);
                    runOnUiThread(new Runnable() {
                        @Override
                        public void run() {
                            if (web == null) return;   // Activity 已销毁
                            web.evaluateJavascript(
                                    "window.__httpDone && window.__httpDone("
                                            + jsonStr(cbId) + "," + jsonStr(b64) + ")", null);
                        }
                    });
                }
            });
        }

        @JavascriptInterface
        public String platform() {
            return "android";
        }
    }

    /** 最小 JSON 字符串转义：Base64 输出本身安全，但 cbId 来自页面，不能裸拼。 */
    private static String jsonStr(String s) {
        if (s == null) return "\"\"";
        StringBuilder sb = new StringBuilder(s.length() + 2);
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '"' || c == '\\') { sb.append('\\').append(c); }
            else if (c < 0x20) { sb.append(String.format("\\u%04x", (int) c)); }
            else { sb.append(c); }
        }
        return sb.append('"').toString();
    }

    private void postNotification(String title, String text) {
        try {
            NotificationManager nm =
                    (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm == null) return;
            String chId = "rebalance";
            if (Build.VERSION.SDK_INT >= 26) {
                NotificationChannel ch = new NotificationChannel(
                        chId, "调仓提醒", NotificationManager.IMPORTANCE_DEFAULT);
                ch.setDescription("调仓日与买入日各提醒一次");
                nm.createNotificationChannel(ch);
            }
            Notification.Builder b = Build.VERSION.SDK_INT >= 26
                    ? new Notification.Builder(this, chId)
                    : new Notification.Builder(this);
            b.setContentTitle(title).setContentText(text)
                    .setSmallIcon(android.R.drawable.stat_notify_more)
                    .setAutoCancel(true);
            nm.notify(2001, b.build());
        } catch (Throwable ignored) {
            // 通知失败不影响主流程：页面里的横幅已经在提示了
        }
    }

    @Override
    public void onBackPressed() {
        // 先问页面：能不能在内部后退（例如从"今日"回到"概览"）
        web.evaluateJavascript("(window.__maBack && window.__maBack()) ? 1 : 0",
                new android.webkit.ValueCallback<String>() {
                    @Override
                    public void onReceiveValue(String value) {
                        if (!"1".equals(value)) {
                            finish();
                        }
                    }
                });
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            web.removeJavascriptInterface("AndroidBridge");
            web.destroy();
            web = null;
        }
        httpPool.shutdownNow();
        super.onDestroy();
    }
}
