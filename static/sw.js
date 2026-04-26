/**
 * Minerva BI — Service Worker
 * Network First для admin-сторінок (завжди свіжі дані).
 * Cache First для статики (/static/).
 */

const CACHE_VER    = 'minerva-v1';
const STATIC_CACHE = CACHE_VER + '-static';
const PAGE_CACHE   = CACHE_VER + '-pages';

// ── Встановлення ─────────────────────────────────────────────────────────────
self.addEventListener('install', event => {
  // Не використовуємо skipWaiting() — новий SW чекає поки всі вкладки закриті,
  // щоб уникнути раптових перезавантажень сторінки при оновленні.
});

// ── Активація — видалити старі кеші ─────────────────────────────────────────
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys
          .filter(k => k.startsWith('minerva-') && k !== STATIC_CACHE && k !== PAGE_CACHE)
          .map(k => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

// ── Fetch стратегія ──────────────────────────────────────────────────────────
self.addEventListener('fetch', event => {
  const req = event.request;
  const url = new URL(req.url);

  // Тільки GET, тільки той самий origin
  if (req.method !== 'GET' || url.origin !== self.location.origin) return;

  // Не перехоплювати API та WebSocket
  if (url.pathname.startsWith('/api/') ||
      url.pathname.startsWith('/ai/chat/api/') ||
      url.pathname.startsWith('/ai/ws/')) return;

  // Статика — Cache First
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(cacheFirst(req, STATIC_CACHE));
    return;
  }

  // Admin і решта — Network First
  event.respondWith(networkFirst(req, PAGE_CACHE));
});

async function cacheFirst(req, cacheName) {
  const cached = await caches.match(req);
  if (cached) return cached;
  try {
    const res = await fetch(req);
    if (res.ok) {
      const cache = await caches.open(cacheName);
      cache.put(req, res.clone());
    }
    return res;
  } catch {
    return new Response('Офлайн', { status: 503 });
  }
}

async function networkFirst(req, cacheName) {
  try {
    const res = await fetch(req);
    if (res.ok && res.status < 400) {
      const cache = await caches.open(cacheName);
      cache.put(req, res.clone());
    }
    return res;
  } catch {
    const cached = await caches.match(req);
    if (cached) return cached;
    return new Response(
      `<!DOCTYPE html><html lang="uk"><head><meta charset="utf-8">
      <meta name="viewport" content="width=device-width,initial-scale=1">
      <title>Minerva — Офлайн</title>
      <style>
        body{background:#080a0f;color:#dde2ec;font-family:system-ui,sans-serif;
             display:flex;align-items:center;justify-content:center;
             min-height:100vh;margin:0;text-align:center;padding:20px;box-sizing:border-box}
        h2{color:#00d4aa;margin:0 0 8px}
        p{color:#6b7280;font-size:14px;margin:0 0 20px}
        button{background:#00d4aa;color:#080a0f;border:none;border-radius:8px;
               padding:10px 24px;font-size:14px;font-weight:600;cursor:pointer}
      </style></head>
      <body><div>
        <div style="font-size:52px;margin-bottom:16px">🏛️</div>
        <h2>Minerva недоступна</h2>
        <p>Перевірте інтернет-з'єднання і спробуйте знову</p>
        <button onclick="location.reload()">Спробувати знову</button>
      </div></body></html>`,
      { headers: { 'Content-Type': 'text/html; charset=utf-8' }, status: 503 }
    );
  }
}
