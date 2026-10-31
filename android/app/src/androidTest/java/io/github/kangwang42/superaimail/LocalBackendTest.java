package io.github.kangwang42.superaimail;

import android.test.ActivityInstrumentationTestCase2;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebView;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

public class LocalBackendTest extends ActivityInstrumentationTestCase2<MainActivity> {
    public LocalBackendTest() { super(MainActivity.class); }
    private WebView web;

    private WebView find(View view) {
        if (view instanceof WebView) return (WebView) view;
        if (view instanceof ViewGroup) {
            ViewGroup group = (ViewGroup) view;
            for (int i=0; i<group.getChildCount(); i++) { WebView found = find(group.getChildAt(i)); if (found != null) return found; }
        }
        return null;
    }

    private String evaluate(String script) throws Exception {
        AtomicReference<String> value = new AtomicReference<>();
        CountDownLatch done = new CountDownLatch(1);
        getActivity().runOnUiThread(() -> web.evaluateJavascript(script, result -> { value.set(result); done.countDown(); }));
        assertTrue("WebView evaluation timed out", done.await(15, TimeUnit.SECONDS));
        return value.get();
    }

    private void waitFor(String expression) throws Exception {
        long end = System.currentTimeMillis()+90000;
        while (System.currentTimeMillis()<end) {
            if ("true".equals(evaluate(expression))) return;
            Thread.sleep(250);
        }
        fail("WebView did not reach expected state: "+expression);
    }

    public void testEmbeddedPythonSetupAndMailSettings() throws Exception {
        MainActivity activity = getActivity();
        getInstrumentation().runOnMainSync(() -> web = find(activity.getWindow().getDecorView()));
        assertNotNull(web);
        waitFor("!!document.querySelector('#login-form')");
        assertEquals("true", evaluate("location.hostname==='127.0.0.1'"));
        evaluate("document.querySelector('[name=password]').value='isolated-test-password';var r=document.querySelector('[name=repeat]');if(r)r.value='isolated-test-password';document.querySelector('#login-form').requestSubmit();true");
        waitFor("!!document.querySelector('.shell')");
        assertEquals("false", evaluate("state.overview.ai.enabled"));
        evaluate("location.hash='#/settings';true");
        waitFor("!!document.querySelector('[data-do=add-account]')");
        evaluate("document.querySelector('[data-do=add-account]').click();true");
        waitFor("document.querySelector('#modal').open");
        assertEquals("true", evaluate("document.querySelector('#microsoft-login').hidden"));
        assertEquals("true", evaluate("document.documentElement.scrollWidth<=innerWidth"));
    }
}
