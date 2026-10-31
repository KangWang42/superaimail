/* Private mail and API responses are never stored in the service-worker cache. */
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', event => {
  if (event.request.mode === 'navigate') event.respondWith(fetch(event.request).catch(() => new Response(
    '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>SuperAIMail 离线</title><h1>暂时连不上邮件服务</h1><p>检查网络或确认运行 SuperAIMail 的设备已开机，然后刷新。</p></html>',
    {headers: {'Content-Type': 'text/html; charset=utf-8'}})));
});
