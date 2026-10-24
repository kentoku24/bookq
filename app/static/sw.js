/* bookq Service Worker — Todo3 full strategies
 * - install: SHELL precache + skipWaiting
 * - activate: delete old bookq-* caches (whitelist), enable navigationPreload, clients.claim
 * - fetch: guard + cacheFirst (per-work) / networkFirst (api) / navigate fallback
 */
const SHELL_CACHE = "bookq-shell-v2";
const API_CACHE = "bookq-api-v1";
const FALLBACK_CACHE = "bookq-images-fallback";
const SHELL_ASSETS = ["/", "/static/offline.html", "/static/manifest.json", "/static/style.css", "/static/app.js", "/static/offline-core.js", "/static/browse/hash.js", "/static/browse/render.js", "/static/browse/scroll.js", "/static/search/facets.js", "/static/offline/storeAdapter.js", "/static/offline/tree-writer.js", "/static/thumbs/resolveCover.js", "/static/thumbs/prefetch.js", "/static/viewer/spread.js", "/static/viewer/navigation.js", "/static/viewer/gesture.js", "/static/viewer/zoom.js", "/static/viewer/overview.js", "/static/icons/icon-192.png", "/static/icons/icon-512.png", "/static/icons/icon-512-maskable.png"];

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
    // /thumb/ shares the per-work cache with /image: prefetch.js stores covers
    // there (cacheNameFor(folder)) and caches.match() reads that copy back, so
    // writing refreshed thumbs anywhere else would update a copy nobody reads.
    // /view/ (normalized reader pages) keys into the same work cache.
    const prefix = url.pathname.startsWith('/image/') ? '/image/'
      : url.pathname.startsWith('/thumb/') ? '/thumb/'
      : url.pathname.startsWith('/view/') ? '/view/'
      : null;
    if (!prefix) return null;
    const parts = url.pathname.slice(prefix.length).split('/');
    if (parts.length < 2) return null;
    const folder = decodeURIComponent(parts.slice(0, -1).join('/'));
    return cacheNameFor(folder);
  } catch {
    return null;
  }
}

// Page/cover bytes are cache-first, but a hit is revalidated with a
// conditional GET before it is trusted: a work's image URLs never change, so
// without this a re-downloaded (or repaired) file on disk could never reach a
// client that had already viewed it — the phone kept rendering the old bytes
// forever. 304 keeps the cached copy (no body transfer), 200 swaps the entry
// and the fresh bytes go straight to the page. Offline, a slow/unreachable
// origin, or an entry without validators falls back to the cached copy, so
// offline reading is never blocked by revalidation.
// The HTTP cache is bypassed ({cache:'no-store'}): Cache storage + ETag is the
// single freshness authority, otherwise a stale HTTP copy could satisfy a
// miss and re-seed the old bytes.
const REVALIDATE_TIMEOUT_MS = 1200;

// Rebuilding a Request from another Request inherits that request's header
// guard: an <img> load arrives as mode:"no-cors", whose guard drops every
// non-CORS-safelisted header — If-None-Match/If-Modified-Since vanish without
// an error in Chromium and WebKit alike, so the "conditional" GET went out bare.
// That made every hit a full re-download (no 304 ever) which also had to finish
// inside REVALIDATE_TIMEOUT_MS, i.e. a replaced image never landed on a slow
// origin. Same-origin URLs are therefore re-issued from their URL, where the
// guard keeps the validators (credentials stay equivalent: cookies are sent).
function revalidationRequest(req, headers, signal) {
  const init = {cache: "no-store", signal};
  if (new URL(req.url).origin === self.location.origin) {
    return new Request(req.url, {...init, headers, mode: "same-origin", credentials: "same-origin"});
  }
  // Cross-origin no-cors cannot carry validators anyway; keep the old shape so
  // the request at least still reaches the origin.
  return new Request(req, {...init, headers});
}

function revalidateRequest(req, hit) {
  const headers = new Headers();
  const etag = hit.headers.get("ETag");
  if (etag) headers.set("If-None-Match", etag);
  const lastModified = hit.headers.get("Last-Modified");
  if (lastModified) headers.set("If-Modified-Since", lastModified);
  if (!etag && !lastModified) return null; // nothing to validate against
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REVALIDATE_TIMEOUT_MS);
  return fetch(revalidationRequest(req, headers, controller.signal))
    .finally(() => clearTimeout(timer));
}

// Same-path /thumb/ URLs at the other width classes. A cover cached before
// the tile width-class change lives at a different w — matching it beats an
// offline miss (scaled cover > no cover).
function thumbWidthFallbacks(reqUrl) {
  try {
    const u = new URL(reqUrl);
    if (!u.pathname.startsWith('/thumb/')) return [];
    const cur = u.searchParams.get('w') || '400';
    return ['200', '400', '800'].filter(w => w !== cur).map(w => {
      const v = new URL(u.href);
      v.searchParams.set('w', w);
      return v.href;
    });
  } catch { return []; }
}

async function cacheFirst(req) {
  const cacheName = perWorkCacheName(req) || "bookq-images-fallback";
  const c = await caches.open(cacheName);
  let hit = await caches.match(req);
  if (!hit) {
    for (const alt of thumbWidthFallbacks(req.url)) {
      hit = await caches.match(alt);
      if (hit) break;
    }
  }
  if (!hit) {
    try {
      const res = await fetch(req, {cache: "no-store"});
      if (!res.ok || res.status===206) return res;
      await c.put(req, res.clone()).catch(() => {});
      return res;
    } catch {
      return Response.error();
    }
  }
  if (self.navigator && self.navigator.onLine === false) return hit;
  let fresh = null;
  try {
    fresh = await revalidateRequest(req, hit);
  } catch {
    fresh = null; // aborted (timeout) or unreachable — the cached copy wins
  }
  if (!fresh) return hit;
  if (fresh.status === 304) return hit;
  if (fresh.ok && fresh.status !== 206) {
    await c.put(req, fresh.clone()).catch(() => {});
    return fresh;
  }
  return hit;
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
    // Sprite URLs gained a v= cache-buster — pre-change caches hold the same
    // sheet under the v-less URL. Match that exactly (folder+names+w stay
    // in the query) rather than the broad ignoreSearch below, which ignores
    // `folder`/`names` too and could serve a DIFFERENT folder's sheet.
    if (!hit && req.url.includes('/api/thumb-sprite')) {
      try {
        const u = new URL(req.url);
        u.searchParams.delete('v');
        hit = await caches.match(u.href);
      } catch { /* malformed URL — fall through */ }
    }
    if (!hit && req.url && req.url.includes('/api/folders')) {
      hit = await caches.match('/api/folders');
    }
    if (!hit && !req.url.includes('/api/thumb-sprite')) hit = await caches.match(req, { ignoreSearch: true });
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

  // Image CacheFirst (per-work) — /view/ reader pages share this path.
  if ((url.pathname.startsWith('/image/') || url.pathname.startsWith('/view/') || request.destination==='image') && !url.pathname.startsWith('/static/')) {
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
