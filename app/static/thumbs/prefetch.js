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
  var PREFETCH_CAP = 50;

  var _warnedNoThumbApi = false;

  // Todo 3: opts passthrough to OfflineThumb.collect — {singleOnly:true}
  // while window.__cc_singleCoverFirst so idle prefetch emits exactly the
  // widths the single-first DOM requests (w=200 singles).
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

  function _sidebarScroller(){ return document.getElementById('sidebar-scroll'); }
  function _sidebarOpen(){
    var sb = document.getElementById('sidebar');
    return !!(sb && sb.classList.contains('open'));
  }
  function _viewportRect(){
    var s = _sidebarScroller();
    var r0 = s ? s.getBoundingClientRect() : null;
    var vh = window.innerHeight || (document.documentElement && document.documentElement.clientHeight) || 0;
    var vw = window.innerWidth || (document.documentElement && document.documentElement.clientWidth) || 0;
    return {
      top: Math.max(0, r0 ? r0.top : 0),
      bottom: Math.min(vh, r0 ? r0.bottom : vh),
      left: Math.max(0, r0 ? r0.left : 0),
      right: Math.min(vw, r0 ? r0.right : vw),
    };
  }
  function _rankOf(r, vp){
    if (r.right <= vp.left || r.left >= vp.right) return -1;
    if (r.bottom > vp.top && r.top < vp.bottom) return 0;
    var h = Math.max(1, vp.bottom - vp.top);
    if (r.bottom <= vp.top && r.bottom >= vp.top - h) return 1;
    if (r.top >= vp.bottom && r.top <= vp.bottom + h) return 2;
    return -1;
  }
  function _isThumbUrl(u){
    return u && (u.indexOf('/thumb/') === 0 || u.indexOf('/api/v1/thumb-sprite') === 0);
  }
  function _dirForUrl(url, tileFolder){
    if (url.indexOf('/api/v1/thumb-sprite') === 0){
      var m = /[?&]folder=([^&]*)/.exec(url);
      if (m){ try { return decodeURIComponent(m[1]) || null; } catch(e) { return null; } }
      return tileFolder || null;
    }
    if (url.indexOf('/thumb/') === 0){
      var path = url.slice(7);
      var q = path.indexOf('?'); if (q !== -1) path = path.slice(0, q);
      var dec;
      try { dec = path.split('/').map(decodeURIComponent).join('/'); } catch(e) { return null; }
      var slash = dec.lastIndexOf('/');
      if (slash === -1) return null;
      return dec.slice(0, slash);
    }
    return null;
  }
  // Visible-first ordering (Todo 3): DOM-visible folders' thumbs fetch
  // first so follow-up bytes land where seen. Cap-50 / concurrency-3 /
  // caches.match skip below are unchanged.
  function _eligibleTargets(){
    var grid = document.getElementById('tile-grid');
    if (!grid) return [];
    var vp = _viewportRect();
    var els = grid.querySelectorAll('.tile[data-path], .item[data-path], .card[data-path]');
    var groups = [[], [], []];
    for (var i = 0; i < els.length; i++){
      var el = els[i];
      var r;
      try { r = el.getBoundingClientRect(); } catch(e){ continue; }
      if (!r || (r.width === 0 && r.height === 0)) continue;
      var rank = _rankOf(r, vp);
      if (rank < 0) continue;
      var urls = [];
      var imgs = el.querySelectorAll('img[src]');
      for (var j = 0; j < imgs.length; j++){
        var u = imgs[j].getAttribute('src');
        if (_isThumbUrl(u)) urls.push(u);
      }
      if (urls.length) groups[rank].push({el: el, urls: urls});
    }
    var ordered = groups[0].concat(groups[1], groups[2]);
    var out = []; var seen = new Set();
    for (var t = 0; t < ordered.length; t++){
      var tile = ordered[t];
      for (var k = 0; k < tile.urls.length; k++){
        var url = tile.urls[k];
        if (seen.has(url)) continue;
        var dir = _dirForUrl(url, tile.el.getAttribute('data-path') || '');
        if (!dir) continue;
        seen.add(url);
        out.push({url: url, dir: dir});
        if (out.length >= PREFETCH_CAP) return out;
      }
    }
    return out;
  }

  var _batchGen = 0;
  var _activeController = null;
  var _activeDone = null;
  function cancelThumbPrefetch(){
    _batchGen++;
    var c = _activeController;
    _activeController = null;
    if (c){ try { c.abort(); } catch(e){} }
  }

  async function prefetchThumbs() {
    cancelThumbPrefetch();
    if (typeof document !== 'undefined' && document.hidden) return;
    if (!_sidebarOpen()) return;
    // Also try closure-exposed via window proxy (app.js defines window.tree getter)
    // Fallback: try window.getTree if app exposes getter
    // Todo 3: while the single-first flag holds, collect ONLY the w=200
    // singles the DOM requests (no double-fetch, no orphan cache entries —
    // caches.match below hits exact URLs).
    // Todo 5: reuse the list setFullPathsAndIndex built in the same pass
    // as _fullPath/nodeMap (window.__cc_thumbIndex) — no second full walk.
    // Missing or stale cache (different tree reference) falls back to the
    // collect walk below, so behavior is identical either way.
    var gen = _batchGen;
    var controller = (typeof AbortController === 'function') ? new AbortController() : null;
    _activeController = controller;
    var signal = controller ? controller.signal : null;
    var prev = _activeDone;
    var doneResolve = null;
    _activeDone = new Promise(function (r) { doneResolve = r; });
    function alive(){ return gen === _batchGen && !(signal && signal.aborted); }
    try {
      if (prev) { try { await prev; } catch(e){} }
      if (gen !== _batchGen) return;
      var targets = _eligibleTargets();
      if (!targets.length) return;
      var idx = 0;
      var quotaExceeded = false;
      async function worker() {
        while (alive() && !quotaExceeded && idx < targets.length) {
          var cur = targets[idx++];
          var url = cur.url;
          try { var hit = await caches.match(url); if (hit) continue; } catch(e) { console.warn(e); }
          if (!alive()) return;
          try {
            var _cf = (window.Connectivity && window.Connectivity.fetch) || fetch;
            var r = await _cf(url, { credentials: 'same-origin', signal: signal || undefined });
            if (!r.ok) continue;
            var cacheClone = r.clone();
            var sizeClone = r.clone();
            if (!alive()) return;
            var dir = cur.dir;
            var c = await caches.open(window.OfflineStore.cacheNameFor(dir));
            if (!alive()) return;
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
    finally {
      if (_activeController === controller) _activeController = null;
      if (doneResolve) doneResolve();
    }
  }

  var _idlePending = false;
  var _scrollTimer = 0;
  var _observersInstalled = false;

  function scheduleThumbPrefetch() {
    // Todo 3: same idle slot also schedules the DOM-side multi hydration
    // (render.js scheduleMultiHydration upgrades leftover single-first tiles,
    // visible-first; no-op when the full render already replaced them).
    if (_idlePending) return;
    _idlePending = true;
    var fn = function () {
      _idlePending = false;
      if (typeof document !== 'undefined' && document.hidden) return;
      if (!_sidebarOpen()) return;
      prefetchThumbs().catch(function (e) { console.warn(e); });
      try { if (window.scheduleMultiHydration) window.scheduleMultiHydration(); } catch (e) { console.warn(e); }
    };
    if (typeof requestIdleCallback === 'function') requestIdleCallback(fn);
    else setTimeout(fn, 0);
  }

  function _onDomMaybeChanged(){
    cancelThumbPrefetch();
    if (typeof document !== 'undefined' && document.hidden) return;
    if (_sidebarOpen()) scheduleThumbPrefetch();
  }
  function _debouncedReschedule(){
    cancelThumbPrefetch();
    // True trailing debounce: reset on every scroll event so the
    // _eligibleTargets rect walk + hydration DOM writes run ~250ms after
    // scrolling settles, never mid-fling (Firefox on low-end Android
    // serializes them into 250-500ms main-thread frames otherwise).
    // The settle path only schedules — it does NOT cancel: a batch that
    // started after the last scroll event already holds fresh targets, so
    // killing it would discard good fetches and re-queue identical work
    // (the next prefetchThumbs() self-cancels on start anyway).
    if (_scrollTimer) clearTimeout(_scrollTimer);
    _scrollTimer = setTimeout(function(){
      _scrollTimer = 0;
      if (typeof document !== 'undefined' && document.hidden) return;
      if (_sidebarOpen()) scheduleThumbPrefetch();
    }, 250);
  }
  function _installObservers(){
    if (_observersInstalled) return;
    _observersInstalled = true;
    try {
      if (typeof MutationObserver === 'function'){
        var mo = new MutationObserver(function(){ _onDomMaybeChanged(); });
        var sb = document.getElementById('sidebar');
        var grid = document.getElementById('tile-grid');
        if (sb) mo.observe(sb, {attributes: true, attributeFilter: ['class']});
        if (grid) mo.observe(grid, {childList: true});
      }
    } catch(e) { console.warn(e); }
    try {
      var sc = _sidebarScroller();
      if (sc) sc.addEventListener('scroll', _debouncedReschedule, {passive: true});
      window.addEventListener('resize', _debouncedReschedule);
      document.addEventListener('visibilitychange', function(){
        if (document.hidden) cancelThumbPrefetch();
        else _onDomMaybeChanged();
      });
    } catch(e) { console.warn(e); }
  }
  try { _installObservers(); } catch(e) { console.warn(e); }

  window.collectThumbPrefetchUrls = collectThumbPrefetchUrls;
  window.prefetchThumbs = prefetchThumbs;
  window.scheduleThumbPrefetch = scheduleThumbPrefetch;
  window.cancelThumbPrefetch = cancelThumbPrefetch;
  window.ThumbPrefetch = { collectThumbPrefetchUrls, prefetchThumbs, scheduleThumbPrefetch, cancelThumbPrefetch, PREFETCH_CONCURRENCY };
})();
