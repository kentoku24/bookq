/* bookq browse/render — browse tile/list/card & search renders.
 * Plain global script. Delegates to window.* helpers at call-time.
 */
(function () {
  // escapeHtml / folderHasOnlyImages / encodePath live in offline-core.js,
  // which index.html loads first — no local copy, no fallback body.
  var _escapeHtml = window.OfflineCore.escapeHtml;
  var _folderHasOnlyImages = window.OfflineCore.folderHasOnlyImages;
  var _folderHasImages = window.OfflineCore.folderHasImages;
  var _readChildLabel = "このフォルダを読む";
  function _offlineControlsHtml(p){ var fn=window.offlineControlsHtml; return fn?fn(p):''; }
  function _progressBarHtml(saved){ var fn=window.progressBarHtml; return fn?fn(saved):{barHtml:'',inlineBarHtml:''}; }
  function _coverUrl(node){
    if (window._coverUrl) return window._coverUrl(node);
    if (window.resolveCover) return window.resolveCover(node);
    return null;
  }
  function _getTileGrid(){ return document.getElementById("tile-grid"); }

  function splitDisplayName(name) {
    var s = String(name || "");
    var i = s.lastIndexOf("/");
    if (i === -1) return { leaf: s, parent: "" };
    return { leaf: s.slice(i + 1), parent: s.slice(0, i) };
  }
  function nameHtml(name) {
    var d = splitDisplayName(name);
    var h = '<span class="tile-name-text">' + _escapeHtml(d.leaf) + '</span>';
    if (d.parent) h += '<span class="tile-name-parent">' + _escapeHtml(d.parent) + '</span>';
    return h;
  }

  function multiThumbSpec(count){
    return {
      2: { rows: [2], cols: 2 },
      3: { rows: [1,2], cols: 2 },
      4: { rows: [2,2], cols: 2 },
      5: { rows: [2,3], cols: 6 },
      6: { rows: [2,2,2], cols: 2 },
      7: { rows: [2,3,2], cols: 6 },
      8: { rows: [3,2,3], cols: 6 },
      9: { rows: [3,3,3], cols: 3 },
    }[Math.min(count, 9)] || null;
  }
  function _tilePlaceholder(){ return '<div class="tile-placeholder">\uD83D\uDCC1</div>'; }
  function _cardPlaceholder(){ return '<div class="tile-placeholder" style="aspect-ratio:16/9">\uD83D\uDCC1</div>'; }
  var _encodePath = window.OfflineCore.encodePath;
  function _buildNodeMap(){
    if (window.buildNodeMap) return window.buildNodeMap();
    var tree = window.tree || [];
    var m=new Map();
    (function idx(nodes){ for(var n of nodes){ m.set(n._fullPath,n); if(n.children) idx(n.children); } })(tree);
    return m;
  }
  function _getBrowseChildren(){ return window.getBrowseChildren ? window.getBrowseChildren() : []; }
  function _getCurrentFolder(){ return window.currentFolder; }
  function _getCurrentBrowsePath(){ try { return window.currentBrowsePath || null; } catch(e){ return null; } }
  function _emptyBrowseMessage(){ return window.emptyBrowseMessage ? window.emptyBrowseMessage() : "フォルダが見つかりません"; }

  // Todo 4 one-shot render ctx: ONE localStorage parse + ONE offlineMap snapshot
  // + memoized leafPaths per visible folder, built once per render call.
  // Per-tile code does map lookups only (no per-tile getPartialProgress walks,
  // no per-tile ReadingProgress.get parses). Corrupt localStorage degrades to
  // an empty map so the grid never falls (oracle#9). The ctx is render-scoped
  // (never cached across renders): _downloadingSet/progress events and
  // refreshOfflineMap resolutions all funnel through renderView, which rebuilds
  // it — mid-download renders re-fetch the live maps.
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
  function _buildRenderCtx(nodes){
    var list = nodes || [];
    var progressAll = _safeProgressAll();
    var om = window.offlineMap || {};
    var orm = window.offlineRecordMap || new Map();
    var leafMap = {};
    var hasOnlyMap = {};
    for (var i = 0; i < list.length; i++){
      var n = list[i];
      var p = n._fullPath;
      try {
        leafMap[p] = (window.getLeafPathsFor) ? window.getLeafPathsFor(p) : [p];
      } catch(e){ leafMap[p] = [p]; }
      try { hasOnlyMap[p] = !!_folderHasOnlyImages(n); } catch(e){ hasOnlyMap[p] = false; }
    }
    var ctx = { progressAll: progressAll, offlineMap: om, offlineRecordMap: orm, leafMap: leafMap };
    try { window.__cc_renderCtx = ctx; window.__cc_hasOnlyMap = hasOnlyMap; } catch(e) { console.warn(e); }
    return ctx;
  }
  function _progressFor(ctx, path){
    var saved = (ctx.progressAll && ctx.progressAll[path]) || null;
    return _progressBarHtml(saved);
  }
  function _offlineHtmlFor(ctx, path){
    try {
      if (window.StoreAdapter && typeof window.StoreAdapter.offlineControlsHtmlCtx === 'function')
        return window.StoreAdapter.offlineControlsHtmlCtx(path, ctx);
      if (typeof window.offlineControlsHtmlCtx === 'function')
        return window.offlineControlsHtmlCtx(path, ctx);
    } catch(e){ console.warn(e); }
    return _offlineControlsHtml(path);
  }
  // Parent-level images-tiles: one pseudo tile per MIXED child of the current
  // browse level, each opening that child's own loose images as a single
  // chapter. Folders render first, then images-tiles (order fixed here).
  // Render-layer only — never mutates the tree or the node map.
  function _readChildNodes(folders){
    var out = [];
    var list = folders || [];
    for (var i = 0; i < list.length; i++){
      var n = list[i];
      try { if (_folderHasImages(n) && !_folderHasOnlyImages(n)) out.push(n); } catch(e){}
    }
    return out;
  }
  // Root exception: root-level loose images have no parent level, so root
  // browsing alone shows ONE self tile (data-read-self, same label) for them.
  function _rootHasLooseImages(){
    if (_getCurrentBrowsePath()) return false;
    var roots = [];
    try { roots = window.tree || []; } catch(e){ roots = []; }
    for (var j = 0; j < roots.length; j++){
      if (roots[j] && roots[j].type === "image") return true;
    }
    return false;
  }
  // Delegated browse-click fallback: a single #tile-grid listener using
  // closest(), shared by tiles/list/cards. Prefers the app.js binder when
  // present; the hasOnly verdict comes from the render-time map with a live
  // fallback. Parent-level images-tiles (data-read-child / data-read-self)
  // open the viewer directly and never drill; plain folder tiles keep the
  // loadImages-vs-navigateToFolder branching.
  function _browseClickEl(e){
    var el = e.target.closest ? e.target.closest('.tile,.item[data-path],.card') : null;
    var grid = _getTileGrid();
    if (!el || !grid || !grid.contains(el)) return null;
    return el;
  }
  function _handleBrowseClick(e, el){
    if(e.target.closest('.offline-btn')||e.target.closest('.ux-pop')) return;
    var path=el.dataset.path;
    if(el.dataset.readChild==="1"||el.dataset.readSelf==="1"){ window.currentFolder=path; if(window.closeSidebar) window.closeSidebar(); if(window.loadImages) window.loadImages(path); return; }
    var liveNode = window.getNodeAt ? window.getNodeAt(path) : null;
    var hasOnly = false;
    try {
      var m = window.__cc_hasOnlyMap;
      if (liveNode && m && (path in m)) hasOnly = !!m[path];
      else hasOnly = _folderHasOnlyImages(liveNode);
    } catch(err){ console.warn(err); hasOnly = false; }
    if(liveNode && hasOnly){ window.currentFolder=path; if(window.closeSidebar) window.closeSidebar(); if(window.loadImages) window.loadImages(path); }
    else if(window.navigateToFolder) window.navigateToFolder(path);
  }
  function _ensureBrowseDelegated(){
    var grid = _getTileGrid(); if(!grid) return;
    if(grid.__ccBrowseDelegated) return;
    grid.__ccBrowseDelegated = true;
    grid.addEventListener("click", function(e){
      if(window.searchMode) return;
      var el = _browseClickEl(e);
      if(!el) return;
      _handleBrowseClick(e, el);
    });
  }
  function _bindTiles(){
    if(window._bindBrowseClicks){ try { window._bindBrowseClicks(".tile"); return; } catch(e){ console.warn(e); } }
    _ensureBrowseDelegated();
  }
  function _bindList(){
    if(window._bindBrowseClicks){ try { window._bindBrowseClicks(".item[data-path]"); return; } catch(e){ console.warn(e); } }
    _ensureBrowseDelegated();
  }
  function _bindCards(){
    if(window._bindBrowseClicks){ try { window._bindBrowseClicks(".card"); return; } catch(e){ console.warn(e); } }
    _ensureBrowseDelegated();
  }

  // Todo 3 single-cover-first: while window.__cc_singleCoverFirst===true
  // (shallow window; set by app.js restoreTree, consumed here — never
  // re-derived), tiles emit ONLY the single cover at w=200 via
  // window.resolveCoverSingle, with onerror matching resolveCoverHtml.
  // Full mode (flag false) keeps the existing order: multi-grid w=200 →
  // thumb/thumbs[0] single → placeholder. All covers keep loading=lazy.
  function _isSingleCoverFirst(){ try { return window.__cc_singleCoverFirst === true; } catch(e){ return false; } }
  function _singleCoverImg(f){
    var url = window.resolveCoverSingle ? window.resolveCoverSingle(f) : null;
    if (url) return '<img class="tile-img" src="' + _escapeHtml(url) + '" loading="lazy" decoding="async" data-cc-single="1" onerror="this.style.display=\'none\'">';
    return _tilePlaceholder();
  }
  function _isArchiveTile(folderEnc){
    try {
      var leaf = String(folderEnc || "").split("/").pop();
      return /\.(zip|cbz|rar|cbr|epub)$/i.test(decodeURIComponent(leaf));
    } catch(e){ return false; }
  }
  function _multiGridHtml(f, folderEnc){
    var spec = multiThumbSpec(f.thumbs.length);
    if(!spec) return null;
    var n = Math.min(f.thumbs.length, 9);
    var gridCols = spec.cols;
    if(n >= 2 && _isArchiveTile(folderEnc)){
      var names = f.thumbs.slice(0, n).map(encodeURIComponent).join(",");
      var src = "/api/thumb-sprite?folder=" + folderEnc + "&names=" + names + "&w=200";
      var one = '<img src="' + _escapeHtml(src) + '" loading="lazy" decoding="async">';
      return '<div class="tile-multi" style="grid-template-columns:repeat(' + gridCols + ',1fr);grid-template-rows:repeat(' + spec.rows.length + ',minmax(0,1fr))">' + one + '</div>';
    }
    var imgs = "";
    var idx = 0;
    for (var itemsInRow of spec.rows) {
      var span = gridCols / itemsInRow;
      for (var j = 0; j < itemsInRow && idx < n; j++, idx++) {
        var tp = f.thumbs[idx].split('/').map(encodeURIComponent).join('/');
        imgs += '<img src="/thumb/' + folderEnc + '/' + tp + '?w=200" loading="lazy" decoding="async" style="grid-column:span ' + span + '">';
      }
    }
    return '<div class="tile-multi" style="grid-template-columns:repeat(' + gridCols + ',1fr);grid-template-rows:repeat(' + spec.rows.length + ',minmax(0,1fr))">' + imgs + '</div>';
  }
  function _isInViewport(el){
    try {
      var r = el.getBoundingClientRect();
      var vh = window.innerHeight || document.documentElement.clientHeight;
      var vw = window.innerWidth || document.documentElement.clientWidth;
      return r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw;
    } catch(e){ return true; }
  }
  // Lazy multi hydration (Todo 3): on idle after full arrival, upgrade any
  // remaining single-first tiles (img[data-cc-single]) to the multi-grid,
  // visible-first. No-op when the full render already replaced them (normal
  // path) or while the flag is still true (shallow window / full failed).
  function hydrateMultiCovers(){
    if (_isSingleCoverFirst()) return 0;
    var grid = _getTileGrid(); if (!grid) return 0;
    var imgs = grid.querySelectorAll('img[data-cc-single]');
    if (!imgs.length) return 0;
    var upgraded = 0;
    for (var k = 0; k < imgs.length; k++){
      var img = imgs[k];
      var tile = img.closest ? img.closest('.tile[data-path]') : null;
      if (!tile) continue;
      if (!_isInViewport(tile)) continue;
      var path = tile.getAttribute('data-path');
      var node = window.getNodeAt ? window.getNodeAt(path) : null;
      if (!node || !node.thumbs || node.thumbs.length < 2) { img.removeAttribute('data-cc-single'); continue; }
      var html = _multiGridHtml(node, _encodePath(node._fullPath));
      if (!html) continue;
      var tmp = document.createElement('div'); tmp.innerHTML = html;
      var multi = tmp.firstChild;
      if (multi) { tile.replaceChild(multi, img); upgraded++; }
    }
    return upgraded;
  }
  function scheduleMultiHydration(){
    var fn = function(){ try { hydrateMultiCovers(); } catch(e){ console.warn(e); } };
    if (typeof requestIdleCallback === 'function') requestIdleCallback(fn);
    else setTimeout(fn, 0);
  }
  // Shallow-warmup idle fill poll (plan T4): after the first full
  // /api/folders load, app.js hands the response ETag to noteFullLoad and
  // the poll loop takes over. Each idle tick fetches
  // GET /api/folders?since=<gen> (bare /api/folders while the generation is
  // still unknown) with 5s→30s backoff on unchanged, and feeds a changed
  // tree to onTree — the existing restoreTree path in app.js, never a new
  // render function. thumb=None nodes keep rendering the existing
  // _tilePlaceholder() until the fill lands, so the placeholder→cover
  // transition falls out for free. Ticks skip their fetch while
  // document.hidden (pause) and resume on visible; the single setTimeout
  // chain owns scheduling, so no new global listeners are added.
  // NOTE on server idle interplay (routes.py _background_fill): every poll
  // stamps the server idle clock, so the backoff growth doubles as the
  // quiet window the server-side fill needs — never poll faster than this
  // schedule or the fill starves.
  var FILL_POLL_BASE_MS = 5000;
  var FILL_POLL_MAX_MS = 30000;
  var _fillPoll = { gen: null, etag: null, delay: FILL_POLL_BASE_MS, timer: 0, onTree: null };
  function _fillPollParseGen(etag){
    var m = /-g(\d+)"?\s*$/.exec(String(etag || ""));
    if (!m) return null;
    var n = parseInt(m[1], 10);
    return isFinite(n) ? n : null;
  }
  function _fillPollClear(){
    if (_fillPoll.timer){ try { clearTimeout(_fillPoll.timer); } catch(e){} _fillPoll.timer = 0; }
  }
  function _fillPollSchedule(){
    _fillPollClear();
    var delay = _fillPoll.delay;
    _fillPoll.timer = setTimeout(function(){
      _fillPoll.timer = 0;
      var run = function(){ try { _fillPollTick(); } catch(e){ console.warn(e); _fillPollBackoff(); } };
      try {
        if (typeof requestIdleCallback === "function"){ requestIdleCallback(run); return; }
      } catch(e){}
      run();
    }, delay);
  }
  function _fillPollBackoff(){
    _fillPoll.delay = Math.min(FILL_POLL_MAX_MS, _fillPoll.delay * 2);
    _fillPollSchedule();
  }
  function _fillPollTick(){
    if (!_fillPoll.onTree) return;
    if (typeof document !== "undefined" && document.hidden){ _fillPollSchedule(); return; }
    var url = (_fillPoll.gen === null) ? "/api/folders" : ("/api/folders?since=" + _fillPoll.gen);
    fetch(url, {credentials: "same-origin"}).then(function(res){
      if (res.status === 304 || !res.ok){ _fillPollBackoff(); return null; }
      var etag = null;
      try { etag = res.headers ? res.headers.get("ETag") : null; } catch(e){}
      return res.json().then(function(body){ return {body: body, etag: etag}; });
    }).then(function(out){
      if (!out) return; // 304 / HTTP error already backed off above
      var body = out.body;
      if (body && !Array.isArray(body) && body.tree){
        // Dict-with-generation shape: numeric compare against known gen.
        var gen = body.generation;
        if (gen === _fillPoll.gen){ _fillPollBackoff(); return; }
        if (typeof gen === "number" && isFinite(gen)) _fillPoll.gen = gen;
        if (out.etag) _fillPoll.etag = out.etag;
        _fillPoll.delay = FILL_POLL_BASE_MS;
        try { _fillPoll.onTree(body.tree); } catch(e){ console.warn(e); }
        _fillPollSchedule();
        return;
      }
      if (Array.isArray(body)){
        // Bare-list shape (generation unknown): ETag change means a new tree.
        if (!out.etag){ _fillPollBackoff(); return; } // no signal — never redraw blind
        if (_fillPoll.etag && out.etag === _fillPoll.etag){ _fillPollBackoff(); return; }
        var hadBaseline = !!_fillPoll.etag;
        _fillPoll.etag = out.etag;
        var g = _fillPollParseGen(out.etag);
        if (g !== null) _fillPoll.gen = g;
        if (!hadBaseline){ _fillPoll.delay = FILL_POLL_BASE_MS; _fillPollSchedule(); return; }
        _fillPoll.delay = FILL_POLL_BASE_MS;
        try { _fillPoll.onTree(body); } catch(e){ console.warn(e); }
        _fillPollSchedule();
        return;
      }
      // Unknown shape (e.g. {changed:false}): treat as unchanged.
      _fillPollBackoff();
    }).catch(function(e){ console.warn(e); _fillPollBackoff(); });
  }
  function noteFullLoad(info){
    _fillPollClear();
    _fillPoll.onTree = (info && info.onTree) || null;
    _fillPoll.etag = (info && info.etag) || null;
    _fillPoll.gen = _fillPollParseGen(_fillPoll.etag);
    _fillPoll.delay = FILL_POLL_BASE_MS;
    if (!_fillPoll.onTree) return; // nothing to feed — stay idle, no timer
    _fillPollSchedule();
  }
  function fillPollStop(){ _fillPollClear(); _fillPoll.onTree = null; }
  function fillPollState(){ return {generation: _fillPoll.gen, delayMs: _fillPoll.delay, active: !!_fillPoll.onTree}; }
  function renderTileImg(f, folderEnc){
    if (_isSingleCoverFirst()) return _singleCoverImg(f);
    if (f.thumbs && f.thumbs.length >= 2){
      var multi = _multiGridHtml(f, folderEnc);
      if (multi) return multi;
      var url = _coverUrl(f);
      if(url) return '<img class="tile-img" src="' + _escapeHtml(url) + '" loading="lazy" decoding="async">';
      return _tilePlaceholder();
    }
    var url2 = _coverUrl(f);
    if(url2) return '<img class="tile-img" src="' + _escapeHtml(url2) + '" loading="lazy" decoding="async">';
    return _tilePlaceholder();
  }
  function renderTiles(){
    var folders = _getBrowseChildren();
    var tileGrid=_getTileGrid(); if(!tileGrid) return;
    var readChildren=_readChildNodes(folders);
    var rootLoose=_rootHasLooseImages();
    var ctx=_buildRenderCtx(folders.concat(readChildren));
    var html="";
    for(var f of folders){
      var isActive = f._fullPath === _getCurrentFolder();
      var cls = isActive ? "tile active" : "tile";
      var folderEnc = _encodePath(f._fullPath);
      var progress = _progressFor(ctx, f._fullPath);
      var imgHtml = renderTileImg(f, folderEnc);
      var offlineHtml = _offlineHtmlFor(ctx, f._fullPath);
      html += '<div class="' + cls + '" data-path="' + _escapeHtml(f._fullPath) + '" data-type="folder" tabindex="0" role="button">' + imgHtml + '<div class="tile-name">' + progress.barHtml + nameHtml(f.name) + '</div>' + offlineHtml + '</div>';
    }
    // Parent-level images-tiles come AFTER the folder tiles (folders first,
    // then images-tiles). Cover is the child itself; progress/offline reuse
    // the existing per-path helpers keyed by the child path (folder-wide unit).
    for(var rc of readChildren){
      var cEnc=_encodePath(rc._fullPath);
      var cProgress=_progressFor(ctx, rc._fullPath);
      var cImg=renderTileImg(rc, cEnc);
      var cOffline=_offlineHtmlFor(ctx, rc._fullPath);
      var cCls=rc._fullPath===_getCurrentFolder()?"tile active":"tile";
      html+='<div class="'+cCls+'" data-path="'+_escapeHtml(rc._fullPath)+'" data-read-child="1" data-type="folder" tabindex="0" role="button">'+cImg+'<div class="tile-name">'+cProgress.barHtml+nameHtml(_readChildLabel)+'</div>'+cOffline+'</div>';
    }
    // Root exception: ONE self tile for root-level loose images (no parent
    // level exists). Cover falls back to the placeholder; same label constant.
    if(rootLoose){
      var sProgress=_progressFor(ctx, "");
      var sOffline=_offlineHtmlFor(ctx, "");
      html+='<div class="tile" data-path="" data-read-self="1" data-type="folder" tabindex="0" role="button">'+_tilePlaceholder()+'<div class="tile-name">'+sProgress.barHtml+nameHtml(_readChildLabel)+'</div>'+sOffline+'</div>';
    }
    tileGrid.innerHTML = html || '<div style="color:#555;text-align:center;padding:20px;font-size:13px">' + _emptyBrowseMessage() + '</div>';
    _bindTiles();
    var el=document.getElementById("folder-count"); if(el) el.textContent=(folders.length+readChildren.length+(rootLoose?1:0))+" 件";
  }
  function renderList(){
    var nodes = _getBrowseChildren();
    var tileGrid=_getTileGrid(); if(!tileGrid) return;
    var readChildren=_readChildNodes(nodes);
    var rootLoose=_rootHasLooseImages();
    var ctx=_buildRenderCtx(nodes.concat(readChildren));
    var html="";
    for(var node of nodes){
      var path=node._fullPath;
      var isActive=path===_getCurrentFolder();
      var cls=["item","folder"]; if(isActive) cls.push("active");
      var url=_coverUrl(node);
      var thumbHtml=url?'<img class="thumb" src="'+_escapeHtml(url)+'" loading="lazy" decoding="async">':"";
      var progress=_progressFor(ctx, path);
      var badge=progress.inlineBarHtml;
      html+='<div class="'+cls.join(" ")+'" data-path="'+_escapeHtml(path)+'" data-type="folder" tabindex="0" role="button"><span class="icon">\uD83D\uDCC1</span><span class="name">'+_escapeHtml(node.name)+badge+'</span>'+thumbHtml+_offlineHtmlFor(ctx, path)+'</div>';
    }
    // Parent-level images-tiles come AFTER the folder tiles (folders first,
    // then images-tiles) — list-mode mirror of the tile grid above.
    for(var rcn of readChildren){
      var rcp=rcn._fullPath;
      var rcCls=["item","folder"]; if(rcp===_getCurrentFolder()) rcCls.push("active");
      var rcUrl=_coverUrl(rcn);
      var rcThumb=rcUrl?'<img class="thumb" src="'+_escapeHtml(rcUrl)+'" loading="lazy" decoding="async">':"";
      var rcProgress=_progressFor(ctx, rcp);
      html+='<div class="'+rcCls.join(" ")+'" data-path="'+_escapeHtml(rcp)+'" data-read-child="1" data-type="folder" tabindex="0" role="button"><span class="icon">\uD83D\uDCC1</span><span class="name">'+_escapeHtml(_readChildLabel)+rcProgress.inlineBarHtml+'</span>'+rcThumb+_offlineHtmlFor(ctx, rcp)+'</div>';
    }
    // Root exception: ONE self tile for root-level loose images.
    if(rootLoose){
      var lsProgress=_progressFor(ctx, "");
      html+='<div class="item folder" data-path="" data-read-self="1" data-type="folder" tabindex="0" role="button"><span class="icon">\uD83D\uDCC1</span><span class="name">'+_escapeHtml(_readChildLabel)+lsProgress.inlineBarHtml+'</span>'+_offlineHtmlFor(ctx, "")+'</div>';
    }
    tileGrid.innerHTML=html||'<div class="item" style="color:#555">'+_emptyBrowseMessage()+'</div>';
    _bindList();
    var ec=document.getElementById("folder-count"); if(ec) ec.textContent=(nodes.length+readChildren.length+(rootLoose?1:0))+" 件";
  }
  function renderCards(){
    var folders=_getBrowseChildren();
    var tileGrid=_getTileGrid(); if(!tileGrid) return;
    var readChildren=_readChildNodes(folders);
    var rootLoose=_rootHasLooseImages();
    var ctx=_buildRenderCtx(folders.concat(readChildren));
    var html="";
    for(var f of folders){
      var isActive=f._fullPath===_getCurrentFolder();
      var url=_coverUrl(f);
      var imgHtml;
      if(url) imgHtml='<img class="card-img" src="'+_escapeHtml(url)+'" loading="lazy" decoding="async">';
      else imgHtml=_cardPlaceholder();
      var meta=f.metadata||{};
      var metaRows="";
      if(f.has_metadata && meta){
        if(meta.artist&&meta.artist.length) metaRows+='<div class="meta-row"><span class="label">\uD83C\uDFA8</span> '+meta.artist.slice(0,2).map(function(a){return _escapeHtml(a);}).join(', ')+'</div>';
        if(meta.tags&&meta.tags.length) metaRows+='<div class="meta-row"><span class="label">\uD83C\uDFF7\uFE0F</span> '+meta.tags.slice(0,4).map(function(t){return _escapeHtml(t);}).join(', ')+'</div>';
      }
      var progress=_progressFor(ctx, f._fullPath);
      var offlineHtml=_offlineHtmlFor(ctx, f._fullPath);
      html+='<div class="card '+(isActive?'active':'')+'" data-path="'+_escapeHtml(f._fullPath)+'" tabindex="0" role="button">'+imgHtml+'<div class="card-content"><div class="card-title">'+progress.barHtml+'<span class="card-title-text">'+_escapeHtml(f.name)+'</span></div><div class="card-meta">'+metaRows+'</div></div>'+offlineHtml+'</div>';
    }
    // Parent-level images-tiles come AFTER the folder tiles (folders first,
    // then images-tiles) — card-mode mirror of the tile grid above.
    for(var rcc of readChildren){
      var rccUrl=_coverUrl(rcc);
      var rccImg=rccUrl?'<img class="card-img" src="'+_escapeHtml(rccUrl)+'" loading="lazy" decoding="async">':_cardPlaceholder();
      var rccProgress=_progressFor(ctx, rcc._fullPath);
      var rccOffline=_offlineHtmlFor(ctx, rcc._fullPath);
      html+='<div class="card '+(rcc._fullPath===_getCurrentFolder()?'active':'')+'" data-path="'+_escapeHtml(rcc._fullPath)+'" data-read-child="1" tabindex="0" role="button">'+rccImg+'<div class="card-content"><div class="card-title">'+rccProgress.barHtml+'<span class="card-title-text">'+_escapeHtml(_readChildLabel)+'</span></div><div class="card-meta"></div></div>'+rccOffline+'</div>';
    }
    // Root exception: ONE self tile for root-level loose images.
    if(rootLoose){
      var csProgress=_progressFor(ctx, "");
      var csOffline=_offlineHtmlFor(ctx, "");
      html+='<div class="card" data-path="" data-read-self="1" tabindex="0" role="button">'+_cardPlaceholder()+'<div class="card-content"><div class="card-title">'+csProgress.barHtml+'<span class="card-title-text">'+_escapeHtml(_readChildLabel)+'</span></div><div class="card-meta"></div></div>'+csOffline+'</div>';
    }
    tileGrid.innerHTML=html||'<div style="color:#555;text-align:center;padding:20px;font-size:13px">'+_emptyBrowseMessage()+'</div>';
    _bindCards();
  }
  // scrollAnchor が渡された描画だけが位置を決める（undefined = 触らない）。
  // innerHTML 置換と同一タスクで書くので、誤った位置が 1 フレーム見えない。
  function _applyScrollAnchor(anchorPath){
    try { if (window.BrowseScroll) window.BrowseScroll.apply(anchorPath); } catch(e){ console.warn(e); }
  }
  function renderBrowse(opts){
    var mode=opts&&opts.mode?opts.mode:(window.viewMode||'tile');
    var results=opts&&'results' in opts?opts.results:(window.searchMode?window.searchResults:null);
    var isSearch=Array.isArray(results);
    if(mode==='tile'){ if(isSearch) { if(window.renderSearchTiles) window.renderSearchTiles(); else renderTiles(); } else renderTiles(); }
    else if(mode==='card'){ if(isSearch) { if(window.renderSearchCards) window.renderSearchCards(); else renderCards(); } else renderCards(); }
    else { if(isSearch) { if(window.renderSearchList) window.renderSearchList(); else renderList(); } else renderList(); }
    // 検索結果の描画には適用しない。undefined は「位置を決めない」の合図。
    if(!isSearch && opts && opts.scrollAnchor !== undefined) _applyScrollAnchor(opts.scrollAnchor);
  }
  window.multiThumbSpec=multiThumbSpec;
  window.FillPoll={noteFullLoad:noteFullLoad,stop:fillPollStop,state:fillPollState};
  window.renderTileImg=renderTileImg;
  window.hydrateMultiCovers=hydrateMultiCovers;
  window.scheduleMultiHydration=scheduleMultiHydration;
  window.renderTiles=renderTiles;
  window.renderList=renderList;
  window.renderCards=renderCards;
  window.renderBrowse=renderBrowse;
  window.BrowseRender={renderTiles,renderList,renderCards,renderBrowse,multiThumbSpec,splitDisplayName,nameHtml,hydrateMultiCovers,scheduleMultiHydration};
})();