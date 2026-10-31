package io.github.kangwang42.superaimail;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ContentValues;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.MediaStore;
import android.view.Menu;
import android.view.MenuItem;
import android.view.View;
import android.view.WindowInsets;
import android.webkit.CookieManager;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;
import java.io.File;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;

public class MainActivity extends Activity {
    private WebView web;
    private TextView status;
    private String origin = "";
    private SharedPreferences preferences;

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        preferences = getSharedPreferences("connection", MODE_PRIVATE);
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setBackgroundColor(Color.rgb(248, 250, 252));
        layout.setOnApplyWindowInsetsListener((view, insets) -> {
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets bars = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
                view.setPadding(bars.left, bars.top, bars.right, bars.bottom);
            } else {
                view.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(), insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom());
            }
            return insets;
        });
        status = new TextView(this);
        status.setText("正在打开 SuperAIMail…");
        status.setTextSize(13);
        status.setPadding(16, 8, 16, 8);
        status.setTextColor(Color.rgb(71, 85, 105));
        status.setContentDescription("连接设置，点击切换本机或共享服务");
        status.setOnClickListener(v -> connectionDialog());
        layout.addView(status);
        web = new WebView(this);
        web.setBackgroundColor(Color.rgb(248, 250, 252));
        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setSupportZoom(true);
        settings.setBuiltInZoomControls(true);
        settings.setDisplayZoomControls(false);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, false);
        web.setWebChromeClient(new WebChromeClient());
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                String value = request.getUrl().toString();
                if (sameOrigin(value)) return false;
                if (request.isForMainFrame()) openExternal(request.getUrl());
                return true;
            }
        });
        web.setDownloadListener((url, agent, disposition, mime, size) -> {
            if (!sameOrigin(url)) { openExternal(Uri.parse(url)); return; }
            String cookie = CookieManager.getInstance().getCookie(url);
            new Thread(() -> download(url, cookie, disposition, mime), "superaimail-download").start();
        });
        layout.addView(web, new LinearLayout.LayoutParams(-1, 0, 1));
        setContentView(layout);
        connect(preferences.getString("server", ""));
    }

    private boolean sameOrigin(String value) {
        Uri target = Uri.parse(value), current = Uri.parse(origin);
        return current.getScheme() != null && current.getScheme().equals(target.getScheme())
            && current.getAuthority().equals(target.getAuthority());
    }

    private void openExternal(Uri uri) {
        if (!("https".equals(uri.getScheme()) || "http".equals(uri.getScheme()) || "mailto".equals(uri.getScheme()))) return;
        try { startActivity(new Intent(Intent.ACTION_VIEW, uri)); }
        catch (android.content.ActivityNotFoundException error) { Toast.makeText(this, "没有可打开此链接的应用", Toast.LENGTH_LONG).show(); }
    }

    private void connect(String remote) {
        status.setText("正在连接… · 点击设置");
        new Thread(() -> {
            try {
                if (!Python.isStarted()) Python.start(new AndroidPlatform(getApplicationContext()));
                String url;
                if (remote.isEmpty()) {
                    url = Python.getInstance().getModule("superaimail.embedded").callAttr("start", new File(getFilesDir(), "mail").getPath()).toString();
                } else {
                    Python.getInstance().getModule("superaimail.embedded").callAttr("stop");
                    url = remote;
                }
                origin = url;
                runOnUiThread(() -> {
                    status.setText((remote.isEmpty() ? "本机独立运行" : "共享服务 · " + Uri.parse(remote).getHost()) + " · 连接设置");
                    web.loadUrl(url);
                });
            } catch (Exception error) {
                runOnUiThread(() -> new AlertDialog.Builder(this).setTitle("无法启动").setMessage(error.getClass().getSimpleName() + "。请检查存储空间、服务地址或重新打开应用。")
                    .setPositiveButton("连接设置", (dialog, which) -> connectionDialog()).show());
            }
        }, "superaimail-start").start();
    }

    private void connectionDialog() {
        EditText field = new EditText(this);
        field.setSingleLine(true);
        field.setInputType(android.text.InputType.TYPE_CLASS_TEXT | android.text.InputType.TYPE_TEXT_VARIATION_URI);
        field.setHint("https://mail.example.com");
        field.setText(preferences.getString("server", ""));
        AlertDialog dialog = new AlertDialog.Builder(this).setTitle("连接本机或共享服务")
            .setMessage("留空使用手机内置后台；填写 HTTPS 地址可与电脑共用服务。切换不会删除本机邮件。")
            .setView(field).setNegativeButton("取消", null).setPositiveButton("连接", null).create();
        dialog.setOnShowListener(ignored -> dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v -> {
            String value = field.getText().toString().trim();
            Uri uri = Uri.parse(value);
            if (!value.isEmpty() && (!"https".equals(uri.getScheme()) || uri.getHost() == null || uri.getUserInfo() != null
                || (uri.getPath() != null && !uri.getPath().isEmpty() && !"/".equals(uri.getPath())) || uri.getQuery() != null || uri.getFragment() != null)) {
                field.setError("请输入不含路径和账户信息的 HTTPS 地址"); return;
            }
            value = value.replaceAll("/+$", "");
            preferences.edit().putString("server", value).apply();
            dialog.dismiss();
            connect(value);
        }));
        dialog.show();
    }

    private void download(String value, String cookie, String disposition, String mime) {
        Uri item = null;
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL(value).openConnection();
            connection.setConnectTimeout(20000); connection.setReadTimeout(120000); connection.setInstanceFollowRedirects(false);
            if (cookie != null) connection.setRequestProperty("Cookie", cookie);
            if (connection.getResponseCode() != 200) throw new java.io.IOException("下载返回 " + connection.getResponseCode());
            String filename = android.webkit.URLUtil.guessFileName(value, disposition, mime).replaceAll("[\\\\/:*?\"<>|\\p{Cntrl}]", "_");
            if (Build.VERSION.SDK_INT >= 29) {
                ContentValues values = new ContentValues();
                values.put(MediaStore.Downloads.DISPLAY_NAME, filename);
                values.put(MediaStore.Downloads.MIME_TYPE, mime == null ? "application/octet-stream" : mime);
                values.put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/SuperAIMail");
                values.put(MediaStore.Downloads.IS_PENDING, 1);
                item = getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values);
                if (item == null) throw new java.io.IOException("无法创建下载文件");
                try (OutputStream output = getContentResolver().openOutputStream(item); InputStream input = connection.getInputStream()) { copy(input, output); }
                values.clear(); values.put(MediaStore.Downloads.IS_PENDING, 0); getContentResolver().update(item, values, null, null);
            } else {
                File target = new File(getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS), System.currentTimeMillis() + "-" + filename);
                try (OutputStream output = new java.io.FileOutputStream(target); InputStream input = connection.getInputStream()) { copy(input, output); }
            }
            runOnUiThread(() -> Toast.makeText(this, "附件已保存到下载目录", Toast.LENGTH_LONG).show());
        } catch (Exception error) {
            if (item != null) getContentResolver().delete(item, null, null);
            runOnUiThread(() -> Toast.makeText(this, "下载失败，请检查连接后重试", Toast.LENGTH_LONG).show());
        } finally { if (connection != null) connection.disconnect(); }
    }

    private static void copy(InputStream input, OutputStream output) throws java.io.IOException {
        byte[] buffer = new byte[16384]; int size;
        while ((size = input.read(buffer)) != -1) output.write(buffer, 0, size);
    }

    @Override protected void onPause() { CookieManager.getInstance().flush(); super.onPause(); }
    @Override public void onBackPressed() { if (web.canGoBack()) web.goBack(); else super.onBackPressed(); }
    @Override protected void onDestroy() { if (web != null) web.destroy(); super.onDestroy(); }
}
