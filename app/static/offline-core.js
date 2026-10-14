/* bookq offline core — the pieces app.js and offline-harness.html must
 * agree on byte-for-byte. The harness exists to report the app's real offline
 * state, so a second copy of the store, the cache naming, the size formatting
 * or the dedup sum would make it lie the moment either side drifts.
 *
 * Plain global script (no build step, no module): defines OfflineStore and
 * OfflineCore on globalThis. Load it BEFORE app.js / the harness inline
 * script. Published on globalThis rather than window so sw.js can pull the
 * same file in with importScripts() (a Service Worker has no window) and
 * share cacheNameFor instead of keeping its own copy.
 */
(function () {
  // Per-segment percent-encoding — the single URL builder behind /image/
  // (imageUrl: viewer + downloads) and /thumb/ (thumbUrl: browse covers).
  // Slashes stay slashes so the viewer, the browse-grid thumbnails and downloadWork's cache
  // keys all name the same resource with the same string (caches.match is
  // exact-URL). Whole-path encodeURIComponent would turn "/" into "%2F" and
  // silently produce a second, non-matching URL for the same image.
  function encodePath(p) {
    return String(p).split("/").map(encodeURIComponent).join("/");
  }

  // Empty folder means a root-level file: emit "/image/<name>" with no
  // inner leading slash. The server intentionally keeps strict slash rules
  // ("/image//.." path -> 400), so clients must not emit "//". Root URLs
  // were previously 400/unused, so no cached entries can mismatch the new
  // shape (caches.match is exact-URL).
  function imageUrl(folder, name) {
    var inner = folder ? folder + "/" + name : name;
    return "/image/" + encodePath(inner);
  }

  // Browse-list cover builder (Todo 4): same per-segment encoding as
  // imageUrl, but targets the thumbnail endpoint. w is one of 200|400|800.
  // imageUrl stays the viewer + download source (never route those here).
  function thumbUrl(folder, name, w) {
    var width = (w === 200 || w === 800) ? w : 400;
    // Same empty-folder rule as imageUrl above: root covers are
    // "/thumb/<name>?w=..", never "/thumb//<name>?w=..".
    var inner = folder ? folder + "/" + name : name;
    return "/thumb/" + encodePath(inner) + "?w=" + width;
  }

  function cacheNameFor(folder) {
    return "bookq-work-" + encodeURIComponent(folder) + "-v1";
  }

  // Single HTML escaper for every renderer (app.js, browse/render.js,
  // search/facets.js, offline/storeAdapter.js, offline-harness.html). Escapes
  // the apostrophe too, so a single-quoted attribute is as safe as a
  // double-quoted one.
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // A folder node is a "leaf work" when it holds images and nothing else —
  // the predicate that decides whether a tile opens the viewer or drills down,
  // and what downloadWork treats as one cacheable unit. One definition, used
  // by app.js, offline/storeAdapter.js, browse/render.js and the harness.
  function folderHasOnlyImages(node) {
    return !!(node && node.children && node.children.length > 0
      && node.children.every((c) => c.type === "image"));
  }

  // A folder node "has images" when at least one direct child is an image —
  // looser sibling of folderHasOnlyImages: mixed folders (images +
  // subfolders) are true here, false there. Answers "is there anything
  // directly readable here" without touching leaf/download semantics.
  function folderHasImages(node) {
    return !!(node && node.children && node.children.length > 0
      && node.children.some((c) => c.type === "image"));
  }

  function fmt(bytes) {
    bytes = Number(bytes) || 0;
    if (bytes < 1e9) return (bytes / 1e6).toFixed(1) + " MB";
    return (bytes / 1e9).toFixed(1) + " GB";
  }

  // Sum of record bytes, skipping any record that has a descendant record.
  function dedupedBytes(all) {
    const folderSet = new Set(all.map((w) => w.folder));
    let sum = 0;
    for (const w of all) {
      let isParent = false;
      for (const f of folderSet) {
        if (f !== w.folder && f.startsWith(w.folder + "/")) { isParent = true; break; }
      }
      if (!isParent) sum += (w.bytes || 0);
    }
    return sum;
  }

  function rawBytes(all) {
    return all.reduce((s, w) => s + (w.bytes || 0), 0);
  }

  // Thumb enumeration shared by app.js prefetch and offline-harness hit-rate.
  // Single source for: per-segment thumbUrl, per-node cap (thumbs>=2 →
  // first MAX_PER_NODE), dedup by URL. Widths mirror the DOM renders exactly
  // (single cover w=400, multi-grid w=200) so prefetch's exact-URL match/put
  // lines up with what the grid requests — no orphan entries. Returns
  // [{url, folder, thumb}]. Cache policy (Todo 4): /thumb entries live in the
  // same per-work cache as /image (prefetch puts + SW caches.match hit the
  // exact same URL string, so no orphan entries); storeAdapter downloads stay
  // on /image (full-res reading source, untouched).
  // Todo 3: collect(nodes, {singleOnly:true}) emits ONLY the single cover per
  // node at w=200 — exactly what the single-first DOM requests, so idle
  // prefetch's exact-URL caches.match hits and no w=400 orphan is created
  // (first-to-verdict stays w=200 unified, no double-fetch). Default (no opts)
  // is unchanged: single w=400 + multi w=200 (full-mode DOM contract).
  const OfflineThumb = (function () {
    const MAX_PER_NODE = 9;
    const MAX_TOTAL = 50;
    function collect(nodes, opts) {
      const singleOnly = !!(opts && opts.singleOnly);
      const out = [];
      const seen = new Set();
      (function walk(list) {
        for (const n of list || []) {
          const fp = n._fullPath;
          if (fp) {
            if (n.thumb) {
              const url = singleOnly ? thumbUrl(fp, n.thumb, 200) : thumbUrl(fp, n.thumb);
              if (!seen.has(url)) { seen.add(url); out.push({ url, folder: fp, thumb: n.thumb }); }
            }
            if (!singleOnly && n.thumbs && Array.isArray(n.thumbs) && n.thumbs.length >= 2) {
              for (const t of n.thumbs.slice(0, MAX_PER_NODE)) {
                const u = thumbUrl(fp, t, 200);
                if (!seen.has(u)) { seen.add(u); out.push({ url: u, folder: fp, thumb: t }); }
              }
            }
          }
          if (n.children) walk(n.children);
        }
      })(nodes);
      return out;
    }
    function urls(nodes, opts) {
      return collect(nodes, opts).map((e) => e.url);
    }
    // Todo 5 integration name: collectThumbUrls(nodes, {singleOnly:true}).
    function collectThumbUrls(nodes, opts) {
      return collect(nodes, opts);
    }
    return { MAX_PER_NODE, MAX_TOTAL, collect, urls, collectThumbUrls };
  })();

  const DB_NAME = "bookq-offline";
  const DB_VERSION = 2;
  const STORE = "works";
  const KV = "kv";
  const TREE_KEY = "folders-tree";

  function handleUpgrade(db, oldVersion) {
    if (oldVersion < 1) {
      if (!db.objectStoreNames.contains(STORE)) {
        const st = db.createObjectStore(STORE, { keyPath: "folder" });
        st.createIndex("lastReadAt", "lastReadAt", { unique: false });
        st.createIndex("addedAt", "addedAt", { unique: false });
      }
    }
    if (oldVersion < 2) {
      if (!db.objectStoreNames.contains(KV)) {
        db.createObjectStore(KV, { keyPath: "key" });
      }
    }
  }

  // Single shared connection per DB name. Every OfflineStore.OfflineTree call
  // reuses it (instead of opening a fresh connection each time), so connections
  // do not accumulate and a future DB_VERSION bump never lands in a blocked
  // state because of orphaned connections. onversionchange closes it so the
  // upgrade can proceed; the next call reopens.
  const _dbPromises = Object.create(null);
  function openDB() {
    const name = DB_NAME;
    if (_dbPromises[name]) return _dbPromises[name];
    _dbPromises[name] = new Promise((resolve, reject) => {
      const req = indexedDB.open(name, DB_VERSION);
      req.onupgradeneeded = (e) => {
        handleUpgrade(req.result, e.oldVersion || 0);
      };
      req.onsuccess = () => {
        const db = req.result;
        // Drop the cached handle so the next call reopens instead of reusing
        // a closed connection (reusing it would fail every transaction).
        db.onversionchange = () => { _dbPromises[name] = null; db.close(); };
        db.onclose = () => { _dbPromises[name] = null; };
        resolve(db);
      };
      req.onerror = () => { _dbPromises[name] = null; reject(req.error); };
      req.onblocked = () => { _dbPromises[name] = null; reject(req.error || new Error("blocked")); };
    });
    return _dbPromises[name];
  }
  // Private hook for tests / harness that want a raw IDBDatabase handle,
  // independent of the shared connection singleton.
  function openRawDB() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = (e) => { handleUpgrade(req.result, e.oldVersion || 0); };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
      req.onblocked = () => reject(req.error || new Error("blocked"));
    });
  }

  const OfflineStore = (function () {
    function open() { return openDB(); }
    function get(folder) {
      return open().then((db) => new Promise((resolve, reject) => {
        const req = db.transaction(STORE, "readonly").objectStore(STORE).get(folder);
        req.onsuccess = () => resolve(req.result || null);
        req.onerror = () => reject(req.error);
      }));
    }
    function put(record) {
      return open().then((db) => new Promise((resolve, reject) => {
        const req = db.transaction(STORE, "readwrite").objectStore(STORE).put(record);
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
      }));
    }
    function del(folder) {
      return open().then((db) => new Promise((resolve, reject) => {
        const req = db.transaction(STORE, "readwrite").objectStore(STORE).delete(folder);
        req.onsuccess = () => resolve();
        req.onerror = () => reject(req.error);
      }));
    }
    function getAll() {
      return open().then((db) => new Promise((resolve, reject) => {
        const req = db.transaction(STORE, "readonly").objectStore(STORE).getAll();
        req.onsuccess = () => resolve(req.result || []);
        req.onerror = () => reject(req.error);
      }));
    }
    function getOldest() {
      return getAll().then((all) => {
        if (!all.length) return null;
        let oldest = all[0];
        for (const w of all) {
          const a = w.lastReadAt || w.addedAt || 0;
          const b = oldest.lastReadAt || oldest.addedAt || 0;
          if (a < b) oldest = w;
        }
        return oldest;
      });
    }
    async function storageEstimate() {
      try {
        if (navigator.storage && navigator.storage.estimate) return await navigator.storage.estimate();
      } catch(e) { console.warn(e); }
      return null;
    }
    async function cacheKeysCount(cacheName) {
      try {
        const c = await caches.open(cacheName);
        return (await c.keys()).length;
      } catch(e) { console.warn(e); return 0; }
    }
    // Wipe both stores: work records AND the KV snapshot (the cached tree).
    // Used by offline-core's wipeClientData; a partial wipe would leave the
    // tree snapshot behind and reload could still paint cached browse state.
    function clear() {
      return open().then((db) => new Promise((resolve, reject) => {
        const tx = db.transaction([STORE, KV], "readwrite");
        tx.objectStore(STORE).clear();
        tx.objectStore(KV).clear();
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error);
      }));
    }
    return { DB_NAME, STORE, DB_VERSION, cacheNameFor, open, _open: openRawDB, get, put, delete: del, getAll, getOldest, storageEstimate, cacheKeysCount, clear };
  })();

  const OfflineTree = (function () {
    function openKV() { return openDB(); }
    // Todo 5: opts.defer schedules the write on requestIdleCallback (falls
    // back to an immediate write where idle callbacks don't exist) so warm
    // revalidation never blocks the redraw. Default stays immediate so
    // reload-restore tests observe the snapshot without extra waiting.
    async function putTree(data, opts) {
      if (!Array.isArray(data)) return Promise.resolve();
      if (opts && opts.defer && typeof requestIdleCallback === "function") {
        return new Promise((resolve) => {
          requestIdleCallback(() => { _writeTree(data).then(resolve, resolve); });
        });
      }
      return _writeTree(data);
    }
    async function _writeTree(data) {
      if (!Array.isArray(data)) return Promise.resolve();
      const snapshot = (typeof structuredClone === "function")
        ? structuredClone(data)
        : JSON.parse(JSON.stringify(data));
      try {
        const db = await openKV();
        await new Promise((resolve, reject) => {
          const tx = db.transaction(KV, "readwrite");
          const st = tx.objectStore(KV);
          const req = st.put({ key: TREE_KEY, data: snapshot, savedAt: Date.now(), version: DB_VERSION });
          req.onsuccess = () => resolve(req.result);
          req.onerror = () => reject(req.error);
        });
      } catch (e) { console.warn(e); }
    }
    async function getTree() {
      try {
        const db = await openKV();
        const result = await new Promise((resolve, reject) => {
          const req = db.transaction(KV, "readonly").objectStore(KV).get(TREE_KEY);
          req.onsuccess = () => resolve(req.result || null);
          req.onerror = () => reject(req.error);
        });
        if (!result || !Array.isArray(result.data)) return null;
        return result.data;
      } catch (e) { console.warn(e); return null; }
    }
    async function delTree() {
      try {
        const db = await openKV();
        await new Promise((resolve, reject) => {
          const req = db.transaction(KV, "readwrite").objectStore(KV).delete(TREE_KEY);
          req.onsuccess = () => resolve();
          req.onerror = () => reject(req.error);
        });
      } catch (e) { console.warn(e); }
    }
    return { DB_NAME, KV, TREE_KEY, DB_VERSION, openKV, _openKV: openKV, putTree, getTree, delTree };
  })();

  // ── Client-data wipe ──────────────────────────────────────────────────────
  // Everything kept for offline reading lives in one of three places: per-work
  // /image+/thumb caches (created by *viewing* as well as by downloadWork), the
  // fallback + api caches, and IndexedDB (work records + the cached tree).
  // A wipe has to cover all three, including work caches with no IndexedDB
  // record: those orphans are invisible to the "saved works" list but still pin
  // stale bytes after the library changes on disk (the 2026-10-13 report).
  // The shell cache is deliberately kept — it holds the app code and the
  // offline page, is rebuilt by the SW install, and the harness itself has to
  // stay usable offline.
  const SHELL_CACHE_PREFIX = "bookq-shell-";
  const DATA_CACHE_PREFIXES = ["bookq-work-", "bookq-api-", "bookq-images-"];
  const WIPE_STORAGE_KEYS = ["bookq_progress", "bookq_persist_toast"];
  const WIPE_SESSION_KEYS = ["bookq_auto_sync_done", "bookq_return_hash"];
  // Kept on purpose (QA/registration flags, not reading data):
  // bookq_harness / bookq_enable_sw / bookq_touch / bookq_perf.

  function isDataCacheName(name) {
    return DATA_CACHE_PREFIXES.some((p) => String(name).startsWith(p));
  }

  // What a wipe would remove — drives the harness preview and button state, so
  // a client that only *viewed* a work (no saved record) can still wipe.
  async function wipeTargets() {
    const report = {caches: [], entries: 0, works: 0, tree: false, keys: []};
    try {
      for (const name of await caches.keys()) {
        if (!isDataCacheName(name)) continue;
        let count = 0;
        try { count = (await (await caches.open(name)).keys()).length; } catch (e) { console.warn(e); }
        report.caches.push({name, entries: count});
        report.entries += count;
      }
    } catch (e) { console.warn(e); }
    try { report.works = (await OfflineStore.getAll()).length; } catch (e) { console.warn(e); }
    try { report.tree = !!(await OfflineTree.getTree()); } catch (e) { console.warn(e); }
    for (const k of WIPE_STORAGE_KEYS) {
      try { if (localStorage.getItem(k) !== null) report.keys.push(k); } catch (e) {}
    }
    return report;
  }

  async function wipeClientData() {
    const report = await wipeTargets();
    const deleted = {caches: [], entries: 0, works: report.works, tree: report.tree, keys: []};
    for (const c of report.caches) {
      try {
        if (await caches.delete(c.name)) { deleted.caches.push(c.name); deleted.entries += c.entries; }
      } catch (e) { console.warn(e); }
    }
    try { await OfflineStore.clear(); } catch (e) { console.warn(e); }  // records + tree snapshot
    for (const k of WIPE_STORAGE_KEYS) {
      try { if (localStorage.getItem(k) !== null) { localStorage.removeItem(k); deleted.keys.push(k); } } catch (e) {}
    }
    for (const k of WIPE_SESSION_KEYS) {
      try { if (sessionStorage.getItem(k) !== null) { sessionStorage.removeItem(k); deleted.keys.push(k); } } catch (e) {}
    }
    return deleted;
  }

  // globalThis, not window: sw.js importScripts()es this file and a Service
  // Worker has no window (globalThis === self there, === window in a page).
  globalThis.OfflineStore = OfflineStore;
  globalThis.OfflineTree = OfflineTree;
  globalThis.OfflineThumb = OfflineThumb;
  globalThis.OfflineCore = { encodePath, imageUrl, thumbUrl, cacheNameFor, escapeHtml, folderHasOnlyImages, folderHasImages, fmt, dedupedBytes, rawBytes, OfflineStore, OfflineThumb, OfflineTree, collectThumbUrls: OfflineThumb.collectThumbUrls, wipeTargets, wipeClientData, isDataCacheName, SHELL_CACHE_PREFIX, DATA_CACHE_PREFIXES, WIPE_STORAGE_KEYS, WIPE_SESSION_KEYS };
})();
