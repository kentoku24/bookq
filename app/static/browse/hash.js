/* bookq browse/hash — hash routing & URL state.
 * Plain global script: defines window.parseViewerHash, viewerHash,
 * parseBrowseHash, browseHash, currentViewerState, urlStack etc,
 * and the SINGLE hash listener (applyUrlState).
 * Load before app.js; app.js exposes mutable state via window getters.
 */
(function () {
  var VIEWER_HASH_PREFIX = "#/view/";
  var BROWSE_HASH_PREFIX = "#/browse/";

  // These are mirrored from app.js IIFE closure via window getters.
  // Access through window.* at call-time so ordering (this loads before app.js)
  // does not matter — lookups happen when functions are invoked.
  function _getTree() {
    if (typeof window.tree !== 'undefined') return window.tree;
    if (window.__BOOKQ_DEBUG && window.__BOOKQ_DEBUG.getTree) {
      try { return window.__BOOKQ_DEBUG.getTree(); } catch(e) { console.warn(e); }
    }
    return [];
  }
  function _getCurrentFolder() { return window.currentFolder; }
  function _getImages() { return window.images || []; }
  function _getChapterList() { return window.chapterList || []; }
  function _getCurrentBrowsePath() { return window.currentBrowsePath; }
  function _getTreeLoaded() { return window.treeLoaded; }

  function parseViewerHash() {
    var raw = location.hash;
    if (!raw.startsWith(VIEWER_HASH_PREFIX)) return null;
    var rest = raw.slice(VIEWER_HASH_PREFIX.length);
    var q = rest.indexOf("?");
    var rawPath = q === -1 ? rest : rest.slice(0, q);
    var folder;
    try { folder = decodeURIComponent(rawPath); } catch { return null; }
    var query = q === -1 ? "" : rest.slice(q + 1);
    var m = query.match(/(?:^|&)p=(\d+)/);
    var ms = query.match(/(?:^|&)s=([12])/);
    var mo = query.match(/(?:^|&)o=([01])/);
    return {
      folder,
      page: m ? parseInt(m[1], 10) : 0,
      s: ms ? parseInt(ms[1], 10) : null,
      o: mo ? parseInt(mo[1], 10) : null,
    };
  }

  // 既定値（横持ち 2P、ずらし無し）は書かない。1P では o は無意味なので落とす。
  function viewerHash(folder, page, layout) {
    var enc = folder.split("/").map(encodeURIComponent).join("/");
    var h = VIEWER_HASH_PREFIX + enc + "?p=" + page;
    if (layout) {
      if (layout.landscapeSpread === false) h += "&s=1";
      else if (layout.spreadOffset === 1) h += "&o=1";
    }
    return h;
  }

  function parseBrowseHash() {
    var raw = location.hash;
    if (!raw.startsWith(BROWSE_HASH_PREFIX)) return null;
    var rest = raw.slice(BROWSE_HASH_PREFIX.length);
    var q = rest.indexOf("?");
    var rawPath = q === -1 ? rest : rest.slice(0, q);
    var path;
    try { path = decodeURIComponent(rawPath); } catch { return { path: "", v: null }; }
    // NOTE: asymmetric with parseViewerHash (returns null): malformed browse
    // falls back to root via restoreBrowse("") per test_malformed_browse_hash_falls_back_to_root.
    var query = q === -1 ? "" : rest.slice(q + 1);
    var mv = query.match(/(?:^|&)v=(list|card)(?:$|&)/);
    return { path, v: mv ? mv[1] : null };
  }

  // 既定 tile は書かない。ルートで tile なら "" （従来どおりハッシュ無し）。
  function browseHash(path, viewMode) {
    var p = (path === null || path === undefined) ? "" : path;
    var suffix = (viewMode === "list" || viewMode === "card") ? "?v=" + viewMode : "";
    if (p === "" && suffix === "") return "";
    return BROWSE_HASH_PREFIX + p.split("/").map(encodeURIComponent).join("/") + suffix;
  }

  // ── helpers that need tree — use window proxies at call time ──
  function _findNodeByPath(nodes, targetPath) {
    for (var node of nodes) {
      if (node._fullPath === targetPath) return node;
      if (node.children && targetPath.startsWith(node._fullPath + "/")) {
        var found = _findNodeByPath(node.children, targetPath);
        if (found) return found;
      }
    }
    return null;
  }
  function _getNodeAt(path) {
    if (!path) return null;
    // Prefer window.getNodeAt if app.js has exposed it (canonical tree walk)
    if (typeof window.getNodeAt === 'function' && window.getNodeAt !== _getNodeAt) {
      try { return window.getNodeAt(path); } catch(e) { console.warn(e); }
    }
    var t = _getTree();
    return _findNodeByPath(t, path);
  }
  function _findParentPath(nodes, targetPath, parentPath) {
    for (var node of nodes) {
      if (node._fullPath === targetPath) return parentPath;
      if (node.children && targetPath.startsWith(node._fullPath + "/")) {
        var found = _findParentPath(node.children, targetPath, node._fullPath);
        if (found !== null) return found;
      }
    }
    return null;
  }
  function _getParentPath(path) {
    if (!path) return null;
    if (typeof window.getParentPath === 'function' && window.getParentPath !== _getParentPath) {
      try { return window.getParentPath(path); } catch(e) { console.warn(e); }
    }
    var t = _getTree();
    return _findParentPath(t, path, null);
  }

  function _getCurrentChapterInfo(globalIdx) {
    if (typeof window.getCurrentChapterInfo === 'function') {
      try { return window.getCurrentChapterInfo(globalIdx); } catch(e) { console.warn(e); }
    }
    var chapterBoundaries = window.chapterBoundaries || [];
    var chapterList = _getChapterList();
    var chapterIdx = 0;
    for (var i = 1; i < chapterBoundaries.length; i++) {
      if (globalIdx >= chapterBoundaries[i]) chapterIdx = i;
      else break;
    }
    var chapter = chapterList[chapterIdx];
    var localIdx = globalIdx - (chapterBoundaries[chapterIdx] || 0);
    return { chapterIdx, chapterName: chapter ? chapter.name : "", localIdx };
  }

  function currentViewerState() {
    var currentFolder = _getCurrentFolder();
    var images = _getImages();
    var chapterList = _getChapterList();
    if (!currentFolder || !images.length || !chapterList.length) return null;
    var currentIdx = (typeof window.currentIdx !== 'undefined') ? window.currentIdx : 0;
    var info = _getCurrentChapterInfo(currentIdx);
    var chapter = chapterList[info.chapterIdx];
    if (!chapter) return null;
    return {
      folder: chapter.fullPath, page: info.localIdx,
      landscapeSpread: window.landscapeSpread !== false,
      spreadOffset: window.spreadOffset === 1 ? 1 : 0,
    };
  }

  function pushViewerState() {
    var st = currentViewerState();
    if (!st) return;
    var target = location.pathname + location.search + viewerHash(st.folder, st.page, st);
    if (location.pathname + location.search + location.hash === target) return;
    history.pushState(null, "", target);
    urlStackPush(target);
  }

  function replaceViewerState() {
    var st = currentViewerState();
    if (!st) return;
    var target = location.pathname + location.search + viewerHash(st.folder, st.page, st);
    if (location.pathname + location.search + location.hash === target) return;
    history.replaceState(null, "", target);
    urlStackReplace(target);
  }

  var _urlSyncTimer = null;
  function scheduleUrlSync() {
    clearTimeout(_urlSyncTimer);
    _urlSyncTimer = setTimeout(replaceViewerState, 200);
  }

  function clearViewerState() {
    if (location.hash || location.href.endsWith("#")) {
      var target = location.pathname + location.search;
      history.replaceState(null, "", target);
      urlStackReplace(target);
    }
  }

  // ── browse history stack ──
  var urlStack = [];
  var urlStackPos = -1;
  function urlStackPush(target) {
    urlStack.length = urlStackPos + 1;
    urlStack.push(target);
    urlStackPos = urlStack.length - 1;
  }
  function urlStackReplace(target) {
    if (urlStackPos >= 0 && urlStackPos < urlStack.length) urlStack[urlStackPos] = target;
  }
  function reconcileUrlStack() {
    var full = location.pathname + location.search + location.hash;
    var idx = urlStack.indexOf(full);
    if (idx !== -1) { urlStackPos = idx; }
    else { urlStack = [full]; urlStackPos = 0; }
  }
  // Path-only key for urlStack ancestor matching: a plain "?v=..." string
  // compare breaks once the view mode differs between when an entry was
  // pushed and now (e.g. root visited in tile mode → "/", later popped to
  // from list mode → target "/#/browse/?v=list" — same tree level, different
  // query, and root's bare "no hash" form doesn't even share the "#/browse/"
  // prefix). Normalize both the query and the bare-root/#/browse/ asymmetry
  // away so ancestor lookups key on tree path alone. Non-browse (viewer)
  // entries never collide with a browse key — see test_breadcrumb_back_after_mode_change_uses_history.
  function _browseStackKey(u) {
    var hashIdx = u.indexOf("#");
    var hash = hashIdx === -1 ? "" : u.slice(hashIdx);
    if (hash === "") return "path:"; // canonical bare root
    if (hash.indexOf(BROWSE_HASH_PREFIX) !== 0) return "other:" + hash; // e.g. #/view/... — never a browse ancestor
    var rest = hash.slice(BROWSE_HASH_PREFIX.length);
    var q = rest.indexOf("?");
    var rawPath = q === -1 ? rest : rest.slice(0, q);
    return "path:" + rawPath;
  }
  function pushBrowseUrl(target) {
    var current = location.pathname + location.search + location.hash;
    if (target === current) return;
    // Ancestor (ↄ breadcrumb "up") matching only looks BACKWARD (i <
    // urlStackPos) — a forward entry (from a prior Back) can carry a stale
    // view mode from before it was superseded (e.g. tapping "series" again
    // from root(tile) after having visited it in list mode used to
    // history.go(+1) onto that old "?v=list" entry and silently flip the
    // mode). Forward entries are never key-matched, only pushed fresh.
    var key = _browseStackKey(target);
    var idx = -1;
    for (var i = 0; i < urlStackPos; i++) {
      if (_browseStackKey(urlStack[i]) === key) { idx = i; break; }
    }
    if (idx !== -1) { history.go(idx - urlStackPos); urlStackPos = idx; return; }
    history.pushState(null, "", target);
    urlStackPush(target);
  }
  function writeBrowseStateWith(path) {
    pushBrowseUrl(location.pathname + location.search + browseHash(path, window.viewMode));
  }
  function writeBrowseState() {
    var cur = _getCurrentBrowsePath();
    // currentBrowsePath is window.currentBrowsePath — read via getter
    if (typeof window.currentBrowsePath !== 'undefined') cur = window.currentBrowsePath;
    writeBrowseStateWith(cur);
  }

  // ── view-mode-only URL update: replaceState, not pushState ──
  // pushBrowseUrl()/writeBrowseState() push a history entry, which is right
  // for folder navigation (Back should return to the previous folder) but
  // wrong for a pure view-mode toggle (Back must not step through
  // tile/list/card one at a time — same contract as replaceViewerState()
  // for s/o in the viewer, see test_layout_change_uses_replace_state_not_push).
  function replaceBrowseUrl(target) {
    var current = location.pathname + location.search + location.hash;
    if (target === current) return;
    history.replaceState(null, "", target);
    urlStackReplace(target);
  }
  function writeBrowseStateWithReplace(path) {
    replaceBrowseUrl(location.pathname + location.search + browseHash(path, window.viewMode));
  }
  function writeBrowseStateReplace() {
    var cur = _getCurrentBrowsePath();
    if (typeof window.currentBrowsePath !== 'undefined') cur = window.currentBrowsePath;
    writeBrowseStateWithReplace(cur);
  }

  // ── URL state flags (proxied) ──
  // urlRestoring / urlRestoreGen / urlSyncPaused / treeLoaded live in app.js;
  // we expose setters that write through window when possible.
  function _setUrlRestoring(v) { try { window.urlRestoring = v; } catch(e) { console.warn(e); } }
  function _getUrlRestoring() { return window.urlRestoring; }
  function _incUrlRestoreGen() {
    if (typeof window.urlRestoreGen !== 'undefined') return ++window.urlRestoreGen;
    return 0;
  }
  function _getUrlRestoreGen() { return window.urlRestoreGen; }

  // Tear down helpers — need resetStrip / teardownViewer which live in spread/viewer.
  // Call through window if available, else no-op (fallback defined below).
  function _resetStrip() {
    if (typeof window.resetStrip === 'function') return window.resetStrip();
    // minimal fallback if viewer not yet loaded
    var track = document.getElementById("img-track");
    if (track) { track.style.transition = "none"; track.style.transform = "translateX(0)"; }
  }
  function _teardownViewer() {
    try { if (window.Overview) window.Overview.close(); if (window.Zoom) window.Zoom.reset(false); } catch (e) { console.warn(e); }
    if (typeof window.teardownViewer === 'function') return window.teardownViewer();
    var track = document.getElementById("img-track");
    if (track) { track.style.transition = "none"; track.style.transform = "translateX(0)"; }
    try { window.currentFolder = null; window.images = []; window.chapterList = []; window.chapterBoundaries = []; window.currentIdx = 0; } catch(e) { console.warn(e); }
    var folderName = document.getElementById("folder-name");
    var counter = document.getElementById("counter");
    var pageSliderWrap = document.getElementById("page-slider-wrap");
    var imgContainer = document.getElementById("img-container");
    var spinner = document.getElementById("spinner");
    if (folderName) folderName.textContent = "";
    if (counter) counter.textContent = "";
    if (pageSliderWrap) pageSliderWrap.classList.remove("visible");
    if (imgContainer) imgContainer.classList.remove("loading");
    if (spinner) spinner.style.display = "none";
    var sidebar = document.getElementById("sidebar");
    var viewer = document.getElementById("viewer");
    if (sidebar) sidebar.classList.add("open");
    if (viewer) viewer.classList.add("sidebar-open");
    if (typeof window.syncSidebarA11y === 'function') try { window.syncSidebarA11y(); } catch(e) { console.warn(e); }
  }

  function resetToTop() {
    // ルート（作品一覧）はアンカーを使わない: 常に上端。ビューア復帰の文脈も切る。
    try { if (window.BrowseScroll) window.BrowseScroll.clearViewerReturn(); } catch(e) { console.warn(e); }
    _teardownViewer();
    var empty = document.getElementById("empty");
    if (empty) { empty.innerHTML = "<p>\uD83D\uDCC2 サイドバーからフォルダを選んでください</p>"; empty.style.display = "flex"; }
    try { window.currentBrowsePath = null; } catch(e) { console.warn(e); }
    if (typeof window.updateBrowseHeader === 'function') try { window.updateBrowseHeader(); } catch(e) { console.warn(e); }
    if (typeof window.renderView === 'function') try { window.renderView({ scrollAnchor: null }); } catch(e) { console.warn(e); }
    clearViewerState();
  }

  function restoreBrowse(path) {
    // ビューア復帰の章は teardown 前の live 状態から確定する（URL の p= は 200ms
    // デバウンスで古いので使わない）。☰ 復帰はこの関数が 2 回走る
    // （returnToBrowse と、pushBrowseUrl の history.go(-1) 後の hashchange）ため、
    // BrowseScroll 側にマーカーとして残し、2 回目も同じ位置を返せるようにする。
    var BS = window.BrowseScroll;
    if (BS && path) {
      try {
        var vst = currentViewerState();
        if (vst && vst.folder) BS.noteViewerReturn(path, vst.folder);
      } catch(e) { console.warn(e); }
    }
    // Bump token first so in-flight loadImages cannot overwrite
    try { ++window.urlRestoreGen; window.urlRestoring = false; } catch(e) { console.warn(e); }
    _teardownViewer();
    var node = path ? _getNodeAt(path) : null;
    var dead = !!path && !node; // a path was requested but doesn't resolve — dead/nonexistent folder
    try { window.currentBrowsePath = (path && node) ? path : null; } catch(e) { console.warn(e); }
    if (typeof window.updateBrowseHeader === 'function') try { window.updateBrowseHeader(); } catch(e) { console.warn(e); }
    // Clear search
    try { window.searchMode = false; } catch(e) { console.warn(e); }
    var si = document.getElementById("search-input");
    if (si) si.value = "";
    // 位置は BrowseScroll.decide が一元管理する: 保存位置（従来の rAF 復元）か
    // アンカー（ビューア復帰 / 最大既読 / 上端）のどちらか一方だけを書く。
    var savedTop;
    try {
      var getScrollKey = window.getScrollKey;
      var scrollPositions = window.scrollPositions;
      if (getScrollKey && scrollPositions) {
        var key = getScrollKey(window.currentBrowsePath);
        if (scrollPositions[key] !== undefined) savedTop = scrollPositions[key];
      }
    } catch(e) { console.warn(e); }
    var decision = null;
    if (BS) {
      try {
        decision = BS.decide({
          level: window.currentBrowsePath || null,
          cause: 'restore',
          hasSaved: savedTop !== undefined,
        });
      } catch(e) { console.warn(e); }
    }
    if (typeof window.renderView === 'function') {
      try {
        window.renderView(
          (decision && decision.via !== 'saved') ? { scrollAnchor: decision.anchor } : undefined
        );
      } catch(e) { console.warn(e); }
    }
    if (savedTop !== undefined && (!decision || decision.via === 'saved')) {
      requestAnimationFrame(function () {
        var ss = document.getElementById("sidebar-scroll");
        if (ss) ss.scrollTop = savedTop;
      });
    }
    // Clear the hash back to canonical root when: (a) a dead/nonexistent
    // folder was requested — CLAUDE.md "a dead or empty folder clears the
    // hash" is unconditional, view mode notwithstanding (a bogus
    // "?v=list" must not survive); or (b) we're genuinely at root in tile
    // mode, where the bare URL IS the canonical form. Root in list/card mode
    // keeps its "#/browse/?v=..." hash — that's not dead, it's the real URL.
    if (!window.currentBrowsePath && location.hash.startsWith(BROWSE_HASH_PREFIX) &&
        (dead || window.viewMode === "tile")) {
      history.replaceState(null, "", location.pathname + location.search);
      urlStackReplace(location.pathname + location.search);
    }
  }

  function returnToBrowse() {
    var level = window.currentBrowsePath || (window.currentFolder ? _getParentPath(window.currentFolder) : null);
    var node = level ? _getNodeAt(level) : null;
    restoreBrowse((level && node) ? level : null);
    writeBrowseState();
  }

  function applyUrlState() {
    reconcileUrlStack();
    var st = parseViewerHash();
    var bs = parseBrowseHash();
    var cur = currentViewerState();
    if (!window.treeLoaded) return;

    // The URL is the source of truth for view mode: an explicit v=list|card
    // sets it, and its absence — including the bare/no-hash canonical root,
    // which is only ever written for root+tile (browseHash("", "tile") ===
    // "") — resets it to the default tile. Done unconditionally here (not
    // folded into the branches below) because a Back/Forward or
    // pushBrowseUrl's history.go() can land on a URL that looks like a no-op
    // to the branches below (currentBrowsePath already matches, no viewer
    // state) while still needing the mode corrected — see
    // test_breadcrumb_back_after_mode_change_uses_history /
    // test_browse_url_without_v_resets_to_tile.
    var modeChanged = false;
    if (!st) {
      var nextMode = (bs && bs.v) ? bs.v : "tile";
      if (window.viewMode !== nextMode) {
        window.viewMode = nextMode;
        if (window.updateFooterBtn) window.updateFooterBtn();
        modeChanged = true;
      }
    }

    if (st && (!cur || st.folder !== cur.folder || st.page !== cur.page)) {
      try { window.urlRestoring = true; } catch(e) { console.warn(e); }
      if (typeof window.loadImages === 'function') window.loadImages(st.folder);
    } else if (bs !== null) {
      restoreBrowse(bs.path);
    } else if (!st && (cur || window.currentFolder || window.currentBrowsePath)) {
      try { ++window.urlRestoreGen; window.urlRestoring = false; } catch(e) { console.warn(e); }
      resetToTop();
    } else if (modeChanged && bs === null && typeof window.renderView === 'function') {
      // Bare-root arrival (e.g. breadcrumb ↩ dedup'd onto a pre-toggle
      // history entry via history.go()) whose synchronous caller already
      // rendered and updated state BEFORE this async hashchange fired, so
      // none of the branches above see anything to do — but viewMode just
      // changed above, and nothing re-rendered the grid for it. A second
      // render here is cheap; see test_breadcrumb_back_after_mode_change_uses_history.
      try { window.renderView(); } catch(e) { console.warn(e); }
    }
  }

  // ── SINGLE hash listener (MUST be only one in codebase) ──
  window.addEventListener("hashchange", applyUrlState);

  // Expose
  window.parseViewerHash = parseViewerHash;
  window.viewerHash = viewerHash;
  window.parseBrowseHash = parseBrowseHash;
  window.browseHash = browseHash;
  window.currentViewerState = currentViewerState;
  window.pushViewerState = pushViewerState;
  window.replaceViewerState = replaceViewerState;
  window.scheduleUrlSync = scheduleUrlSync;
  window.clearViewerState = clearViewerState;
  window.applyUrlState = applyUrlState;
  window.restoreBrowse = restoreBrowse;
  window.resetToTop = resetToTop;
  window.returnToBrowse = returnToBrowse;
  window.reconcileUrlStack = reconcileUrlStack;
  window.writeBrowseState = writeBrowseState;
  window.writeBrowseStateWith = writeBrowseStateWith;
  window.writeBrowseStateReplace = writeBrowseStateReplace;
  window.writeBrowseStateWithReplace = writeBrowseStateWithReplace;
  window.pushBrowseUrl = pushBrowseUrl;
  window.VIEWER_HASH_PREFIX = VIEWER_HASH_PREFIX;
  window.BROWSE_HASH_PREFIX = BROWSE_HASH_PREFIX;
  // urlStack accessors
  Object.defineProperty(window, 'urlStack', {
    get: function () { return urlStack; },
    set: function (v) { urlStack = v; },
    configurable: true
  });
  Object.defineProperty(window, 'urlStackPos', {
    get: function () { return urlStackPos; },
    set: function (v) { urlStackPos = v; },
    configurable: true
  });
  window.urlStackPush = urlStackPush;
  window.urlStackReplace = urlStackReplace;
  window.HashRouter = {
    parseViewerHash, viewerHash, parseBrowseHash, browseHash,
    currentViewerState, pushViewerState, replaceViewerState,
    scheduleUrlSync, clearViewerState, applyUrlState,
    restoreBrowse, resetToTop, returnToBrowse,
    reconcileUrlStack, writeBrowseState, writeBrowseStateWith,
    writeBrowseStateReplace, writeBrowseStateWithReplace
  };
})();