/* bookq search/facets — search, facets, filter chips & bottom sheet.
 * Plain global script: defines window.doSearch, loadFacets, renderFacetOptions,
 * updateFilterChips etc. Depends on window.tree, window.activeFilters etc
 * exposed by app.js composition root.
 */
(function () {
  // State lives in app.js; access via window at call-time.
  function _getActiveFilters() { return window.activeFilters; }
  function _getFacets() { return window.facets; }
  function _setFacets(v) { window.facets = v; }
  function _getFacetGen() { return window.facetLoadGen; }
  function _incFacetGen() { return ++window.facetLoadGen; }
  function _getFacetGenVal() { return window.facetLoadGen; }
  function _getSearchResults() { return window.searchResults; }
  function _setSearchResults(v) { window.searchResults = v; }
  function _getSearchMode() { return window.searchMode; }
  function _setSearchMode(v) { window.searchMode = v; }

  // Single escaper, from offline-core.js (loaded first in index.html).
  var _escapeHtml = window.OfflineCore.escapeHtml;
  function _offlineControlsHtml(path) {
    if (typeof window.offlineControlsHtml === 'function') return window.offlineControlsHtml(path);
    return '';
  }
  function _coverUrl(node, w) {
    if (typeof window._coverUrl === 'function') return window._coverUrl(node, w);
    if (window.resolveCover) return window.resolveCover(node, w);
    return null;
  }
  // Todo 4: search renders share the one-shot ctx (single localStorage parse +
  // single offlineMap snapshot + memoized leafPaths per visible folder).
  // Render-scoped like the browse ctx — rebuilt on every search render, so
  // _downloadingSet/progress/refreshOfflineMap freshness comes for free.
  function _safeProgressAll(){
    try {
      var RP = window.ReadingProgress;
      if (RP && typeof RP.getAllSafe === 'function') return RP.getAllSafe() || {};
      if (RP && typeof RP.getAll === 'function') return RP.getAll() || {};
      var raw = (window.localStorage) ? window.localStorage.getItem('bookq_progress') : null;
      if (!raw) return {};
      return JSON.parse(raw) || {};
    } catch(e){ return {}; }
  }
  function _buildSearchCtx(paths){
    var progressAll = _safeProgressAll();
    var om = window.offlineMap || {};
    var orm = window.offlineRecordMap || new Map();
    var leafMap = {};
    for (var i = 0; i < (paths || []).length; i++){
      var p = paths[i];
      try { leafMap[p] = (window.getLeafPathsFor) ? window.getLeafPathsFor(p) : [p]; }
      catch(e){ leafMap[p] = [p]; }
    }
    return { progressAll: progressAll, offlineMap: om, offlineRecordMap: orm, leafMap: leafMap };
  }
  function _searchOfflineHtml(ctx, path) {
    try {
      if (window.StoreAdapter && typeof window.StoreAdapter.offlineControlsHtmlCtx === 'function')
        return window.StoreAdapter.offlineControlsHtmlCtx(path, ctx);
      if (typeof window.offlineControlsHtmlCtx === 'function')
        return window.offlineControlsHtmlCtx(path, ctx);
    } catch(e){ console.warn(e); }
    return _offlineControlsHtml(path);
  }
  function _buildNodeMap() {
    if (typeof window.buildNodeMap === 'function') return window.buildNodeMap();
    var tree = window.tree || [];
    var m = new Map();
    (function indexTree(nodes){ for(var n of nodes){ m.set(n._fullPath, n); if(n.children) indexTree(n.children);} })(tree);
    return m;
  }

  function _resolveDeps(deps){
    deps = deps || {};
    return { fetch: deps.fetch || (typeof window !== 'undefined' && window.fetch ? window.fetch.bind(window) : fetch) };
  }
  async function loadFacets(path, deps) {
    if(path && typeof path === 'object' && path.fetch){ deps = path; path = deps.path || ""; }
    var _d = _resolveDeps(deps);
    var myGen = _incFacetGen();
    var q = path ? ("?path=" + encodeURIComponent(path)) : "";
    try {
      var res = await _d.fetch("/api/metadata/facets" + q);
      if (myGen !== _getFacetGenVal()) return;
      if (!res.ok) throw new Error("facets HTTP " + res.status);
      _setFacets(await res.json());
      renderFacetOptions();
    } catch(e) { console.warn(e); }
  }

  async function doSearch(deps) {
    var _d = _resolveDeps(deps);
    var searchInput = document.getElementById("search-input");
    var q = searchInput ? searchInput.value.trim() : "";
    if (!q) {
      _setSearchMode(false);
      _setSearchResults([]);
      try { if (window.refreshOfflineMap) await window.refreshOfflineMap(); } catch(e) { console.warn(e); }
      if (window.renderView) window.renderView();
      updateFilterChips();
      return;
    }
    var activeFilters = _getActiveFilters() || {artist:[],tag:[],sort:'relevance'};
    var params = new URLSearchParams({ q });
    if (activeFilters.artist && activeFilters.artist.length) params.set('artist', activeFilters.artist.join(','));
    if (activeFilters.tag && activeFilters.tag.length) params.set('tag', activeFilters.tag.join(','));
    params.set('sort', activeFilters.sort || 'relevance');
    try {
      var res = await _d.fetch('/api/search?' + params.toString());
      if (!res.ok) throw new Error("search HTTP " + res.status);
      var data = await res.json();
      _setSearchResults(data.results || []);
      _setSearchMode(true);
      try { if (window.refreshOfflineMap) await window.refreshOfflineMap(); } catch(e) { console.warn(e); }
      if (window.renderView) window.renderView();
    } catch(e) { console.warn(e); }
  }

  function _bindSearchClicks(selector) {
    var tileGrid = document.getElementById("tile-grid");
    var searchInput = document.getElementById("search-input");
    if (!tileGrid) return;
    if (typeof window._bindSearchClicks === 'function') {
      try { window._bindSearchClicks(selector); return; } catch(e){ console.warn(e); }
    }
    if (tileGrid.__ccSearchDelegated) return;
    tileGrid.__ccSearchDelegated = true;
    tileGrid.addEventListener("click", function(e){
      if (!window.searchMode) return;
      var el = e.target.closest ? e.target.closest('.tile,.item[data-path],.card') : null;
      if (!el || !tileGrid.contains(el)) return;
      if(e.target.closest('.offline-btn') || e.target.closest('.ux-pop')) return;
      var path = el.dataset.path;
      if (!path) return;
      window.currentFolder = path;
      if (window.closeSidebar) window.closeSidebar();
      else {
        var sidebar = document.getElementById("sidebar");
        var viewer = document.getElementById("viewer");
        if (sidebar) sidebar.classList.remove("open");
        if (viewer) viewer.classList.remove("sidebar-open");
      }
      if (window.loadImages) window.loadImages(path);
      if (searchInput) searchInput.value = "";
      _setSearchMode(false);
      _setSearchResults([]);
      clearTimeout(window.searchTimer);
    });
  }

  function renderSearchTiles() {
    var tileGrid = document.getElementById("tile-grid");
    if (!tileGrid) return;
    var searchResults = _getSearchResults() || [];
    var ctx = _buildSearchCtx(searchResults.map(function(r){ return r.folder_path; }));
    var html = "";
    var nodeByPath = _buildNodeMap();
    for (var r of searchResults) {
      var imgHtml;
      var node = nodeByPath.get(r.folder_path);
      var url = node ? _coverUrl(node, 200) : null;
      if (url) imgHtml = '<img class="tile-img" src="' + _escapeHtml(url) + '" loading="lazy" decoding="async" onerror="this.style.display=\'none\'">';
      else imgHtml = '<div class="tile-placeholder">\uD83D\uDCC1</div>';
      var metaHtml = "";
      if (r.has_metadata) {
        var artistBadges = (r.artist || []).slice(0, 2).map(function(a){ return '<span class="metadata-badge artist">' + _escapeHtml(a) + '</span>'; }).join('');
        var tagBadges = (r.tags || []).slice(0, 4).map(function(t){ return '<span class="metadata-badge tag">' + _escapeHtml(t) + '</span>'; }).join('');
        if (artistBadges || tagBadges) metaHtml = '<div class="tile-metadata">' + artistBadges + tagBadges + '</div>';
      }
      var offlineHtml = _searchOfflineHtml(ctx, r.folder_path);
      html += '<div class="tile" data-path="' + _escapeHtml(r.folder_path) + '" tabindex="0" role="button">' + imgHtml + '<div class="tile-name">' + window.BrowseRender.nameHtml(r.folder_name) + '</div>' + metaHtml + offlineHtml + '</div>';
    }
    tileGrid.innerHTML = html || '<div style="color:#555;text-align:center;padding:20px;font-size:13px">一致する結果がありません</div>';
    _bindSearchClicks(".tile");
  }

  function renderSearchList() {
    var tileGrid = document.getElementById("tile-grid");
    if (!tileGrid) return;
    var searchResults = _getSearchResults() || [];
    var ctx = _buildSearchCtx(searchResults.map(function(r){ return r.folder_path; }));
    var html = "";
    for (var r of searchResults) {
      var saved = ctx.progressAll[r.folder_path] || null;
      var progress = window.progressBarHtml ? window.progressBarHtml(saved) : {inlineBarHtml:''};
      var badge = progress.inlineBarHtml || '';
      var metaHtml = "";
      if (r.has_metadata) {
        var badges = [];
        (r.artist || []).slice(0, 2).forEach(function(a){ badges.push('<span class="metadata-badge artist">' + _escapeHtml(a) + '</span>'); });
        (r.tags || []).slice(0, 3).forEach(function(t){ badges.push('<span class="metadata-badge tag">' + _escapeHtml(t) + '</span>'); });
        metaHtml = '<div class="meta">' + badges.join('') + '</div>';
      }
      var offlineHtml = _searchOfflineHtml(ctx, r.folder_path);
      html += '<div class="item folder search-result" data-path="' + _escapeHtml(r.folder_path) + '" data-type="folder" tabindex="0" role="button"><span class="icon">\uD83D\uDCC1</span><span class="name">' + _escapeHtml(r.folder_name) + badge + '</span>' + metaHtml + offlineHtml + '</div>';
    }
    tileGrid.innerHTML = html || '<div class="item" style="color:#555">一致する結果がありません</div>';
    _bindSearchClicks(".item[data-path]");
  }

  function renderSearchCards() {
    var tileGrid = document.getElementById("tile-grid");
    if (!tileGrid) return;
    var searchResults = _getSearchResults() || [];
    var ctx = _buildSearchCtx(searchResults.map(function(r){ return r.folder_path; }));
    var html = "";
    var nodeByPath = _buildNodeMap();
    for (var r of searchResults) {
      var imgHtml;
      var node = nodeByPath.get(r.folder_path);
      var url = node ? _coverUrl(node) : null;
      if (url) imgHtml = '<img class="card-img" src="' + _escapeHtml(url) + '" loading="lazy" decoding="async" onerror="this.style.display=\'none\'">';
      else imgHtml = '<div class="tile-placeholder" style="aspect-ratio:16/9">\uD83D\uDCC1</div>';
      var title = r.title_en || r.title_jp || r.folder_name;
      var metaRows = "";
      if (r.has_metadata) {
        if (r.artist && r.artist.length) metaRows += '<div class="meta-row"><span class="label">\uD83C\uDFA8</span> ' + r.artist.slice(0, 3).map(function(a){return _escapeHtml(a);}).join(', ') + '</div>';
        if (r.tags && r.tags.length) metaRows += '<div class="meta-row"><span class="label">\uD83C\uDFF7\uFE0F</span> ' + r.tags.slice(0, 5).map(function(t){return _escapeHtml(t);}).join(', ') + '</div>';
      }
      var offlineHtml = _searchOfflineHtml(ctx, r.folder_path);
      html += '<div class="card" data-path="' + _escapeHtml(r.folder_path) + '" tabindex="0" role="button">' + imgHtml + '<div class="card-content"><div class="card-title">' + _escapeHtml(title) + '</div><div class="card-meta">' + metaRows + '</div></div>' + offlineHtml + '</div>';
    }
    tileGrid.innerHTML = html || '<div style="color:#555;text-align:center;padding:20px;font-size:14px">一致する結果がありません</div>';
    _bindSearchClicks(".card");
  }

  // ── Filter chips / Facet options ──
  function updateFilterChips() {
    var filterChips = document.getElementById("filter-chips");
    if (!filterChips) return;
    var activeFilters = _getActiveFilters() || {artist:[],tag:[],sort:'relevance'};
    var chips = [];
    for (var artist of (activeFilters.artist||[])) {
      chips.push('<span class="chip"><span class="chip-label">\uD83C\uDFA8' + _escapeHtml(artist) + '</span><button class="chip-remove" data-filter="artist" data-value="' + _escapeHtml(artist) + '" aria-label="フィルタを削除">\u2715</button></span>');
    }
    for (var tag of (activeFilters.tag||[])) {
      chips.push('<span class="chip"><span class="chip-label">\uD83C\uDFF7\uFE0F' + _escapeHtml(tag) + '</span><button class="chip-remove" data-filter="tag" data-value="' + _escapeHtml(tag) + '" aria-label="フィルタを削除">\u2715</button></span>');
    }
    if (chips.length > 0) {
      filterChips.innerHTML = chips.join('');
      filterChips.classList.remove("hidden");
      filterChips.querySelectorAll(".chip-remove").forEach(function(btn){
        btn.addEventListener("click", function(e){
          e.stopPropagation();
          var filter = btn.dataset.filter;
          var value = btn.dataset.value;
          if (filter && value) {
            var af = _getActiveFilters();
            af[filter] = af[filter].filter(function(v){ return v !== value; });
            updateFilterChips();
            clearTimeout(window.searchTimer);
            window.searchTimer = setTimeout(doSearch, 300);
          }
        });
      });
    } else {
      filterChips.classList.add("hidden");
    }
  }

  function renderFacetOptions() {
    var facets = _getFacets() || {artists:[],tags:[]};
    var activeFilters = _getActiveFilters() || {artist:[],tag:[],sort:'relevance'};
    var filterBtn = document.getElementById("filter-btn");
    var artistsContainer = document.querySelector("#facet-artists .facet-chips");
    if (artistsContainer) {
      artistsContainer.innerHTML = (facets.artists || []).map(function(a){
        var active = activeFilters.artist.includes(a) ? ' active' : '';
        return '<span class="chip' + active + '" data-facet="artist" data-value="' + _escapeHtml(a) + '" tabindex="0" role="button" aria-pressed="' + (active ? 'true' : 'false') + '">' + _escapeHtml(a) + '</span>';
      }).join('') || '<span style="color:#555;font-size:12px">No artists</span>';
      artistsContainer.querySelectorAll('.chip').forEach(function(el){
        el.addEventListener('click', function(){
          var val = el.dataset.value; var facet = el.dataset.facet;
          if (!val || !facet) return;
          var idx = activeFilters[facet].indexOf(val);
          if (idx >= 0) activeFilters[facet].splice(idx, 1);
          else activeFilters[facet].push(val);
          el.classList.toggle('active');
          el.setAttribute('aria-pressed', el.classList.contains('active'));
        });
      });
    }
    var tagsContainer = document.querySelector("#facet-tags .facet-chips");
    if (tagsContainer) {
      tagsContainer.innerHTML = (facets.tags || []).map(function(t){
        var active = activeFilters.tag.includes(t) ? ' active' : '';
        return '<span class="chip' + active + '" data-facet="tag" data-value="' + _escapeHtml(t) + '" tabindex="0" role="button" aria-pressed="' + (active ? 'true' : 'false') + '">' + _escapeHtml(t) + '</span>';
      }).join('') || '<span style="color:#555;font-size:12px">No tags</span>';
      tagsContainer.querySelectorAll('.chip').forEach(function(el){
        el.addEventListener('click', function(){
          var val = el.dataset.value; var facet = el.dataset.facet;
          if (!val || !facet) return;
          var idx = activeFilters[facet].indexOf(val);
          if (idx >= 0) activeFilters[facet].splice(idx, 1);
          else activeFilters[facet].push(val);
          el.classList.toggle('active');
          el.setAttribute('aria-pressed', el.classList.contains('active'));
        });
      });
    }
    var sortRadios = document.querySelectorAll('input[name="sort"]');
    sortRadios.forEach(function(radio){ radio.checked = (radio.value === activeFilters.sort); });
    if (filterBtn) filterBtn.classList.toggle('active', activeFilters.artist.length > 0 || activeFilters.tag.length > 0);
  }

  // Bottom sheet open/close — called by app.js wiring (needs sidebar etc)
  function openFilterSheet() {
    document.querySelectorAll(".facet-chips").forEach(function(c){ c.innerHTML = ""; });
    var loadFacetsFn = window.loadFacets || loadFacets;
    var searchMode = window.searchMode;
    var currentBrowsePath = window.currentBrowsePath;
    loadFacetsFn(searchMode ? "" : currentBrowsePath);
    var bottomSheet = document.getElementById("bottom-sheet");
    var sheetOverlay = document.getElementById("bottom-sheet-overlay");
    var sidebar = document.getElementById("sidebar");
    var setInert = window.setInert || function(el,h){ if(h) el.setAttribute("inert",""); else el.removeAttribute("inert"); };
    if (bottomSheet) setInert(bottomSheet, false);
    if (sidebar) setInert(sidebar, true);
    if (sheetOverlay) { sheetOverlay.classList.remove("hidden"); sheetOverlay.classList.add("visible"); }
    if (bottomSheet) {
      bottomSheet.classList.remove("hidden");
      requestAnimationFrame(function(){
        bottomSheet.classList.add("open");
        var closeBtn = document.getElementById("bottom-sheet-close");
        if (closeBtn) closeBtn.focus();
      });
    }
  }
  function closeFilterSheet() {
    var bottomSheet = document.getElementById("bottom-sheet");
    var sheetOverlay = document.getElementById("bottom-sheet-overlay");
    var filterBtn = document.getElementById("filter-btn");
    var setInert = window.setInert || function(el,h){ if(h) el.setAttribute("inert",""); else el.removeAttribute("inert"); };
    var syncSidebarA11y = window.syncSidebarA11y || function(){};
    if (bottomSheet) bottomSheet.classList.remove("open");
    if (bottomSheet) setInert(bottomSheet, true);
    syncSidebarA11y();
    if (filterBtn) filterBtn.focus();
    if (sheetOverlay) sheetOverlay.classList.remove("visible");
    setTimeout(function(){
      if (bottomSheet) bottomSheet.classList.add("hidden");
      if (sheetOverlay) sheetOverlay.classList.add("hidden");
    }, 350);
  }

  window.loadFacets = loadFacets;
  window.doSearch = doSearch;
  window.renderFacetOptions = renderFacetOptions;
  window.updateFilterChips = updateFilterChips;
  window.renderSearchTiles = renderSearchTiles;
  window.renderSearchList = renderSearchList;
  window.renderSearchCards = renderSearchCards;
  window.openFilterSheet = openFilterSheet;
  window.closeFilterSheet = closeFilterSheet;
  window.SearchFacets = { loadFacets, doSearch, renderFacetOptions, updateFilterChips, renderSearchTiles, renderSearchList, renderSearchCards, openFilterSheet, closeFilterSheet };
})();
