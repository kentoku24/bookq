/* bookq Service Worker — Todo3 full strategies
 * - install: SHELL precache + skipWaiting
 * - activate: delete old bookq-* caches (whitelist), enable navigationPreload, clients.claim
 * - fetch: guard + cacheFirst (per-work) / networkFirst (api) / navigate fallback
 */
const SHELL_CACHE = "bookq-shell-v2";
const API_CACHE = "bookq-api-v1";
const FALLBACK_CACHE = "bookq-images-fallback";
const SHELL_ASSETS = ["/", "/static/offline.html", "/static/manifest.json", "/static/style.css", "/static/app.js", "/static/offline-core.js", "/static/browse/hash.js", "/static/browse/render.js", "/static/browse/scroll.js", "/static/search/facets.js", "/static/offline/storeAdapter.js", "/static/thumbs/resolveCover.js", "/static/thumbs/prefetch.js", "/static/viewer/spread.js", "/static/viewer/gesture.js", "/static/viewer/zoom.js", "/static/viewer/overview.js", "/static/icons/icon-192.png", "/static/icons/icon-512.png", "/static/icons/icon-512-maskable.png"];

// Per-work cache naming comes from offline-core.js, the same file the page
// loads: a second copy here would rename every work's cache the moment either
// side drifted, and the page would stop finding what the SW cached.
// importScripts is fetched and stored with the SW script itself, so it also
// resolves while offline (offline-core.js is in SHELL_ASSETS too).
importScripts('/static/offline-core.js');
const { cacheNameFor } = self.OfflineCore;

function perWorkCacheName(req) {
  try {
    const url = new URL(req.url);
    if (!url.pathname.startsWith('/image/')) return null;
    const parts = url.pathname.slice(7).split('/');
    if (parts.length < 2) return null;
    const folder = decodeURIComponent(parts.slice(0, -1).join('/'));
    return cacheNameFor(folder);
  } catch {
    return null;
  }
}

async function cacheFirst(req) {
  const cacheName = perWorkCacheName(req) || "bookq-images-fallback";
  const hit = await caches.match(req);
  if (hit) return hit;
  try {
    const res = await fetch(req);
    if (!res.ok || res.status===206) return res;
    const clone = res.clone();
    const c = await caches.open(cacheName);
    await c.put(req, clone).catch(() => {});
    return res;
  } catch {
    return Response.error();
  }
}

async function networkFirst(req, cacheName) {
  try {
    const res = await fetch(req);
    if (res.ok) {
      const clone = res.clone();
      (await caches.open(cacheName)).put(req, clone).catch(() => {});
    }
    return res;
  } catch {
    let hit = await caches.match(req);
    if (!hit && req.url && req.url.includes('/api/folders')) {
      hit = await caches.match('/api/folders');
    }
    if (!hit) hit = await caches.match(req, { ignoreSearch: true });
    return hit || Response.error();
  }
}

async function staticSWR(req) {
  const hit = await caches.match(req);
  const fetchAndPut = fetch(req).then(async (res) => {
    if (res.ok && res.status!==206) {
      const c = await caches.open(SHELL_CACHE);
      await c.put(req, res.clone()).catch(() => {});
    }
    return res;
  });
  if (hit) {
    fetchAndPut.catch(() => {});
    return hit;
  }
  const res = await fetchAndPut.catch(() => null);
  if (res) return res;
  const raced = await caches.match(req);
  if (raced) return raced;
  if (req.destination === 'document') {
    const offline = await caches.match('/static/offline.html');
    if (offline) return offline;
  }
  return Response.error();
}

// install — precache shell and take over immediately
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(SHELL_CACHE)
      .then((cache) => Promise.allSettled(SHELL_ASSETS.map((u) => cache.add(new Request(u, {cache:'reload'})))))
      .then(() => self.skipWaiting()),
  );
});

// activate — delete old bookq-* caches except whitelist, enable navigation preload, claim clients
self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      if ("navigationPreload" in self.registration) {
        try {
          await self.registration.navigationPreload.enable();
        } catch (_) {}
      }
      const whitelist = [SHELL_CACHE, API_CACHE, FALLBACK_CACHE];
      const keys = await caches.keys();
      await Promise.all(
        keys
          .filter(
            (k) =>
              k.startsWith("bookq-") &&
              !whitelist.includes(k) &&
              !k.startsWith("bookq-work-"),
          )
          .map((k) => caches.delete(k)),
      );
      await self.clients.claim();
    })(),
  );
});

// fetch — guard + CacheFirst / NetworkFirst routing
self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method!=='GET' || request.headers.has('range')) return;

  const url = new URL(request.url);

  // Online-check probe: network-only, no cache fallback — VPN disconnect must be detected
  if (url.pathname === '/api/health' && (url.search.includes('online_check') || request.headers.get('X-Online-Check'))) {
    event.respondWith(fetch(request).catch(() => Response.error()));
    return;
  }

  // Static StaleWhileRevalidate — /static/* with asset destinations.
  // Include document (offline.html) and manifest so precached shell files
  // don't fall through to a raw network fetch that fails offline.
  if (url.pathname.startsWith('/static/') && ['script','style','image','font','css','document','manifest',''].includes(request.destination||'')) {
    event.respondWith(staticSWR(request));
    return;
  }

  // Image CacheFirst (per-work)
  if ((url.pathname.startsWith('/image/') || request.destination==='image') && !url.pathname.startsWith('/static/')) {
    event.respondWith(cacheFirst(request));
    return;
  }

  // API NetworkFirst
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(networkFirst(request, API_CACHE));
    return;
  }

  if (request.mode==='navigate') {
    event.respondWith(
      (async () => {
        try {
          const preload = await event.preloadResponse;
          if (preload) return preload;
          const res = await fetch(request);
          return res;
        } catch {
          // "/" is always precached, so it has to come AFTER offline.html —
          // otherwise the app shell answers every offline navigation and the
          // offline page can never be reached.
          let hit = await caches.match(request);
          if (!hit) hit = await caches.match(request, { ignoreSearch: true });
          if (!hit) hit = await caches.match("/static/offline.html");
          if (!hit) hit = await caches.match("/");
          if (hit) return hit;
          return Response.error();
        }
      })(),
    );
    return;
  }
});
