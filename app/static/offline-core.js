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
  // Slashes stay slashes so the viewer, the browse-grid thumbnails and
  // downloadWork's cache keys all name the same resource with the same string
  // (caches.match is exact-URL). Whole-path encodeURIComponent would turn "/"
  // into "%2F" and silently produce a second, non-matching URL for the same image.
  //
  // Client-only dev against the spec mock (#237): `?api_base=<mock>` re-bases
  // media URLs too (same rule as BookqApi in api/v1.js). SW importScripts has
  // no query — try/catch keeps it same-origin there.
  var _mediaBase = "";
  try {
    var _loc = (typeof window !== "undefined" && window.location) ? window.location : location;
    var _m = /[?&]api_base=([^&]+)/.exec(_loc.search || "");
    if (_m) _mediaBase = decodeURIComponent(_m[1]).replace(/\/+$/, "");
  } catch (e) { /* stay same-origin */ }
  function _murl(p) { return _mediaBase + p; }
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
    return _murl("/image/" + encodePath(inner));
  }

  // Browse-list cover builder (Todo 4): same per-segment encoding as
  // imageUrl, but targets the thumbnail endpoint. w is one of 200|400|800.
  // imageUrl stays the viewer + download source (never route those here).
  function thumbUrl(folder, name, w) {
    var width = (w === 200 || w === 800) ? w : 400;
    // Same empty-folder rule as imageUrl above: root covers are
    // "/thumb/<name>?w=..", never "/thumb//<name>?w=..".
    var inner = folder ? folder + "/" + name : name;
    return _murl("/thumb/" + encodePath(inner) + "?w=" + width);
  }

  // The cover width class this device's browse grid requests. dpr>=2 renders
  // the ~128px tile at >=256 device px, so the 400 class keeps covers sharp
  // (a 200px thumb stretched ~1.8x visibly blurs); below that, 200 is the
  // ~1.8x fit measured on the low-end Android target (decoded bitmaps stay
  // ~4x smaller — what keeps that device off the image-decode-cache cliff).
  // One rule drives sprites, single covers, and the enumerate lists below.
  function coverWidth() {
    var dpr = (typeof window !== "undefined" && window.devicePixelRatio) || 0;
    return dpr >= 2 ? 400 : 200;
  }

  // Reader-page URL targeting /view (server-normalized to a fast-decode
  // format). Only used via readerUrl, gated by OfflineCore.readerUseView.
  function viewUrl(folder, name) {
    var inner = folder ? folder + "/" + name : name;
    return _murl("/view/" + encodePath(inner));
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
  // first MAX_PER_NODE), dedup by URL. Single covers enumerate at
  // coverWidth() — the same device-dpr rule the DOM applies — so idle
  // prefetch's exact-URL caches.match lines up with the real requests.
  // Multi-thumb nodes enumerate their per-cell /thumb/?w=200 URLs, NOT the
  // sprite-sheet URL the DOM actually requests (one /api/thumb-sprite at a
  // dpr-dependent w plus v=2) — enumerate-vs-DOM is not byte-identical there,
  // which is fine because prefetch eligibility is DOM-scan driven and the
  // harness hit-rate was never exact on multi tiles anyway.
  // Cache policy (Todo 4): /thumb entries live in the
  // same per-work cache as /image (prefetch puts + SW caches.match hit the
  // exact same URL string, so no orphan entries); storeAdapter downloads stay
  // on /image (full-res reading source, untouched).
  // Todo 3: collect(nodes, {singleOnly:true}) emits ONLY the single cover per
  // node at coverWidth() — exactly what the single-first DOM requests on this
  // device, so no orphan entry is created and first-to-verdict stays
  // unified (no double-fetch). Card-img still requests w=400 explicitly and
  // is intentionally not enumerated — DOM-scan prefetch covers card URLs.
  // Android scroll perf (2026-10): w=400 singles on a dpr<2 device decoded
  // to ~52MB of bitmaps across a 57-tile grid → image decode cache thrash →
  // ~34ms frames. dpr-scaled singles cut that ~4x and restored ~22ms drag
  // cadence on the target device.
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
              const url = thumbUrl(fp, n.thumb, coverWidth());
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

  let _treeWriterGen = 0;
  let _treeWriterWorker = null;
  let _treeWriterSlot = null;
  function _cancelTreeWrites() {
    _treeWriterGen++;
    const slot = _treeWriterSlot;
    _treeWriterSlot = null;
    _treeWriterWorker = null;
    if (slot) { try { slot.cancel(); } catch (e) {} }
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
      _cancelTreeWrites();
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
    // Generation + cancellation are established at call time: the pending
    // slot's cancel() drops the queued idle callback (where cancelIdleCallback
    // exists) and settles the promise immediately, and a stale idle callback
    // re-checks before it can spawn the real write — latest-wins still holds
    // while nothing runs before idle.
    function _deferTreeJob(gen, run) {
      return new Promise(function (resolve, reject) {
        let settled = false;
        let idleId = null;
        const slot = {
          cancel() {
            if (settled) return;
            settled = true;
            if (idleId !== null) {
              try { if (typeof cancelIdleCallback === "function") cancelIdleCallback(idleId); } catch (e) {}
            }
            if (_treeWriterSlot === slot) _treeWriterSlot = null;
            resolve();
          }
        };
        idleId = requestIdleCallback(function () {
          if (settled || gen !== _treeWriterGen) return;
          settled = true;
          if (_treeWriterSlot === slot) _treeWriterSlot = null;
          try { Promise.resolve(run()).then(resolve, reject); }
          catch (e) { reject(e); }
        });
        _treeWriterSlot = slot;
      });
    }
    async function putTree(data, opts) {
      _cancelTreeWrites();
      const gen = _treeWriterGen;
      if (!Array.isArray(data)) return Promise.resolve();
      if (opts && opts.defer && typeof requestIdleCallback === "function") {
        return _deferTreeJob(gen, function () { return _writeTree(data, gen); });
      }
      return _writeTree(data, gen);
    }
    const TREE_WRITER_URL = "/static/offline/tree-writer.js";
    const TREE_WRITER_TIMEOUT_MS = 10000;
    function _parseTreeJson(jsonText, treeEnvelope) {
      let parsed;
      try { parsed = JSON.parse(jsonText); } catch (e) { return { error: e }; }
      const data = (treeEnvelope && parsed && !Array.isArray(parsed)) ? (parsed.nodes || parsed.tree) : parsed;
      if (!Array.isArray(data)) return { error: new Error("invalid tree snapshot") };
      return { data };
    }
    function putJson(jsonText, opts) {
      _cancelTreeWrites();
      const gen = _treeWriterGen;
      const treeEnvelope = !!(opts && opts.treeEnvelope);
      if (typeof jsonText !== "string" || !jsonText.length) return Promise.resolve();
      function start() {
        return new Promise((resolve, reject) => {
          function fallback() {
            if (gen !== _treeWriterGen) { resolve(); return; }
            const r = _parseTreeJson(jsonText, treeEnvelope);
            if (r.error) { reject(r.error); return; }
            _writeTree(r.data, gen).then(resolve, reject);
          }
          let worker = null;
          if (typeof Worker === "function") {
            try { worker = new Worker(TREE_WRITER_URL); } catch (e) { worker = null; }
          }
          if (!worker) { fallback(); return; }
          let settled = false;
          const timer = setTimeout(function () { fail(); }, TREE_WRITER_TIMEOUT_MS);
          function dispose(ownGlobals) {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            worker.onmessage = null;
            worker.onerror = null;
            if (ownGlobals) {
              if (_treeWriterSlot === slot) _treeWriterSlot = null;
              if (_treeWriterWorker === worker) _treeWriterWorker = null;
            }
            try { worker.terminate(); } catch (e) {}
          }
          const slot = {
            cancel() {
              if (settled) return;
              dispose(false);
              resolve();
            }
          };
          function settle(fn) {
            if (settled) return;
            dispose(true);
            fn();
          }
          function fail() {
            settle(function () {
              if (gen !== _treeWriterGen) { resolve(); return; }
              fallback();
            });
          }
          _treeWriterWorker = worker;
          _treeWriterSlot = slot;
          worker.onmessage = function (e) {
            if (settled || gen !== _treeWriterGen) return;
            const d = e && e.data;
            if (d && d.ok) { settle(resolve); return; }
            if (d && d.invalid) { settle(function () { reject(new Error("invalid tree snapshot")); }); return; }
            fail();
          };
          worker.onerror = function () {
            if (settled || gen !== _treeWriterGen) return;
            fail();
          };
          try { worker.postMessage({ text: jsonText, envelope: treeEnvelope }); } catch (e) { fail(); }
        });
      }
      // Defer holds the whole Worker/fallback write until idle; _deferTreeJob's
      // pending slot already owns cancellation so superseded jobs settle
      // without ever spawning or parsing. Once idle runs, start()'s worker
      // slot replaces it.
      if (opts && opts.defer && typeof requestIdleCallback === "function") {
        return _deferTreeJob(gen, start);
      }
      return start();
    }
    async function _writeTree(data, gen) {
      if (!Array.isArray(data)) return Promise.resolve();
      if (gen !== undefined && gen !== _treeWriterGen) return Promise.resolve();
      const snapshot = (typeof structuredClone === "function")
        ? structuredClone(data)
        : JSON.parse(JSON.stringify(data));
      try {
        const db = await openKV();
        if (gen !== undefined && gen !== _treeWriterGen) return;
        await new Promise((resolve, reject) => {
          const tx = db.transaction(KV, "readwrite");
          tx.objectStore(KV).put({ key: TREE_KEY, data: snapshot, savedAt: Date.now(), version: DB_VERSION });
          tx.oncomplete = () => resolve();
          tx.onerror = () => reject(tx.error);
          tx.onabort = () => reject(tx.error || new Error("tx aborted"));
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
      _cancelTreeWrites();
      try {
        const db = await openKV();
        await new Promise((resolve, reject) => {
          const req = db.transaction(KV, "readwrite").objectStore(KV).delete(TREE_KEY);
          req.onsuccess = () => resolve();
          req.onerror = () => reject(req.error);
        });
      } catch (e) { console.warn(e); }
    }
    return { DB_NAME, KV, TREE_KEY, DB_VERSION, openKV, _openKV: openKV, putTree, putJson, getTree, delTree };
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

  // ── Reading-progress localStorage key (#375) ──────────────────────────
  // Progress is scoped per authenticated account: `bookq_progress:<email>`
  // (the normalized user-state key index.html injects as
  // __BOOKQ_PROGRESS_USER__). localStorage is already origin-scoped, so the
  // email alone separates accounts on one server. The pre-#375 fixed key
  // `bookq_progress` is legacy: migrateProgress() moves it into the current
  // account's scoped key once, then removes it so a different account on
  // the same browser can never inherit it. With no injected user (harness
  // pages, SW context) the legacy key is used unchanged.
  const LEGACY_PROGRESS_KEY = "bookq_progress";

  function progressKey() {
    const u = globalThis.__BOOKQ_PROGRESS_USER__;
    return (typeof u === "string" && u) ? LEGACY_PROGRESS_KEY + ":" + u : LEGACY_PROGRESS_KEY;
  }

  // Every progress key present — the scoped one plus any stale legacy or
  // other-account keys a wipe must also remove.
  function progressStorageKeys() {
    const out = [];
    try {
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (k === LEGACY_PROGRESS_KEY || k.indexOf(LEGACY_PROGRESS_KEY + ":") === 0) out.push(k);
      }
    } catch (e) {}
    return out;
  }

  function migrateProgress() {
    const scoped = progressKey();
    if (scoped === LEGACY_PROGRESS_KEY) return;  // no account context — nothing to scope
    try {
      if (localStorage.getItem(scoped) === null && localStorage.getItem(LEGACY_PROGRESS_KEY) !== null) {
        localStorage.setItem(scoped, localStorage.getItem(LEGACY_PROGRESS_KEY));
      }
      localStorage.removeItem(LEGACY_PROGRESS_KEY);
    } catch (e) { console.warn(e); }
  }
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
    // Account-scoped progress keys (#375) — `bookq_progress` alone no
    // longer covers them.
    for (const k of progressStorageKeys()) {
      if (!report.keys.includes(k)) report.keys.push(k);
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
    for (const k of progressStorageKeys()) {
      try { if (localStorage.getItem(k) !== null) { localStorage.removeItem(k); if (!deleted.keys.includes(k)) deleted.keys.push(k); } } catch (e) {}
    }
    for (const k of WIPE_SESSION_KEYS) {
      try { if (sessionStorage.getItem(k) !== null) { sessionStorage.removeItem(k); deleted.keys.push(k); } } catch (e) {}
    }
    // Server-held resume point: without this a wiped device would resurrect
    // another device's "続きから" on the next boot pull. progress/sync.js is
    // absent in the SW importScripts context — typeof guard, not window.X &&.
    if (typeof ProgressSync !== "undefined" && ProgressSync.remove) {
      try { deleted.server = await ProgressSync.remove(); } catch (e) { console.warn(e); }
    }
    return deleted;
  }

  // globalThis, not window: sw.js importScripts()es this file and a Service
  // Worker has no window (globalThis === self there, === window in a page).
  globalThis.OfflineStore = OfflineStore;
  globalThis.OfflineTree = OfflineTree;
  globalThis.OfflineThumb = OfflineThumb;
  var api = { encodePath, imageUrl, thumbUrl, viewUrl, coverWidth, cacheNameFor, escapeHtml, folderHasOnlyImages, folderHasImages, fmt, dedupedBytes, rawBytes, OfflineStore, OfflineThumb, OfflineTree, collectThumbUrls: OfflineThumb.collectThumbUrls, wipeTargets, wipeClientData, isDataCacheName, SHELL_CACHE_PREFIX, DATA_CACHE_PREFIXES, WIPE_STORAGE_KEYS, WIPE_SESSION_KEYS, progressKey, progressStorageKeys, migrateProgress };
  // Single reader-page URL source for the viewer AND downloadWork — the flag
  // (?view=jpeg / localStorage bookq_view=jpeg, set by app.js) must resolve
  // identically in both or offline caches would key pages the viewer never
  // requests (caches.match is exact-URL).
  api.readerUseView = false;
  api.readerUrl = function (folder, name) {
    return api.readerUseView ? viewUrl(folder, name) : imageUrl(folder, name);
  };
  globalThis.OfflineCore = api;
})();
