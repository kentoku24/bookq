/* bookq thumbs/prefetch — thumb URL collection & background prefetch.
 * Plain global script (no build): defines window.collectThumbPrefetchUrls,
 * window.prefetchThumbs, window.scheduleThumbPrefetch. Load before app.js.
 * Cache policy (Todo 4): /thumb entries are INCLUDED in the per-work cache
 * (same cacheNameFor(dir) as /image downloads). match + put below use the
 * exact same URL string, so no orphan entries; SW caches.match is global and
 * hits either home. storeAdapter downloads stay on /image (untouched).
 */
(function () {
  var PREFETCH_CONCURRENCY = 3;

  var _warnedNoThumbApi = false;

  // Todo 3: opts passthrough to OfflineThumb.collect — {singleOnly:true}
  // while window.__cc_singleCoverFirst so idle prefetch emits exactly the
  // widths the single-first DOM requests (w=200 singles, no w=400 upgrade).
  function collectThumbPrefetchUrls(nodes, out, seen, opts) {
    out = out || [];
    seen = seen || new Set();
    var api = window.OfflineThumb || (window.OfflineCore && window.OfflineCore.OfflineThumb);
    var fn = api && (api.collectThumbUrls || api.collect);
    if (!fn){
      if(!_warnedNoThumbApi){ _warnedNoThumbApi = true; console.error('ThumbPrefetch: OfflineThumb API missing, skipping prefetch'); }
      return out;
    }
    for (var e of fn.call(api, nodes, opts)) {
      if (!seen.has(e.url)) { seen.add(e.url); out.push(e); }
    }
    return out;
  }

  async function prefetchThumbs() {
    try {
      var tree = window.tree || window.__CC_tree;
      // Also try closure-exposed via window proxy (app.js defines window.tree getter)
      if (typeof window.tree !== 'undefined') tree = window.tree;
      // Fallback: try window.getTree if app exposes getter
      if (!tree && window.__BOOKQ_DEBUG && window.__BOOKQ_DEBUG.getTree) {
        try { tree = window.__BOOKQ_DEBUG.getTree(); } catch(e) { console.warn(e); }
      }
      if (!tree || !tree.length) return;
      var list = []; var seen = new Set();
      // Todo 3: while the single-first flag holds, collect ONLY the w=200
      // singles the DOM requests (no w=400 upgrade → no double-fetch, no
      // orphan cache entries — caches.match below hits exact URLs).
      var singleFirst = false;
      try { singleFirst = window.__cc_singleCoverFirst === true; } catch(e) {}
      // Todo 5: reuse the list setFullPathsAndIndex built in the same pass
      // as _fullPath/nodeMap (window.__cc_thumbIndex) — no second full walk.
      // Missing or stale cache (different tree reference) falls back to the
      // collect walk below, so behavior is identical either way.
      var reused = null;
      try {
        var idx = window.__cc_thumbIndex;
        if (idx && idx.tree === tree) reused = singleFirst ? idx.single : idx.full;
      } catch(e) { console.warn(e); }
      if (Array.isArray(reused)) {
        list = reused.slice();
      } else {
        collectThumbPrefetchUrls(tree, list, seen, singleFirst ? { singleOnly: true } : undefined);
      }
      // Visible-first ordering (Todo 3): DOM-visible folders' thumbs fetch
      // first so follow-up bytes land where seen. Cap-50 / concurrency-3 /
      // caches.match skip below are unchanged.
      var targets = list;
      try {
        var visible = new Set(Array.from(document.querySelectorAll('.tile[data-path]')).map(function (el) { return el.getAttribute('data-path'); }));
        if (visible.size) {
          var visFirst = [], hidRest = [];
          for (var ti = 0; ti < targets.length; ti++) {
            var it = targets[ti];
            if (visible.has(it.folder)) visFirst.push(it); else hidRest.push(it);
          }
          targets = visFirst.concat(hidRest);
        }
      } catch(e) { console.warn(e); }
      if (targets.length > 50) {
        try {
          var visible50 = new Set(Array.from(document.querySelectorAll('.tile[data-path]')).map(function (el) { return el.getAttribute('data-path'); }));
          if (visible50.size) {
            var filtered = targets.filter(function (it) { return visible50.has(it.folder); });
            if (filtered.length) targets = filtered.slice(0, 50);
            else targets = targets.slice(0, 50);
          } else {
            targets = targets.slice(0, 50);
          }
        } catch(e) { console.warn(e); }
        if (targets.length > 50) targets = targets.slice(0, 50);
      }
      if (!targets.length) return;
      var idx = 0;
      var quotaExceeded = false;
      async function worker() {
        while (!quotaExceeded && idx < targets.length) {
          var cur = targets[idx++];
          var url = cur.url;
          try { var hit = await caches.match(url); if (hit) continue; } catch(e) { console.warn(e); }
          try {
            var r = await fetch(url, { credentials: 'same-origin' });
            if (!r.ok) continue;
            var cacheClone = r.clone();
            var sizeClone = r.clone();
            var fullPath = cur.folder + '/' + cur.thumb;
            var dir = fullPath.slice(0, fullPath.lastIndexOf('/'));
            if (!dir) continue;
            var OS = window.OfflineStore;
            var cacheName = OS ? OS.cacheNameFor(dir) : 'bookq-work-' + encodeURIComponent(dir) + '-v1';
            var c = await caches.open(cacheName);
            await c.put(url, cacheClone);
            // Prefetch intentionally discards the byte size: unlike storeAdapter.js
            // (which sums content-length || blob().size for the storage bar),
            // background thumb prefetch does no bytes accounting.
            try { await sizeClone.blob(); } catch(e) { if (window.__BOOKQ_DEBUG) console.warn(e); }
          } catch (e) {
            if (e && e.name === 'QuotaExceededError') { quotaExceeded = true; break; }
            continue;
          }
        }
      }
      await Promise.all(Array.from({ length: PREFETCH_CONCURRENCY }, function () { return worker(); }));
    } catch(e) { console.warn(e); }
  }

  function scheduleThumbPrefetch() {
    // Todo 3: same idle slot also schedules the DOM-side multi hydration
    // (render.js scheduleMultiHydration upgrades leftover single-first tiles,
    // visible-first; no-op when the full render already replaced them).
    var fn = function () {
      prefetchThumbs().catch(function (e) { console.warn(e); });
      try { if (window.scheduleMultiHydration) window.scheduleMultiHydration(); } catch (e) { console.warn(e); }
    };
    if (typeof requestIdleCallback === 'function') requestIdleCallback(fn);
    else setTimeout(fn, 0);
  }

  window.collectThumbPrefetchUrls = collectThumbPrefetchUrls;
  window.prefetchThumbs = prefetchThumbs;
  window.scheduleThumbPrefetch = scheduleThumbPrefetch;
  window.ThumbPrefetch = { collectThumbPrefetchUrls, prefetchThumbs, scheduleThumbPrefetch, PREFETCH_CONCURRENCY };
})();
