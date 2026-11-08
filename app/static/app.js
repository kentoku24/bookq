// ── Folder tile grid ──
(function(){
  const sidebar = document.getElementById("sidebar");
  const viewer = document.getElementById("viewer");
  const menuBtn = document.getElementById("menu-btn");
  const searchInput = document.getElementById("search-input");
  const tileGrid = document.getElementById("tile-grid");
  const sidebarScroll = document.getElementById("sidebar-scroll");
  const folderName = document.getElementById("folder-name");
  const counter = document.getElementById("counter");
  const track = document.getElementById("img-track");
  const imgContainer = document.getElementById("img-container");
  const spinner = document.getElementById("spinner");
  const hint = document.getElementById("hint");
  const empty = document.getElementById("empty");
  const zonePrev = document.getElementById("zone-prev");
  const zoneToggle = document.getElementById("zone-toggle");
  const zoneNext = document.getElementById("zone-next");
  const toolbar = document.getElementById("toolbar");
  const pageSlider = document.getElementById("page-slider");
  const pageSliderWrap = document.getElementById("page-slider-wrap");
  const resumeBtn = document.getElementById("resume-btn");
  const resumeBtnText = document.getElementById("resume-btn-text");

  // ── PTR constants — top reload delegated to Safari native; keep bottom/horizontal ──
  // One reader for the media query — the same expression was written out
  // five times below.
  function prefersReducedMotion(){ return window.matchMedia('(prefers-reduced-motion: reduce)').matches; }

  const PTR_SNAP_MS = 180;
  const H_SWIPE_THRESHOLD = 60;
  const H_SWIPE_DEACTIVATE = 48;
  const H_DRAG_ARM = 20;
  const VIEWER_SWIPE_THRESHOLD = 70;
  const VIEWER_SWIPE_DEACTIVATE = 58;

  // ── Touch indicator for recordVideo — lazily loaded only when ?touch=1 or
  // localStorage.bookq_touch=1 (body lives in touch-indicator.js)
  if (location.search.includes('touch=1') || (()=>{try{return localStorage.getItem('bookq_touch')==='1'}catch(e) { console.warn(e); return false; }})()) {
    const s = document.createElement('script'); s.src = '/static/touch-indicator.js'; document.head.appendChild(s);
  }

  // ── Service Worker registration (Todo 2) ──
  // TEST_MODE gate: skip registration unless ?offline=1 or localStorage flag.
  (function registerSW(){
    if (!('serviceWorker' in navigator)) return;
    // Standalone exception: a Home Screen Web App has no URL bar, so ?offline=1 can never be entered there — always register when launched standalone.
    const isStandalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
    if (window.__BOOKQ_TEST_MODE__ === 'true' && !isStandalone && !location.search.includes('offline=1') && !localStorage.getItem('bookq_enable_sw')) return;
    // Only an *update* may reload. A first-ever install activates with
    // skipWaiting + clients.claim, which fires controllerchange while the page
    // has no previous controller — reloading there throws away the book the
    // user is already reading (and destroys the execution context under test).
    const hadController = !!navigator.serviceWorker.controller;
    let swReloaded = false;
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      if (!hadController || swReloaded) return;
      swReloaded = true;
      location.reload();
    });
    navigator.serviceWorker.register('/sw.js', { scope: '/' }).then(registration => {
      registration.addEventListener('updatefound', () => {
        const nw = registration.installing;
        if (!nw) return;
        nw.addEventListener('statechange', () => {
          if (nw.state === 'installed' && navigator.serviceWorker.controller) {
            showErrorToast('更新あり: 再読み込みで最新版が適用されます');
          }
        });
      });
    }).catch(e => { console.warn('SW register failed', e); });
  })();

  // ── OfflineStore / cache naming: shared with the harness via offline-core.js ──
  const OfflineStore = window.OfflineStore;
  const { encodePath, escapeHtml, folderHasOnlyImages, folderHasImages, fmt: fmtBytes, dedupedBytes, rawBytes, wipeClientData } = window.OfflineCore;
  let offlineMap = {};
  let offlineRecordMap = new Map();
  let offlineParentPaths = new Set();
  let _offlineMapGen = 0;
  function refreshOfflineMap(){
    const gen = ++_offlineMapGen;
    return OfflineStore.getAll().then(all=>{
      // The rebuild measured ~75ms on the T620 — landing it mid-gesture
      // stalls the scroll it's riding on, so wait for the gesture to settle.
      // A badge updating a few hundred ms late beats a dropped frame.
      // typeof guard: harness contexts run app.js without render.js loaded,
      // and an unguarded call would throw into the outer catch and never
      // apply the map at all.
      const settle = (typeof window.browseWhenSettled === 'function')
        ? window.browseWhenSettled()
        : Promise.resolve();
      return settle.then(()=>{
        // Resolve only after the map swap so existing `await
        // refreshOfflineMap(); renderView()` callers repaint with the fresh
        // map, not a stale one. A call overtaken while waiting (getAll ran
        // before a later caller's) must not apply its older snapshot over
        // the newer map — drop it instead.
        if (gen !== _offlineMapGen) return;
        const m={};
        all.forEach(w=>{ m[w.folder]=true; });
        offlineMap=m;
        window.offlineMap=m;
        offlineRecordMap=new Map(all.map(w=>[w.folder, w]));
        try { window.offlineRecordMap = offlineRecordMap; } catch(e) { console.warn(e); }
        offlineParentPaths=new Set();
        for(const folder of offlineRecordMap.keys()){
          for(let slash=folder.indexOf('/'); slash>=0; slash=folder.indexOf('/', slash+1)){
            offlineParentPaths.add(folder.slice(0, slash));
          }
        }
        try { window.offlineParentPaths = offlineParentPaths; } catch(e) { console.warn(e); }
        window.offlineRecords=all.slice();
      });
    }).catch(()=>{});
  }
  refreshOfflineMap();
  window.refreshOfflineMap = refreshOfflineMap;
  window.offlineMap = offlineMap;
  try { window.offlineRecordMap = offlineRecordMap; window.offlineParentPaths = offlineParentPaths; } catch(e) { console.warn(e); }
  // Logout erases client-held library data BEFORE the session is cleared
  // server-side, so an offline tap still wipes the local copies — the
  // /auth/logout interstitial then repeats the wipe and refreshes the '/'
  // shell entry for the online path. The race keeps a hung IDB from
  // blocking navigation; the server-side wipe is the backstop.
  const logoutBtn = document.getElementById('logout-btn');
  if (logoutBtn) {
    logoutBtn.addEventListener('click', (e) => {
      e.preventDefault();
      const wiped = Promise.resolve()
        .then(() => wipeClientData())
        .catch(() => {});
      Promise.race([wiped, new Promise(r => setTimeout(r, 3000))])
        .then(() => { location.href = '/auth/logout'; });
    });
  }
  function updateProgress(folder,pct){
    try{
      const esc = (typeof CSS !== 'undefined' && CSS.escape) ? CSS.escape(folder) : folder.replace(/["\\]/g,'\\$&');
      let els = document.querySelectorAll('.offline-btn[data-folder="'+esc+'"] .progress-ring');
      if(!els.length) els = document.querySelectorAll('.offline-btn[data-path="'+esc+'"] .progress-ring');
      if(!els.length) els = document.querySelectorAll('[data-folder="'+esc+'"] .progress-ring');
      if(!els.length) els = document.querySelectorAll('[data-path="'+esc+'"] .progress-ring');
      if(!els.length){
        els = Array.from(document.querySelectorAll('.offline-btn .progress-ring')).filter(ring=>{
          const btn = ring.closest('.offline-btn');
          return btn && btn.dataset.folder === folder;
        });
      }
      els.forEach(el=>{
        el.style.setProperty('--progress', pct*360+'deg');
      });
    } catch(e) { console.warn(e); }
  }
  window.updateProgress = updateProgress;
  // Safari 7-day mitigation: touch 1 key per work at startup (1-key only, quota-safe)
  (function scheduleOfflineTouch(){
    const run = async () => {
      try {
        const all = await OfflineStore.getAll();
        for (const w of all){
          try {
            const c = await caches.open(w.cacheName);
            const keys = await c.keys();
            if (keys[0]){
              const r = await c.match(keys[0]);
              if (r) await c.put(keys[0], r.clone());
            }
          } catch(e) { console.warn(e); }
        }
      } catch(e) { console.warn(e); }
    };
    if (document.readyState === 'loading'){
      document.addEventListener('DOMContentLoaded', () => { setTimeout(run, 0); }, {once:true});
    } else {
      setTimeout(run, 0);
    }
  })();

  let persistRequested = false;
  // Feature test: can the browser persist storage? Replaces UA sniff.
  const hasPersistedSupport = !!(typeof navigator !== 'undefined' && navigator.storage && typeof navigator.storage.persisted === 'function');

  // iOS Safari reports {usage:0, quota:0} from estimate(); fall back to the
  // bytes we recorded ourselves so the bar shows something real.
  // deps injection for unit tests — defaults to globals so callers need not change.
  function _resolveDeps(deps){
    deps = deps || {};
    return {
      fetch: deps.fetch || (typeof window !== 'undefined' && window.fetch ? window.fetch.bind(window) : fetch),
      caches: deps.caches || (typeof window !== 'undefined' ? window.caches : (typeof caches !== 'undefined' ? caches : undefined)),
      storage: deps.storage || (typeof navigator !== 'undefined' ? navigator.storage : undefined),
      OfflineStore: deps.OfflineStore || (typeof window !== 'undefined' ? window.OfflineStore : OfflineStore)
    };
  }
  async function updateStorageBar(deps){
    const _d = _resolveDeps(deps);
    const _storage = _d.storage || (typeof navigator !== 'undefined' ? navigator.storage : undefined);
    const _Store = _d.OfflineStore || OfflineStore;
    try{
      let est=null;
      if(_storage && _storage.estimate){
        try{ est=await _storage.estimate(); } catch(e) { if(window.__BOOKQ_DEBUG) console.warn(e); }
      }
      if(!est || (est.usage===0 && est.quota===0)){
        try{ const alt=await _Store.storageEstimate(); if(alt) est=alt; } catch(e) { console.warn(e); }
      }
      let usage=est ? (est.usage||0) : 0;
      const quota=est ? (est.quota||0) : 0;
      if(usage===0){
        try{
          const all=await _Store.getAll();
          usage = dedupedBytes(all) || rawBytes(all);
        } catch(e) { console.warn(e); }
      }
      const textEl=document.getElementById('storage-text');
      if(textEl){
        textEl.textContent = quota>0
          ? `💾 ${fmtBytes(usage)} / 空き ${fmtBytes(Math.max(0, quota-usage))}`
          : `💾 ${fmtBytes(usage)}`;
      }
    } catch(e) { console.warn(e); }
  }
  function updateOnlineStatus(deps){
    const _d = _resolveDeps(deps);
    const _fetch = _d.fetch;
    const el=document.getElementById('offline-status'); if(!el) return; _fetch(globalThis.BookqApi.endpoints.health+'?online_check=1&_t='+Date.now(),{method:'GET',cache:'no-store',headers:{'X-Online-Check':'1'}}).then(r=>{ const online=!!r.ok; el.textContent=online?'●オンライン':'○オフライン'; el.classList.toggle('offline', !online); }).catch(()=>{ el.textContent='○オフライン'; el.classList.add('offline'); }); }
  let _onlineRevalidateLast = 0;
  let _onlineRevalidateTimer = null;
  // Todo 5: warm-only conditional fetch. Keys mirror the server
  // _folders_response cache keys (path,depth): root full vs shallow1.
  // Cold first fetch (loadFolders stages) stays unconditional — ETag never
  // claims cold savings. Only fetchLatestFolders/revalidateFoldersForOnline
  // send If-None-Match, and only when a prior response supplied an ETag;
  // a missing ETag falls back to the isSameTree compare, exactly once.
  const etagByKey = new Map();
  function _foldersEtagKey(depth){ return ":" + (depth ? "shallow" + depth : "full"); }
  const _rawFoldersText = new WeakMap();
  async function readFoldersResponse(res, depth, isCurrent){
    if(res.status === 304){
      // Warm revalidation hit: server tree unchanged. No body; the caller
      // keeps the current tree with zero redraws and no retry loop.
      return {notModified:true};
    }
    if(!res.ok) return;
    const _cur = () => (typeof isCurrent !== "function") || !!isCurrent();
    let data = null;
    if (typeof res.text === "function"){
      let text;
      try { text = await res.text(); } catch(e) { return; }
      if (!_cur()) return;
      try { data = JSON.parse(text); } catch(e) { return; }
      {
        const nodes = globalThis.BookqApi.nodesOf(data);
        if (nodes) { try { _rawFoldersText.set(nodes, {text: text, treeEnvelope: !Array.isArray(data)}); } catch(e) {} }
        data = nodes;
      }
    } else if (typeof res.json === "function"){
      try { data = await res.json(); } catch(e) { return; }
      if (!_cur()) return;
      data = globalThis.BookqApi.nodesOf(data);
    } else return;
    if(!Array.isArray(data)) return;
    if (!_cur()) return;
    try {
      const etag = res.headers ? res.headers.get("ETag") : null;
      const key = _foldersEtagKey(depth);
      if (etag) etagByKey.set(key, etag);
      else etagByKey.delete(key);
    } catch(e) { console.warn(e); }
    return data;
  }
  async function fetchLatestFolders(deps, opts){
    const _d = _resolveDeps(deps);
    const depth = (opts && opts.depth) || null;
    const url = globalThis.BookqApi.treeUrl(depth ? {depth: depth} : null);
    // "no-cache" = 必ずサーバに再検証（If-None-Match）するが、304 なら HTTP
    // キャッシュのボディを使う。"no-store" だと 3.8MB を毎回まるごと取り直す。
    const init = {cache:"no-cache", credentials:"same-origin"};
    const warmEtag = etagByKey.get(_foldersEtagKey(depth));
    if (warmEtag) init.headers = {"If-None-Match": warmEtag};
    const res = await _d.fetch(url, init);
    return readFoldersResponse(res, depth, opts && opts.isCurrent);
  }
  // tree carries _fullPath from setFullPaths; /api/folders does not — strip it for deep-equal.
  function isSameTree(a, b){
    const stripFullPath = (v) => JSON.stringify(v, (k,val)=> k==='_fullPath' ? undefined : val);
    try{ return stripFullPath(a)===stripFullPath(b); }catch(e) { console.warn(e); return false; }
  }
  // Walk up until a node exists again. Textual split is fallback-only for this
  // missing-path recovery; live lookups keep tree-walk (no string-split).
  function resolveAliveAncestor(path){
    let next = path;
    while(next && !getNodeAt(next)){
      const parent = getParentPath(next);
      if(parent !== null) next = parent;
      else {
        const slash = next.lastIndexOf('/');
        next = slash === -1 ? null : next.slice(0, slash);
      }
    }
    return next;
  }
  let _revalidateInFlight = null;
  async function _revalidateFoldersForOnlineInner(deps){
    const _d = _resolveDeps(deps);
    try{
      if(typeof navigator!=='undefined' && !navigator.onLine) return;
      const now=Date.now();
      if(now - _onlineRevalidateLast < 5*60*1000) return;
      const loadGen = _loadFoldersGen;
      const result = await fetchLatestFolders(_d, {isCurrent: function(){ return loadGen === _loadFoldersGen; }});
      if(!result) return;
      if (loadGen !== _loadFoldersGen) return;
      if(result && result.notModified){
        // 304: tree unchanged — stamp the gate, redraw nothing, loop never.
        _onlineRevalidateLast = now;
        return;
      }
      const data = result;
      if(isSameTree(tree, data)){
        _onlineRevalidateLast = now;
        return;
      }
      const map = await setFullPathsAndIndexAsync(data);
      if(!map || loadGen !== _loadFoldersGen) return;
      tree = data;
      invalidateLeafPaths();
      if(currentBrowsePath && !getNodeAt(currentBrowsePath)){
        currentBrowsePath = resolveAliveAncestor(currentBrowsePath);
      }
      try{
        const c = await (_d.caches || caches).open("bookq-api-v1");
        await c.put(globalThis.BookqApi.endpoints.tree, new Response(JSON.stringify(data), {headers:{"Content-Type":"application/json"}})).catch(()=>{});
      } catch(e) { console.warn(e); }
      if (loadGen !== _loadFoldersGen) return;
      try{ await refreshOfflineMap(); } catch(e) { console.warn(e); }
      if (loadGen !== _loadFoldersGen) return;
      // IDB is the sole offline tree source (IDB-only since 8aacd57): persist
      // the revalidated tree alongside Cache, otherwise the next offline
      // reload would serve the stale snapshot. Full-only, background, never
      // blocking the redraw (Todo2 contract; shallow never reaches here).
      _persistTreeSnapshot(data, {defer:true});
      updateBrowseHeader();
      renderView();
      scheduleThumbPrefetch();
      _onlineRevalidateLast = now;
    } catch(e) { console.warn(e); }
  }
  function revalidateFoldersForOnline(deps){
    if(!_revalidateInFlight){
      _revalidateInFlight = _revalidateFoldersForOnlineInner(deps).then(
        function(){ _revalidateInFlight = null; },
        function(e){ _revalidateInFlight = null; console.warn(e); }
      );
    }
    return _revalidateInFlight;
  }
  function scheduleOnlineRevalidation(){
    if(_onlineRevalidateTimer) clearTimeout(_onlineRevalidateTimer);
    _onlineRevalidateTimer = setTimeout(()=>{ _onlineRevalidateTimer=null; revalidateFoldersForOnline(); }, 500);
  }
  window.addEventListener('online',()=>{ updateOnlineStatus(); updateStorageBar(); scheduleOnlineRevalidation(); });
  // Going offline must invalidate the freshness stamp: an online→offline→online
  // flip inside the 5-minute window has to revalidate immediately, since the
  // tree could not be revalidated while offline.
  window.addEventListener('offline',()=>{ _onlineRevalidateLast = 0; if(_onlineRevalidateTimer){ clearTimeout(_onlineRevalidateTimer); _onlineRevalidateTimer = null; } updateOnlineStatus(); updateStorageBar(); });
  window.revalidateFoldersForOnline = revalidateFoldersForOnline;
  function ensurePersist(deps){
    if(persistRequested) return;
    persistRequested = true;
    try {
      const _d = _resolveDeps(deps);
      const _storage = _d.storage || (typeof navigator !== 'undefined' ? navigator.storage : undefined);
      if(!_storage || !_storage.persist) return;
      const explainOnce = !localStorage.getItem('bookq_persist_toast');
      const _persisted = _storage.persisted.bind(_storage);
      const _persist = _storage.persist.bind(_storage);
      _persisted().then(p=>{
        if(p) return;
        return _persist().then(granted=>{
          if(!granted && explainOnce){
            showErrorToast('オフライン保持のためブラウザの保存許可をしてください');
            try{ localStorage.setItem('bookq_persist_toast','1'); } catch(e) { console.warn(e); }
          }
        });
      }).catch(()=>{});
    } catch(e) { console.warn(e); }
  }
  window.updateStorageBar = updateStorageBar;
  window.updateOnlineStatus = updateOnlineStatus;
  window.hasPersistedSupport = hasPersistedSupport;
  window._resolveDeps = _resolveDeps;
  if(document.readyState==='loading'){ document.addEventListener('DOMContentLoaded',()=>{ updateStorageBar(); updateOnlineStatus(); }); } else { updateStorageBar(); updateOnlineStatus(); }

  // offline/storeAdapter.js owns the offline state machine. These are
  // delegations only: a missing module throws here at startup (wrong defer
  // order in index.html) instead of running a second, drifting copy.
  function isFolderOffline(folder){ return window.StoreAdapter.isFolderOffline(folder); }
  function invalidateLeafPaths(){ return window.StoreAdapter.invalidateLeafPaths(); }
  function getLeafPathsFor(folder, deps){ return window.StoreAdapter.getLeafPathsFor(folder, deps); }
  function computeLeafPathsFor(folder, deps){ return window.StoreAdapter.getLeafPathsFor(folder, deps); }

  const downloadControllers = window.downloadControllers || new Map();
  window.downloadControllers = downloadControllers;
  const OfflineBtnState = window.StoreAdapter.OfflineBtnState;
  const _downloadingSet = window._downloadingSet || new Set();
  window._downloadingSet = _downloadingSet;
  const _stoppingSet = window._stoppingSet || new Set();
  window._stoppingSet = _stoppingSet;
  const _listingSet = window._listingSet || new Set();
  window._listingSet = _listingSet;
  const _deletingSet = window._deletingSet || new Set();
  window._deletingSet = _deletingSet;
  const _confirmMap = window._confirmMap || new Map();
  window._confirmMap = _confirmMap;
  function getPartialProgress(folder){ return window.StoreAdapter.getPartialProgress(folder); }
  function getOfflineBtnState(folder){ return window.StoreAdapter.getOfflineBtnState(folder); }
  window.OfflineBtnState = OfflineBtnState;
  window.getPartialProgress = getPartialProgress;
  window.getOfflineBtnState = getOfflineBtnState;

  // Quota-exceeded confirm dialog: offline/storeAdapter.js owns the markup,
  // the z-order and the double-open promise. This is the delegation only.
  function showQuotaConfirm(opts){ return window.StoreAdapter.showQuotaConfirm(opts); }

  async function downloadWork(folder, deps){ return window.StoreAdapter.downloadWork(folder, deps); }
  async function deleteWork(folder, deps){ return window.StoreAdapter.deleteWork(folder, deps); }
  window.downloadWork = downloadWork;
  window.deleteWork = deleteWork;
  window.showQuotaConfirm = showQuotaConfirm;
  window.ensurePersist = ensurePersist;
  window.getLeafPathsFor = getLeafPathsFor;
  window.computeLeafPathsFor = computeLeafPathsFor;
  window.invalidateLeafPaths = invalidateLeafPaths;
  window.loadFolders = loadFolders;
  window.loadFacets = loadFacets;
  window.doSearch = doSearch;
  window.fetchLatestFolders = fetchLatestFolders;
  window.revalidateFoldersForOnline = revalidateFoldersForOnline;
  // First user gesture of the session — persist() must be called from one, and
  // {once:true} means this listener gets exactly one chance, so don't make it
  // conditional on *which* element was clicked.
  document.addEventListener('click', ()=>{ ensurePersist(); }, {once:true});

  // One delegated listener for every render variant's DL / 削除 button. Survives
  // the tileGrid.innerHTML replacement that each render does; the per-item
  // click handlers all bail out early on .offline-btn, so both can fire.
  function offlineControlsHtml(path){ return window.StoreAdapter.offlineControlsHtml(path); }
  // ── anchored labeled popover for partially-downloaded works ──
  // Lives inside the .tile/.card/.item (position:relative), anchored under its
  // .offline-btn. Never uses CSS `bottom`: that anchors to the tile's own
  // bottom edge and drifts far from the button on tall tiles/cards.
  function closeAllPartialPops(){ return window.StoreAdapter.closeAllPartialPops(); }
  function openPartialPopover(folder){ return window.StoreAdapter.openPartialPopover(folder); }
  tileGrid.addEventListener('click', (e)=>{
    const btn = e.target.closest && e.target.closest('.offline-btn');
    if(!btn) return;
    e.stopPropagation();
    const folder = btn.dataset.folder;
    if(!folder) return;
    if(_confirmMap.has(folder)){
      const kind = _confirmMap.get(folder);
      _confirmMap.delete(folder);
      try{ renderView(); } catch(e) { console.warn(e); }
      if(kind === 'delete'){
        deleteWork(folder);
      }
      return;
    }
    const state = getOfflineBtnState(folder);
    if(state === OfflineBtnState.IDLE){
      downloadWork(folder);
      return;
    } else if(state === OfflineBtnState.PARTIAL){
      openPartialPopover(folder);
      return;
    } else if(state === OfflineBtnState.COMPLETE){
      _confirmMap.set(folder,'delete');
      try{ renderView(); } catch(e) { console.warn(e); }
      return;
    } else if(state === OfflineBtnState.DOWNLOADING){
      const ctrl = downloadControllers.get(folder);
      if(ctrl) try{ ctrl.abort(); } catch(e) { console.warn(e); }
      _downloadingSet.delete(folder);
      _stoppingSet.add(folder);
      try{ renderView(); } catch(e) { console.warn(e); }
      return;
    } else if(state === OfflineBtnState.STOPPING){
      return;
    } else if(state === OfflineBtnState.DELETING){
      return;
    } else {
      if(isFolderOffline(folder)) deleteWork(folder); else downloadWork(folder);
    }
  });
  // Global popover/confirm dismiss (outside-tap + Escape) is owned by
  // offline/storeAdapter.js via __offlineGlobalDismissAttached — do not add a
  // second document-level dismiss here or popovers render twice per keypress.

  // ── ReadingProgress: localStorage-backed persistence ──
  // Todo 4: renders must batch-parse ONCE per render (see browse/render.js
  // _buildRenderCtx) — never call get() per tile. getAllSafe/pick are the
  // one-shot path; corrupt values degrade to {} so the grid never falls.
  const ReadingProgress = {
    KEY: 'bookq_progress',
    _saveTimer: null,
    getAll() { try { return JSON.parse(localStorage.getItem(this.KEY)) || {}; } catch(e) { console.warn(e); return {}; } },
    getAllSafe() { try { return JSON.parse(localStorage.getItem(this.KEY)) || {}; } catch(e) { return {}; } },
    pick(paths) {
      const all = this.getAllSafe();
      const m = {};
      for (const p of (paths || [])) m[p] = all[p] || null;
      return m;
    },
    get(folderPath) { return this.getAll()[folderPath] || null; },
    save(folderPath, page, total) {
      const ts = Date.now();
      this._store(folderPath, page, total, ts);
      // Mirror the latest position server-side (single slot, LWW). push() is
      // fire-and-forget with its own 2s trailing debounce — the turn path
      // never awaits it, so burst turning stays free of added latency.
      // Guarded: a stale precached `/` can pair an old shell (no sync.js)
      // with a newer app.js pulled from the network.
      if (window.ProgressSync) window.ProgressSync.push({ folder: folderPath, page, total, ts });
    },
    // A server-held position pulled at boot lands here: same write + cap as
    // save() but keeps the remote ts verbatim and never re-pushes (no echo
    // loop — a pulled record must not immediately write itself back).
    mergeRemote(folderPath, page, total, ts) {
      this._store(folderPath, page, total, ts);
    },
    _store(folderPath, page, total, ts) {
      const all = this.getAll();
      all[folderPath] = { page, total, ts };
      const keys = Object.keys(all).sort((a, b) => (all[b].ts || 0) - (all[a].ts || 0));
      for (let i = 50; i < keys.length; i++) delete all[keys[i]];
      localStorage.setItem(this.KEY, JSON.stringify(all));
    },
    saveDebounced(folderPath, page, total) {
      clearTimeout(this._saveTimer);
      this._saveTimer = setTimeout(() => this.save(folderPath, page, total), 500);
    },
  };
  try { window.ReadingProgress = ReadingProgress; } catch(e) { console.warn(e); }

  // ── URL state: the open book + page are mirrored in the hash ──
  // Format: #/view/<folder>?p=<page>. Folder is the currently displayed chapter's
  // path, page is the chapter-local index (same semantics as ReadingProgress).
  // Opening a book pushes one entry; every page turn rewrites it in place
  // (replaceState) so Back walks books, never pages.
  // ── hash routing delegated to /static/browse/hash.js (single source) ──
  // Original hash helpers and urlStack were duplicated here and in hash.js,
  // causing duplicate listeners and hash clearing when treeLoaded false.
  // hash.js now owns the prefixes, parsers, writers, urlStack and the sole
  // listener. Keep urlRestoring etc as mirrored state so loads can bump token.
  // Preserve plain values hash.js may have written before these getters exist.
  if (!window.HashRouter) console.error('HashRouter missing: check defer order in index.html');
  let urlRestoring = (typeof window.urlRestoring === 'boolean') ? window.urlRestoring : false;
  let urlRestoreGen = (typeof window.urlRestoreGen === 'number') ? window.urlRestoreGen : 0;
  let urlSyncPaused = (typeof window.urlSyncPaused === 'boolean') ? window.urlSyncPaused : false;
  let treeLoaded = (typeof window.treeLoaded === 'boolean') ? window.treeLoaded : false;
  try {
    Object.defineProperty(window, 'treeLoaded', {get(){return treeLoaded;}, set(v){treeLoaded=v;}, configurable:true});
    Object.defineProperty(window, 'urlRestoring', {get(){return urlRestoring;}, set(v){urlRestoring=v;}, configurable:true});
    Object.defineProperty(window, 'urlRestoreGen', {get(){return urlRestoreGen;}, set(v){urlRestoreGen=v;}, configurable:true});
    Object.defineProperty(window, 'urlSyncPaused', {get(){return urlSyncPaused;}, set(v){urlSyncPaused=v;}, configurable:true});
  } catch(e) { console.warn(e); }
  // ── two-stage first paint (Todo2: shallow depth=1 → full) ──
  // _shallowMode is true between the shallow first paint and the full-tree
  // arrival. While true: facet filtering stays off (shallow nodes carry no
  // subtree_facets), treeLoaded stays false (no prefetch/applyUrlState leak),
  // and drill-down/viewer opens queue until the full tree lands.
  // _pendingNav records the single latest click-queued navigation
  // ({kind:'browse'|'view', path}) from the shallow window or a pending
  // visibility judgment — last write wins. URL deep-links need no record —
  // applyUrlState early-returns while !treeLoaded, so the current hash is
  // still pending and is applied once, after full arrival.
  let _shallowMode = false;
  let _pendingNav = null;
  // Delegated aliases — hash.js loads first (defer order) so window.* exist
  // when this IIFE executes. Fall back to no-ops if not yet loaded (should not happen).
  const VIEWER_HASH_PREFIX = (window.VIEWER_HASH_PREFIX || "#/view/");
  const BROWSE_HASH_PREFIX = (window.BROWSE_HASH_PREFIX || "#/browse/");
  const parseViewerHash = (...a) => window.parseViewerHash ? window.parseViewerHash(...a) : null;
  const viewerHash = (...a) => window.viewerHash ? window.viewerHash(...a) : "";
  const parseBrowseHash = (...a) => window.parseBrowseHash ? window.parseBrowseHash(...a) : null;
  const browseHash = (...a) => window.browseHash ? window.browseHash(...a) : "";
  const currentViewerState = (...a) => window.currentViewerState ? window.currentViewerState(...a) : null;
  const pushViewerState = (...a) => window.pushViewerState ? window.pushViewerState(...a) : undefined;
  const replaceViewerState = (...a) => window.replaceViewerState ? window.replaceViewerState(...a) : undefined;
  const scheduleUrlSync = (...a) => window.scheduleUrlSync ? window.scheduleUrlSync(...a) : undefined;
  const clearViewerState = (...a) => window.clearViewerState ? window.clearViewerState(...a) : undefined;
  const urlStackPush = (...a) => window.urlStackPush ? window.urlStackPush(...a) : undefined;
  const urlStackReplace = (...a) => window.urlStackReplace ? window.urlStackReplace(...a) : undefined;
  const reconcileUrlStack = (...a) => window.reconcileUrlStack ? window.reconcileUrlStack(...a) : undefined;
  const pushBrowseUrl = (...a) => window.pushBrowseUrl ? window.pushBrowseUrl(...a) : undefined;
  const writeBrowseStateWith = (...a) => window.writeBrowseStateWith ? window.writeBrowseStateWith(...a) : undefined;
  const writeBrowseState = (...a) => window.writeBrowseState ? window.writeBrowseState(...a) : undefined;
  // urlStack / urlStackPos are owned by hash.js and exposed via window getters.

  // Tear down the page strip: evict every img/cell/spread element, release
  // pooled images, drop chapter labels, forget cached sizes and the anchor,
  // and neutralize the track's transition/transform. Shared by loadImages and
  // resetToTop — keep the two in sync (any change to strip teardown goes here).
  function resetStrip() {
    // Reset strip
    for (const k of Object.keys(stripImgs)){
      const img = stripImgs[k];
      const cell = stripCells[k];
      if (cell) cell.remove();
      if (img) imagePool.release(img);
      delete stripImgs[k];
      delete stripCells[k];
    }
    // Reset chapter labels
    for (const label of chapterLabels) label.remove();
    chapterLabels = [];
    for (const k of Object.keys(spreadEls)) spreadEls[k].remove();
    spreadEls = {}; pageAR = {}; lastAR = 0.7; anchorSpread = 0;
    for (const k of Object.keys(pairBlankCells)) pairBlankCells[k].remove();
    pairBlankCells = {};
    track.style.transition = "none";
    track.style.transform = "translateX(0)";
  }

  // applyUrlState etc are owned by /static/browse/hash.js — single listener.
  // Keep local name as alias so existing call sites (loadFolders.then etc) work
  // without rewriting every reference.
  function applyUrlState(...a){ return window.applyUrlState ? window.applyUrlState(...a) : undefined; }

  // Tear down the viewer: strip + viewer-state reset. Shared by resetToTop
  // and restoreBrowse — any change to viewer teardown goes here. Does NOT
  // touch the #empty prompt or the URL.
  function teardownViewer(){
    // Leaving the book ends any paired view: solo layout settings were never
    // touched, so dropping the projection state is enough here.
    if (pairStrip){
      pairStrip = null;
      window.PairedView.clear();
      imgContainer.classList.remove("pair-mode");
      imgContainer.classList.remove("pair-blank");
    }
    resetStrip();
    currentFolder = null; images = []; chapterList = []; chapterBoundaries = []; currentIdx = 0;
    folderName.textContent = "";
    counter.textContent = "";
    pageSliderWrap.classList.remove("visible");
    imgContainer.classList.remove("loading");
    spinner.style.display = "none";
    sidebar.classList.add("open"); viewer.classList.add("sidebar-open");
    syncSidebarA11y();
  }

  // resetToTop / restoreBrowse / returnToBrowse delegated to hash.js
  function resetToTop(...a){ return window.resetToTop ? window.resetToTop(...a) : undefined; }
  function restoreBrowse(...a){ return window.restoreBrowse ? window.restoreBrowse(...a) : undefined; }
  function returnToBrowse(...a){ return window.returnToBrowse ? window.returnToBrowse(...a) : undefined; }

  function progressBarHtml(saved) {
    if (!saved || !saved.total) return { barHtml: '', inlineBarHtml: '' };
    const pct = Math.min(100, Math.round(((saved.page + 1) / saved.total) * 100));
    return {
      barHtml: `<div class="tile-progress-track"><div class="tile-progress-fill" style="width:${pct}%"></div></div>`,
      inlineBarHtml: `<span class="tile-progress-inline"><span class="tile-progress-fill" style="width:${pct}%"></span></span>`
    };
  }

  let tree = [];
  let currentFolder = null; // full path (may include / for nested)
  let viewMode = 'tile';    // 'tile' | 'list' | 'card'
  Object.defineProperty(window, 'viewMode', {get(){return viewMode;}, set(v){viewMode=v;}, configurable:true});
  let images = [];
  let currentIdx = 0;
  let currentChapterIdx = 0; // tracked chapter for syncChapterSlider max-write gating
  try {
    Object.defineProperty(window, 'currentFolder', {get(){return currentFolder;}, set(v){currentFolder=v;}, configurable:true});
    Object.defineProperty(window, 'images', {get(){return images;}, set(v){images=v;}, configurable:true});
    Object.defineProperty(window, 'currentIdx', {get(){return currentIdx;}, set(v){currentIdx=v;}, configurable:true});
    Object.defineProperty(window, 'tree', {get(){return tree;}, set(v){tree=v;}, configurable:true});
  } catch(e) { console.warn(e); }
  let stripImgs = {};       // { idx: HTMLImageElement } — created lazily
  let stripCells = {};      // { idx: HTMLDivElement } — page-cell wrappers
  let spreadEls = {};       // { spreadNo: HTMLDivElement } — .spread-container wrappers
  let pageAR = {};          // { idx: naturalWidth/naturalHeight } — survives evictStrip
  let lastAR = 0.7;         // fallback aspect for not-yet-loaded pages (~manga 1:1.4)
  const PREFETCH_AHEAD = 8;  // pages pre-fetched forward (manga: next is to the left)
  const PREFETCH_BEHIND = 3; // pages kept behind
  const EVICT_AHEAD = 14, EVICT_BEHIND = 6;  // beyond this, recycle the DOM node
  const GAP_INNER = 5;  // gap between the two pages inside a spread
  // GAP_OUTER/CHAPTER_GAP_PX live only in viewer/spread.js now (gapBefore());
  // these were dead duplicates.
  try {
    Object.defineProperty(window, 'stripImgs', {get(){return stripImgs;}, set(v){stripImgs=v;}, configurable:true});
    Object.defineProperty(window, 'stripCells', {get(){return stripCells;}, set(v){stripCells=v;}, configurable:true});
    Object.defineProperty(window, 'spreadEls', {get(){return spreadEls;}, set(v){spreadEls=v;}, configurable:true});
    Object.defineProperty(window, 'pageAR', {get(){return pageAR;}, set(v){pageAR=v;}, configurable:true});
    Object.defineProperty(window, 'lastAR', {get(){return lastAR;}, set(v){lastAR=v;}, configurable:true});
    Object.defineProperty(window, 'PREFETCH_AHEAD', {get(){return PREFETCH_AHEAD;}, configurable:true});
    Object.defineProperty(window, 'PREFETCH_BEHIND', {get(){return PREFETCH_BEHIND;}, configurable:true});
    Object.defineProperty(window, 'EVICT_AHEAD', {get(){return EVICT_AHEAD;}, configurable:true});
    Object.defineProperty(window, 'EVICT_BEHIND', {get(){return EVICT_BEHIND;}, configurable:true});
  } catch(e) { console.warn(e); }

  let chapterList = [];         // [{name, fullPath, images: [{name, folder}]}]
  let chapterBoundaries = [];   // [globalImageIdx where each chapter starts]
  try {
    Object.defineProperty(window, 'chapterList', {get(){return chapterList;}, set(v){chapterList=v;}, configurable:true});
    Object.defineProperty(window, 'chapterBoundaries', {get(){return chapterBoundaries;}, set(v){chapterBoundaries=v;}, configurable:true});
  } catch(e) { console.warn(e); }
  let chapterLabels = [];       // DOM elements for chapter divider labels
  let realToGlobalIdx = [];     // realIdx → globalIdx (including spacers)
  let globalToRealIdx = [];     // globalIdx → realIdx (-1 if spacer)
  let spacerCount = 0;
  let realImageCount = 0;
  try {
    Object.defineProperty(window, 'chapterLabels', {get(){return chapterLabels;}, set(v){chapterLabels=v;}, configurable:true});
    Object.defineProperty(window, 'realToGlobalIdx', {get(){return realToGlobalIdx;}, set(v){realToGlobalIdx=v;}, configurable:true});
    Object.defineProperty(window, 'globalToRealIdx', {get(){return globalToRealIdx;}, set(v){globalToRealIdx=v;}, configurable:true});
    Object.defineProperty(window, 'realImageCount', {get(){return realImageCount;}, set(v){realImageCount=v;}, configurable:true});
    Object.defineProperty(window, 'spacerCount', {get(){return spacerCount;}, set(v){spacerCount=v;}, configurable:true});
  } catch(e) { console.warn(e); }

  // ── ImagePool: recycle <img> elements to avoid GC jank ──
  // Instead of creating/destroying <img> on every page turn, we keep a pool
  // of pre-created elements. When an image scrolls out of range, its src is
  // cleared (aborting any in-flight fetch + releasing decoded bitmap memory)
  // and the element returns to the pool for reuse.
  const imagePool = window.SpreadView.imagePool;
  let hintTimer = null;

  // ── Search / Filter state ──
  let searchMode = false;
  let searchResults = [];
  let searchTimer = null;
  let activeFilters = { artist: [], tag: [], sort: 'relevance' };
  let facets = { artists: [], tags: [] };
  let facetLoadGen = 0;

  // ── orientation / responsive ──
  let pageMode = 'single';  // 'single' | 'spread'
  let landscapeSpread = true; // landscape default: 2-page spread (user-togglable)
  let spreadOffset = 0; // 0=default pairs (0,1)(2,3)...; 1=shifted (cover solo, then 1,2)(3,4)...
  let viewW = window.innerWidth;
  let viewH = window.innerHeight;
  try {
    Object.defineProperty(window, 'pageMode', {get(){return pageMode;}, set(v){pageMode=v;}, configurable:true});
    Object.defineProperty(window, 'landscapeSpread', {get(){return landscapeSpread;}, set(v){landscapeSpread=v;}, configurable:true});
    Object.defineProperty(window, 'spreadOffset', {get(){return spreadOffset;}, set(v){spreadOffset=v;}, configurable:true});
    Object.defineProperty(window, 'viewW', {get(){return viewW;}, set(v){viewW=v;}, configurable:true});
    Object.defineProperty(window, 'viewH', {get(){return viewH;}, set(v){viewH=v;}, configurable:true});
  } catch(e) { console.warn(e); }
  const spreadToggle = document.getElementById("spread-toggle");
  const spreadControls = document.getElementById("spread-controls");
  const spreadShift = document.getElementById("spread-shift");

  // ── tap direction (tap-swap): which side advances on single tap. Session-only.
  let tapForward = 'left'; // 'left' | 'right'
  const tapmodeControls = document.getElementById("tapmode-controls");
  const tapmodeLeft = document.getElementById("tapmode-left");
  const tapmodeRight = document.getElementById("tapmode-right");

  // ── spread model ──
  // A spread is the unit of layout AND of navigation: 1 page in single mode,
  // 1-2 in spread mode (solo cover when spreadOffset=1, solo trailing page on an
  // odd count). Pairing runs over GLOBAL indices so the chapter spacers keep
  // doing their job of forcing a chapter to start on the right-hand page; the
  // spacers themselves are then dropped, which is what turns the page they were
  // padding into a solo spread.
  let spreads = [];      // [{pages:[globalIdx,...], chapterStart:bool}]
  let idxToSpread = [];  // global page idx → spread number
  let spreadSizeCache = {};
  let anchorSpread = 0;  // positions are measured relative to this spread
  let sliding = false;   // a page-turn transition is in flight
  let windowWorkGen = 0; // snapTo defers prefetch-window/evict work past the first frame; a newer turn cancels the older job
  const SLIDE_MS = 300;  // matches the .3s transform transition in applyTrackTransform
  let lastTurnAt = -Infinity; // performance.now() of the last snapTo — turns within SLIDE_MS cut instantly (burst mode)
  let snapGen = 0;       // bumped to invalidate a pending slide (new snap, or a drag)

  // ── paired (dual-reader) rendering state — S06 / #81 ──
  // pairStrip is non-null while a committed paired snapshot is being shown
  // and not left. `spreads`/`idxToSpread` then hold the SHARED spread table
  // (idx-mapped, wire `id` preserved); renderPagesForSpread() projects each
  // spread to the ONE page this device renders ([] = blank side). Solo mode
  // keeps it null and every code path below is byte-for-byte pre-pair.
  let pairStrip = null;      // {active, side, spreadId, renderSet:Set, solo:{...}}
  let pairBlankCells = {};   // { spreadNo: HTMLDivElement.page-cell.pair-blank }
  // ?perf=1 page-turn profiler state. Declared HERE, with the rest of the
  // snapTo state, because snapTo reads it: a `const` declared further down the
  // IIFE is in its temporal dead zone until that line runs, and any synchronous
  // snapTo before then would throw ReferenceError. The overlay itself is built
  // in the profiler section below.
  // PWA (Home Screen Web App) has no URL bar, so ?perf=1 can never be entered
  // there — localStorage bookq_perf=1 (toggled from the offline harness)
  // enables the profiler the same way (takes effect on next document load,
  // e.g. the harness's "back to Viewer" link which reloads the app).
  const perfOn = /[?&]perf=1/.test(location.search) || (()=>{try{return localStorage.getItem('bookq_perf')==='1'}catch(e){return false}})();
  // ?view=jpeg routes reader pages through /view (server-normalized JPEG —
  // webp/avif software decode is the suspected page-turn stall on low-end
  // devices). Same localStorage escape hatch as bookq_perf for the PWA.
  // readerUrl is shared with downloadWork so offline cache keys match.
  const viewJpeg = /[?&]view=jpeg/.test(location.search) || (()=>{try{return localStorage.getItem('bookq_view')==='jpeg'}catch(e){return false}})();
  try { window.OfflineCore.readerUseView = viewJpeg; } catch(e) { console.warn(e); }
  // Android composites the toolbar/sidebar backdrop-filter by re-blurring
  // the scrolled backdrop every frame — measured fps 4 vs 22 (Firefox) and
  // 45 vs 83 (Chrome) on the CP08-J1 tile grid. The server tags <html> with
  // .ua-android from the request UA header (no client feature test exists and
  // UA sniffing is banned in this file); CSS drops the frost, desktop keeps it.
  // ?noblur=1 drops backdrop-filter from the floating toolbar for scroll A/B
  // measurement on low-end GPUs (diagnostic flag, not a shipping behavior).
  if (/[?&]noblur=1/.test(location.search)) {
    try {
      const st = document.createElement('style');
      st.textContent = '#toolbar{-webkit-backdrop-filter:none !important;backdrop-filter:none !important}';
      document.head.appendChild(st);
    } catch(e) { console.warn(e); }
  }
  let perfTapAt = 0, perfTapX = 0;
  try {
    Object.defineProperty(window, 'spreads', {get(){return spreads;}, set(v){spreads=v;}, configurable:true});
    Object.defineProperty(window, 'idxToSpread', {get(){return idxToSpread;}, set(v){idxToSpread=v;}, configurable:true});
    Object.defineProperty(window, 'spreadSizeCache', {get(){return spreadSizeCache;}, set(v){spreadSizeCache=v;}, configurable:true});
    Object.defineProperty(window, 'anchorSpread', {get(){return anchorSpread;}, set(v){anchorSpread=v;}, configurable:true});
    Object.defineProperty(window, 'sliding', {get(){return sliding;}, set(v){sliding=v;}, configurable:true});
    Object.defineProperty(window, 'snapGen', {get(){return snapGen;}, set(v){snapGen=v;}, configurable:true});
  } catch(e) { console.warn(e); }

  // viewer/spread.js owns the spread model. These wrappers exist only so the
  // call sites below keep their bare names; they write back through the
  // window.* getters/setters defined just above, so the locals stay in sync.
  function invalidateSpreadSizes(){ return window.SpreadView.invalidateSpreadSizes(); }

  function rebuildSpreads(){ return window.SpreadView.rebuildSpreads(); }

  function spreadOf(idx){ return window.SpreadView.spreadOf(idx); }

  function arOf(idx){ return window.SpreadView.arOf(idx); }

  function spreadMetrics(s){ return window.SpreadView.spreadMetrics(s); }

  function gapBefore(s){ return window.SpreadView.gapBefore(s); }

  function spreadLeft(s){ return window.SpreadView.spreadLeft(s); }

  // The global page indices THIS device renders for spread s: the paired
  // projection ([] on a blank side) or the plain spread pages solo.
  // spread.js's spreadMetrics reads this through the window export.
  function renderPagesForSpread(s){
    const sp = spreads[s];
    if (!sp) return [];
    if (pairStrip && pairStrip.active){
      const own = pairStrip.side === 'right' ? sp.rightIdx : sp.leftIdx;
      return (typeof own === 'number' && own >= 0) ? [own] : [];
    }
    return sp.pages;
  }
  try { window.renderPagesForSpread = renderPagesForSpread; } catch(e) { console.warn(e); }

  // The image index the reader actually looks at for anchor idx: this device's
  // own page of the spread, or -1 when the assigned side is blank (no <img>).
  function displayIdxFor(idx){
    if (pairStrip && pairStrip.active){
      const rp = renderPagesForSpread(spreadOf(idx));
      return rp.length ? rp[0] : -1;
    }
    return idx;
  }
  // Overview/zoom's "page being read" hook: own page when rendered, the spread
  // anchor when blank — a blank cell still sits at the spread's position.
  try {
    window.pairedDisplayIdx = (idx) => {
      const d = displayIdxFor(idx);
      return d >= 0 ? d : idx;
    };
  } catch(e) { console.warn(e); }

  function updateSpreadToggleLabel(){
    spreadToggle.textContent = pageMode === 'spread' ? "2P" : "1P";
  }

  // ── slider helpers: one slider step == one spread ──
  function sliderMax(){ return Math.max(0, spreads.length - 1); }
  function idxToSlider(globalIdx){ return spreadOf(globalIdx); }
  function sliderToIdx(val){
    const s = spreads[Math.max(0, Math.min(val, spreads.length - 1))];
    return s ? s.pages[0] : 0;
  }

  // ── chapter-local slider mapping: one slider step == one spread within chapter ──
  function getChapterSpreadRange(chapterIdx){
    if (!spreads.length) return {lo: 0, hi: 0};
    const c = Math.max(0, Math.min(chapterIdx, chapterBoundaries.length - 1));
    if (chapterBoundaries.length <= 1) return {lo: 0, hi: Math.max(0, spreads.length - 1)};
    const start = chapterBoundaries[c];
    const end = c + 1 < chapterBoundaries.length ? chapterBoundaries[c + 1] : images.length;
    let firstReal = -1, lastReal = -1;
    for (let i = start; i < end; i++){
      if (images[i] && !images[i].isSpacer){ if (firstReal < 0) firstReal = i; lastReal = i; }
    }
    if (firstReal < 0){
      const s = spreadOf(Math.min(start, Math.max(0, images.length - 1)));
      return {lo: s, hi: s};
    }
    return {lo: spreadOf(firstReal), hi: spreadOf(lastReal)};
  }
  function chapterSliderMax(chapterIdx){
    const r = getChapterSpreadRange(chapterIdx);
    return Math.max(0, r.hi - r.lo);
  }
  function idxToChapterSlider(globalIdx){
    const r = getChapterSpreadRange(getCurrentChapterInfo(globalIdx).chapterIdx);
    return spreadOf(globalIdx) - r.lo;
  }
  function chapterSliderToIdx(chapterIdx, val){
    const r = getChapterSpreadRange(chapterIdx);
    const s = spreads[Math.max(r.lo, Math.min(val + r.lo, r.hi))];
    return s ? s.pages[0] : 0;
  }
  function getChapterRealCount(chapterIdx){
    if (!chapterList.length) return 0;
    const c = Math.max(0, Math.min(chapterIdx, chapterList.length - 1));
    const ch = chapterList[c];
    return ch && ch.images ? ch.images.length : 0;
  }
  function getChapterLocalRealIdx(globalIdx){
    return globalIdx - chapterBoundaries[getCurrentChapterInfo(globalIdx).chapterIdx];
  }
  // ── chapter-local slider sync: central max/value write point (wired up by later tasks) ──
  function syncChapterSlider(globalIdx, opts){
    const resetMax = !!(opts && opts.resetMax === true);
    let info;
    try { info = getCurrentChapterInfo(globalIdx); }
    catch(e){ return; }
    if (!info || typeof info.chapterIdx !== "number") return;
    const range = getChapterSpreadRange(info.chapterIdx);
    const lo = range && typeof range.lo === "number" ? range.lo : 0;
    const hi = range && typeof range.hi === "number" ? range.hi : 0;
    const span = Math.max(0, hi - lo);
    const pageSlider = document.getElementById("page-slider");
    if (!pageSlider){ currentChapterIdx = info.chapterIdx; return; }
    if (resetMax || info.chapterIdx !== currentChapterIdx) pageSlider.max = String(span);
    let v = 0;
    try { v = spreadOf(globalIdx) - lo; }
    catch(e){ v = 0; }
    if (typeof v !== "number" || isNaN(v)) v = 0;
    pageSlider.value = String(Math.max(0, Math.min(v, span)));
    currentChapterIdx = info.chapterIdx;
  }

  function updateShiftButtonStyle(){
    if (!spreadShift) return;
    // Paired: the lit state tracks the shared session offset, not local
    // solo layout.
    if (pairStrip && pairStrip.active){
      if (pairStrip.offset === 1) spreadShift.classList.add("active");
      else spreadShift.classList.remove("active");
      return;
    }
    if (pageMode === 'spread' && spreadOffset === 1) spreadShift.classList.add("active");
    else spreadShift.classList.remove("active");
  }
  try { window.updateShiftButtonStyle = updateShiftButtonStyle; window.updateSpreadToggleLabel = updateSpreadToggleLabel; } catch(e) { console.warn(e); }

  spreadShift.addEventListener("click", e => {
    e.stopPropagation();
    if (pairStrip && pairStrip.active){
      // Paired: the offset is shared session state — one setOffset intent
      // reaches the server sequencer; the rebuilt manifest commits back
      // through applyCommittedState (S09). A local shift never runs here.
      window.ViewerNavigation.request({type: "setOffset", offset: pairStrip.offset === 0 ? 1 : 0, via: "spread-shift"});
      return;
    }
    if (pageMode !== 'spread' || !images.length) return;
    // Offset change is a user movement request — one setOffset intent
    // through the navigation port (default handler applies it locally).
    window.ViewerNavigation.request({type: "setOffset", offset: spreadOffset === 0 ? 1 : 0, via: "spread-shift"});
  });

  spreadToggle.addEventListener("click", e => {
    e.stopPropagation();
    // Paired: page grouping is the shared spread table, not a local mode.
    if (pairStrip && pairStrip.active) return;
    if (viewW < viewH || !images.length) return; // only in landscape with images
    if (pageMode === 'spread'){
      // Switch to 1-page mode in landscape
      landscapeSpread = false;
      pageMode = 'single';
      document.body.className = 'landscape-single';
      // o (spread shift) is meaningless in 1P and must not resurface as a
      // stale &o=1 the next time the user switches back to 2P.
      spreadOffset = 0;
    } else {
      // Switch to 2-page spread mode: current page becomes RIGHT page of spread
      landscapeSpread = true;
      pageMode = 'spread';
      document.body.className = 'landscape';
      currentIdx = Math.floor(currentIdx / 2) * 2;
    }
    rebuildSpreads();
    regroupCells();
    updateStripPositions();
    syncChapterSlider(currentIdx, {resetMax:true});
    snapTo(currentIdx, false);
    updateSpreadToggleLabel();
    updateShiftButtonStyle();
    scheduleUrlSync();
  });

  let lastOrientW = -1, lastOrientH = -1;
  let orientRuns = 0;
  function updateOrientation(force){
    // force=true bypasses the no-op guard: loadImages calls this right after
    // setting landscapeSpread/spreadOffset from the URL (決定 Q17 restore), and
    // the viewport dimensions are usually unchanged from the last call, so the
    // guard below would skip recomputing pageMode and the URL's layout would
    // never take effect. The guard still applies to real resize/orientation
    // events, where re-running on an unchanged size is genuinely a no-op.
    if (!force && window.innerWidth === lastOrientW && window.innerHeight === lastOrientH) return; // keyboard/sidebar resize: nothing changed
    lastOrientW = window.innerWidth; lastOrientH = window.innerHeight;
    orientRuns++;
    viewW = window.innerWidth; viewH = window.innerHeight;
    invalidateSpreadSizes(); // viewport changed → every contain-fit is stale
    const prevMode = pageMode;
    const isLandscape = viewW >= viewH;
    if (isLandscape && landscapeSpread){
      pageMode = 'spread';
      document.body.className = 'landscape';
    } else if (isLandscape && !landscapeSpread){
      pageMode = 'single';
      document.body.className = 'landscape-single';
    } else {
      pageMode = 'single';
      document.body.className = 'portrait';
    }
    updateSpreadToggleLabel();
    const modeChanged = prevMode !== pageMode;
    if (!images.length) return;
    // Paired: rotation must not touch the shared spread table, the anchor, or
    // the assigned PageRef — only re-fit sizes and re-snap the same spread.
    const pairedNow = !!(pairStrip && pairStrip.active);
    if (modeChanged && !pairedNow){
      if (pageMode === 'spread') currentIdx = Math.floor(currentIdx / 2) * 2;
    }
    if (!pairedNow){
      rebuildSpreads();
      regroupCells();
    }
    updateStripPositions();
    syncChapterSlider(currentIdx, {resetMax:true});
    snapTo(currentIdx, false);
    updateShiftButtonStyle();
  }
  window.addEventListener('resize', () => updateOrientation()); // do NOT pass updateOrientation directly: the resize Event object would land in its `force` param and always be truthy, defeating the no-op guard
  window.addEventListener('orientationchange', () => setTimeout(updateOrientation, 150));

  // ── sidebar toggle ──
  // Single place that toggles the `inert` attribute — every "hide from tab
  // order + a11y tree" call site below goes through this instead of its own
  // setAttribute/removeAttribute pair.
  function setInert(el, hidden){
    if (hidden) el.setAttribute("inert", "");
    else el.removeAttribute("inert");
  }
  // H4(b): a closed sidebar must leave the tab order and the accessibility
  // tree. Every code path that toggles .open (closeSidebar, toggleSidebar,
  // the F-key handler) goes through syncSidebarA11y so inert/aria-hidden
  // can never drift from the class.
  function syncSidebarA11y(){
    const open = sidebar.classList.contains("open");
    setInert(sidebar, !open);
    if (open) sidebar.removeAttribute("aria-hidden");
    else sidebar.setAttribute("aria-hidden", "true");
  }
  // H4(b): the resume pill is only meaningful while visible; hiding it must
  // also remove it from the tab order.
  function truncateMiddle(s, max = 24) {
    s = String(s || "");
    const chars = Array.from(s);
    if (chars.length <= max) return s;
    return chars.slice(0, 12).join("") + "…" + chars.slice(-8).join("");
  }
  function hideResumeBtn(){ resumeBtn.classList.add("hidden"); setInert(resumeBtn, true); sidebar.classList.remove("has-resume"); }
  function showResumeBtn(){ resumeBtn.classList.remove("hidden"); setInert(resumeBtn, false); sidebar.classList.add("has-resume"); }
  function closeSidebar(){ sidebar.classList.remove("open"); viewer.classList.remove("sidebar-open"); hideResumeBtn(); syncSidebarA11y(); }
  try { window.closeSidebar = closeSidebar; window.setInert = setInert; window.syncSidebarA11y = syncSidebarA11y; } catch(e) { console.warn(e); }
  // The visual side of opening the drawer — shared with browse/pairSync.js
  // (paired scroll-mirror opens the follower's sidebar without a nav intent).
  function openSidebar(){
    sidebar.classList.add("open");
    viewer.classList.add("sidebar-open");
    syncSidebarA11y();
    updateResumeButton();
  }
  try { window.openSidebar = openSidebar; } catch(e) { console.warn(e); }
  function toggleSidebar(){
    if (sidebar.classList.contains("open")) { closeSidebar(); return; }
    openSidebar();
    if (currentFolder) {
      // Opening the sidebar over a reader is a "back to the folder list"
      // request — it goes through the navigation port (default handler
      // returns to the browse level and its #/browse/… URL, which keeps
      // a stale #/view/… hash off screen while the grid is up).
      window.ViewerNavigation.request({type: "browse", via: "sidebar"});
    } else {
      if (searchMode) window.BrowseRender.renderBrowse({mode: 'list', results: searchResults});
      else window.BrowseRender.renderBrowse({mode: viewMode, results: null});
    }
  }
  menuBtn.addEventListener("click", toggleSidebar);
  // The folder-name label next to ☰ is a hitbox too — same toggle
  folderName.addEventListener("click", toggleSidebar);
  document.getElementById("sidebar-header").addEventListener("click", e => {
    if (e.target.tagName === "INPUT") return; // don't close when typing in search
    closeSidebar();
  });
  document.addEventListener("keydown", e => {
    if (e.key !== "Escape") return;
    // H4(c): the filter sheet is a real dialog (role=dialog aria-modal=true)
    // now — Escape must dismiss IT first, not the sidebar behind it.
    if (bottomSheet.classList.contains("open")) { closeFilterSheet(); return; }
    if (document.querySelector(".ux-pop")) { closeAllPartialPops(); return; }
    closeSidebar(); hideResumeBtn();
  });

  // ── load folders ──
  function setFullPaths(nodes, parentPath){
    for (const node of nodes){
      node._fullPath = parentPath ? parentPath + "/" + node.name : node.name;
      if (node.children) setFullPaths(node.children, node._fullPath);
    }
  }

  // Todo 5: single-traversal index. One walk assigns _fullPath, builds the
  // nodeMap, and emits both thumb lists matching OfflineThumb.collect —
  // singles via {singleOnly:true} (device cover width, the same string
  // resolveCoverSingle requests), full via the default path (singles at the
  // device cover width + multi per-cell w=200, MAX_PER_NODE cap — the
  // tile/list DOM contract; card-img w=400 is not enumerated). Multi tiles
  // enumerate cell thumbs, not the sprite-sheet URL the DOM fetches — that
  // mismatch is by design (prefetch eligibility is DOM-scan driven).
  // Replaces the three separate
  // walks (setFullPaths + buildNodeMap + collect); prefetch reuses the
  // cached lists instead of re-walking. findNodeByPath stays a linear walk.
  let _nodeMapCache = new Map();
  let _indexedTreeRef = null;
  let _thumbSingleListCache = [];
  let _thumbFullListCache = [];
  let _indexGen = 0;
  const _indexNow = (typeof performance === "object" && performance && typeof performance.now === "function")
    ? function(){ return performance.now(); }
    : Date.now;
  const _indexYieldQueue = [];
  let _indexYieldChannel = null;
  function _indexYield(fn){
    if (typeof MessageChannel === "function"){
      if (!_indexYieldChannel){
        try {
          _indexYieldChannel = new MessageChannel();
          _indexYieldChannel.port1.onmessage = function(){
            const f = _indexYieldQueue.shift();
            if (f) f();
          };
        } catch(e) { _indexYieldChannel = null; }
      }
      if (_indexYieldChannel){
        _indexYieldQueue.push(fn);
        _indexYieldChannel.port2.postMessage(0);
        return;
      }
    }
    setTimeout(fn, 0);
  }
  function _indexBuilder(nodes){
    const map = new Map();
    const singles = [];
    const full = [];
    const seenS = new Set();
    const seenF = new Set();
    let maxPerNode = 9;
    try {
      const api = (typeof window !== "undefined" && window.OfflineThumb) ? window.OfflineThumb : null;
      if (api && api.MAX_PER_NODE) maxPerNode = api.MAX_PER_NODE;
    } catch(e) { console.warn(e); }
    // Device cover width — the enumerate lists must carry the URL the DOM
    // actually requests on this device (coverWidth: dpr>=2 → 400, else 200).
    let coverW = 200;
    try {
      const oc = (typeof window !== "undefined" && window.OfflineCore) ? window.OfflineCore : null;
      if (oc && oc.coverWidth) coverW = oc.coverWidth();
    } catch(e) { console.warn(e); }
    function thumbUrlFor(fp, name, w){
      try {
        const OC = (typeof window !== "undefined" && window.OfflineCore) ? window.OfflineCore : null;
        if (OC && OC.thumbUrl) return OC.thumbUrl(fp, name, w);
      } catch(e) { console.warn(e); }
      const width = (w === 200 || w === 800) ? w : 400;
      return "/thumb/" + String(fp).split("/").map(encodeURIComponent).join("/")
        + "/" + String(name).split("/").map(encodeURIComponent).join("/") + "?w=" + width;
    }
    const stack = [{list: nodes, i: 0, parentPath: ""}];
    function step(){
      const start = _indexNow();
      let visited = 0;
      while (stack.length){
        const fr = stack[stack.length - 1];
        if (fr.i >= (fr.list ? fr.list.length : 0)){ stack.pop(); continue; }
        const n = fr.list[fr.i++];
        visited++;
        n._fullPath = fr.parentPath ? fr.parentPath + "/" + n.name : n.name;
        const fp = n._fullPath;
        map.set(fp, n);
        if (fp){
          if (n.thumb){
            const su = thumbUrlFor(fp, n.thumb, coverW);
            if (!seenS.has(su)){ seenS.add(su); singles.push({url:su, folder:fp, thumb:n.thumb}); }
            const fu = thumbUrlFor(fp, n.thumb, coverW);
            if (!seenF.has(fu)){ seenF.add(fu); full.push({url:fu, folder:fp, thumb:n.thumb}); }
          }
          if (n.thumbs && Array.isArray(n.thumbs) && n.thumbs.length >= 2){
            for (const t of n.thumbs.slice(0, maxPerNode)){
              const u = thumbUrlFor(fp, t, 200);
              if (!seenF.has(u)){ seenF.add(u); full.push({url:u, folder:fp, thumb:t}); }
            }
          }
        }
        if (n.children) stack.push({list: n.children, i: 0, parentPath: fp});
        if (visited >= 512 || _indexNow() - start >= 4) return false;
      }
      return true;
    }
    return {step: step, map: map, singles: singles, full: full};
  }
  var _indexProvisional = false;   // 公開済み index が暫定（shallow）ツリー由来か
  function _publishIndex(nodes, b, provisional){
    _indexProvisional = !!provisional;
    _nodeMapCache = b.map;
    _indexedTreeRef = nodes;
    _thumbSingleListCache = b.singles;
    _thumbFullListCache = b.full;
    try {
      if (typeof window !== "undefined") window.__cc_thumbIndex = {tree:nodes, single:b.singles, full:b.full};
    } catch(e) { console.warn(e); }
    // トップ階層ノードの _fullPath を FolderVisibility へ — 非表示キー
    // （flatten 名は / を含む）と editable 判定の両方がここを見る。暫定
    // （shallow）ツリーでは公開しない: 生の dir 名を editable 集合に混ぜると
    // PUT すると 404 になるトグルが出てしまう。前回の authoritative set は
    // 維持する（cold boot では空のまま＝トグル無し）。
    try {
      var _fv = (typeof window !== "undefined") ? window.FolderVisibility : null;
      if (!provisional && _fv && typeof _fv.setTopLevelPaths === "function"){
        var _tops = [];
        for (var _ti = 0; nodes && _ti < nodes.length; _ti++){
          var _tn = nodes[_ti];
          if (_tn && _tn.type === "folder" && _tn._fullPath) _tops.push(_tn._fullPath);
        }
        _fv.setTopLevelPaths(_tops);
      }
    } catch(e) { console.warn(e); }
  }
  // Sync build+publish WITHOUT superseding: callers that only need a map for
  // the current tree (buildNodeMap read/repair) must not cancel an in-flight
  // async build. setFullPathsAndIndex keeps the generation bump — an explicit
  // public replacement still supersedes.
  function _buildIndexSync(nodes, provisional){
    const b = _indexBuilder(nodes);
    while (!b.step()) {}
    _publishIndex(nodes, b, provisional);
    return b.map;
  }
  function setFullPathsAndIndex(nodes, provisional){
    _indexGen++;
    return _buildIndexSync(nodes, provisional);
  }
  function setFullPathsAndIndexAsync(nodes){
    const gen = ++_indexGen;
    const b = _indexBuilder(nodes);
    return new Promise(function(resolve){
      function loop(){
        if (gen !== _indexGen){ resolve(null); return; }
        if (b.step()){
          if (gen !== _indexGen){ resolve(null); return; }
          _publishIndex(nodes, b, false);
          resolve(b.map);
          return;
        }
        _indexYield(loop);
      }
      loop();
    });
  }

  let currentBrowsePath = null; // null=root, else full path of folder being browsed
  let scrollPositions = {}; // { "mode:path": scrollTop } — preserve scroll when navigating back (key includes viewMode)
  try {
    Object.defineProperty(window, 'currentBrowsePath', {get(){return currentBrowsePath;}, set(v){currentBrowsePath=v;}, configurable:true});
    Object.defineProperty(window, 'scrollPositions', {get(){return scrollPositions;}, set(v){scrollPositions=v;}, configurable:true});
  } catch(e) { console.warn(e); }
  function getScrollKey(path){ return viewMode + ':' + (path || '_root'); }
  try { window.getScrollKey = getScrollKey; } catch(e) { console.warn(e); }

  // ── folder navigation ──
  function getNodeAt(path){
    if (!path) return null;
    return findNodeByPath(tree, path);
  }

  function filtersActive(){
    return activeFilters.artist.length > 0 || activeFilters.tag.length > 0;
  }
  function emptyBrowseMessage(){
    if (_shallowMode) return "読込中…";
    return filtersActive()
      ? "絞り込みに一致するフォルダがありません"
      : "フォルダが見つかりません";
  }
  function facetGroupMatches(nodeValues, filterValues){
    if (!filterValues.length) return true;
    return filterValues.some(fv => (nodeValues || []).some(cv => cv.toLowerCase().includes(fv.toLowerCase())));
  }
  function nodeMatchesFilters(node){
    const f = node && node.subtree_facets;
    if (!f) return false;
    return facetGroupMatches(f.artists, activeFilters.artist)
        && facetGroupMatches(f.tags, activeFilters.tag);
  }

  function getBrowseChildrenAt(path){
    let children;
    if (!path) children = tree.filter(n => n.type === "folder");
    else {
      const node = getNodeAt(path);
      if (!node || !node.children) return [];
      children = node.children.filter(n => n.type === "folder");
    }
    // 非表示適用はファセット絞り込み・shallow 早期 return より先 — raw tree
    // は削らず、ここで表示側だけを絞る。snapshot 無しの loading/error は
    // render 側の blockedGridHtml 門が一覧自体を出さないので、ここでは
    // isVisible の fail-closed だけ見ればよい。FV 無しの stale shell では
    // 絞らない（watchdog が修復する）。
    var FV = window.FolderVisibility;
    if (FV && !FV.showAll) children = children.filter(n => FV.isVisible(n._fullPath));
    if (filtersActive()){
      // Shallow window: nodes carry no subtree_facets, so nodeMatchesFilters
      // would false out everything — never filter, show all + 読込中 badge.
      if (_shallowMode) return children;
      children = children.filter(nodeMatchesFilters);
    }
    return children;
  }
  function getBrowseChildren(){
    return getBrowseChildrenAt(currentBrowsePath);
  }
  try { window.getBrowseChildrenAt = getBrowseChildrenAt; } catch(e) { console.warn(e); }

  function updateBrowseHeader(){
    const h = document.getElementById("browse-header");
    if (!h) return;
    if (!currentBrowsePath){
      h.innerHTML = '<span id="browse-root-icon">📁</span> フォルダ';
      h.style.cursor = "default";
      h.onclick = null;
    } else {
      const node = getNodeAt(currentBrowsePath);
      h.innerHTML = "↩ " + escapeHtml(node ? node.name : currentBrowsePath);
      h.style.cursor = "pointer";
        h.onclick = (e) => {
        e.stopPropagation();
        const parent = browseParentOf(currentBrowsePath);
        // Same whole-screen pop slide as the edge-swipe commit; the animated
        // path lands in _finishPop which owns state+render+scroll restore.
        if (_startPopAnim(parent)) return;
        currentBrowsePath = parent;
        updateBrowseHeader();
        renderView();
        writeBrowseStateWith(parent);
        // Restore scroll position (mode+path key)
        const key = getScrollKey(currentBrowsePath);
        if (scrollPositions[key] !== undefined){
          requestAnimationFrame(() => { sidebarScroll.scrollTop = scrollPositions[key]; });
        }
      };
    }
  }

  // Shallow-window facet guard: disable the filter UI with a 読込中 badge.
  // Called after every restoreTree render (shallow sets it, full clears it).
  function updateShallowBadge(){
    try {
      const btn = document.getElementById("filter-btn");
      if (btn){
        if (_shallowMode){ btn.disabled = true; btn.title = "読込中…"; }
        else { btn.disabled = false; btn.title = "絞り込み"; }
      }
      if (_shallowMode){
        const fc = document.getElementById("folder-count");
        if (fc && !/読込中/.test(fc.textContent)) fc.textContent = fc.textContent + "（読込中）";
      }
    } catch(e) { console.warn(e); }
  }

  function navigateToFolder(path){
    var FV = window.FolderVisibility;
    if (FV){
      // guardPath: 判定不能（idle/loading / error 無snapshot）はキューだけ、
      // hidden はトップへ、それ以外は通常通り潜る。
      var _g = FV.guardPath(path);
      if (_g === "pending"){ _pendingNav = {kind: "browse", path: path}; return; }
      if (_g === "hidden"){ redirectHiddenToTop(); return; }
    }
    if (_shallowMode){
      // Shallow window: target children==[] — queue the drill-down in the URL
      // (pushState fires no hashchange; applyUrlState runs once post-full) and
      // show the existing waiting style until the full tree lands.
      _pendingNav = {kind: "browse", path: path};
      try { writeBrowseStateWith(path); } catch(e) { console.warn(e); }
      const tg = document.getElementById("tile-grid");
      if (tg) tg.innerHTML = '<div style="color:#555;text-align:center;padding:20px;font-size:13px">読込中…</div>';
      return;
    }
    const key = getScrollKey(currentBrowsePath);
    scrollPositions[key] = sidebarScroll.scrollTop;
    // Push slide-in: clone the CURRENT level as the underlay first (it must
    // capture the pre-render grid), then the live panel slides in from the
    // right over it — the new level covers the old, iOS-style.
    let pushUnder = null;
    if (!prefersReducedMotion() && !navAnimBusy && sidebar && sidebar.classList.contains('open')) {
      pushUnder = mountNavUnderlay(null);
      if (pushUnder) {
        const uss = navUnderlaySlot ? navUnderlaySlot.querySelector('#sidebar-scroll') : null;
        if (uss) uss.scrollTop = sidebarScroll.scrollTop;
      }
    }
    currentBrowsePath = path;
    updateBrowseHeader();
    // 開いた直後の位置: 直下に既読エピソードがあればその最大番号、無ければ上端。
    // window.BrowseScroll は index.html の defer 順で app.js より先に読み込まれる
    // ハード依存（window.Overview と同じ扱い。フォールバック実装は書かない）。
    const decision = window.BrowseScroll.decide({ level: path, cause: 'open', hasSaved: false });
    renderView({ scrollAnchor: decision.anchor });
    writeBrowseState();
    if (pushUnder) _startPushAnim(pushUnder);
  }
  try { window.navigateToFolder = navigateToFolder; } catch(e) { console.warn(e); }

  // ── render tile grid ──
  // thumbs/resolveCover.js owns the cover fallback chain (thumb → thumbs[0] → null).
  function _coverUrl(node, w){ return window.ThumbResolve.resolveCover(node, w); }
  function buildNodeMap(){
    // Todo 5: the map is built by setFullPathsAndIndex on every restoreTree;
    // return the cache when it tracks the live tree, rebuild (one walk) only
    // when an out-of-band mutation replaced the reference. The repair walk
    // uses the non-superseding helper — a pending async full-index build
    // must still finish and publish.
    if (_indexedTreeRef === tree && _nodeMapCache) return _nodeMapCache;
    // 修復 walk は現在の暫定フラグを維持 — shallow tree の repair が
    // authoritative topLevel 公開のふりをしないよう引き継ぐ。
    return _buildIndexSync(tree, _indexProvisional);
  }
  // Todo 4: single delegated #tile-grid listener + closest() — replaces the
  // per-tile addEventListener loops (N listeners → 1). The selector arg is
  // kept for caller compat; the handler matches the browse union
  // (.tile/.item[data-path]/.card) and no-ops while searchMode (search tiles
  // own their delegated handler below). Parent-level images-tiles
  // (data-read-child / root data-read-self) open the viewer directly;
  // plain folder tiles keep the old loadImages-vs-navigateToFolder branching.
  function _bindBrowseClicks(selector){
    if(!tileGrid.__ccBrowseDelegated){
      tileGrid.__ccBrowseDelegated = true;
      tileGrid.addEventListener("click", e=>{
        if(window.searchMode) return;
        const el = e.target.closest ? e.target.closest('.tile,.item[data-path],.card') : null;
        if(!el || !tileGrid.contains(el)) return;
        if(e.target.closest('.offline-btn') || e.target.closest('.ux-pop') || e.target.closest('.visibility-toggle')) return;
        if(el.dataset.readChild === "1" || el.dataset.readSelf === "1"){
          navOpenWork(el.dataset.path, "tile");
          return;
        }
        const path = el.dataset.path;
        let hasOnly = false;
        try {
          const m = window.__cc_hasOnlyMap;
          const node = getNodeAt(path);
          if(node && m && (path in m)) hasOnly = !!m[path];
          else hasOnly = folderHasOnlyImages(node);
          if (node && hasOnly){
            navOpenWork(path, "tile");
          } else {
            navigateToFolder(path);
          }
        } catch(err){ console.warn(err); }
      });
    }
  }
  function _bindSearchClicks(selector){
    if(!tileGrid.__ccSearchDelegated){
      tileGrid.__ccSearchDelegated = true;
      tileGrid.addEventListener("click", e=>{
        if(!window.searchMode) return;
        const el = e.target.closest ? e.target.closest('.tile,.item[data-path],.card') : null;
        if(!el || !tileGrid.contains(el)) return;
        if(e.target.closest('.offline-btn') || e.target.closest('.ux-pop') || e.target.closest('.visibility-toggle')) return;
        const path = el.dataset.path;
        if (!path) return;
        navOpenWork(path, "search");
        searchInput.value = "";
        searchMode = false;
        searchResults = [];
        clearTimeout(searchTimer);
      });
    }
  }
  try { window._bindBrowseClicks = _bindBrowseClicks; window._bindSearchClicks = _bindSearchClicks; } catch(e) { console.warn(e); }

  // ── thumb prefetch: per-work cache via OfflineCore.imageUrl (per-segment encodePath) ──
  const PREFETCH_CONCURRENCY = 3;
  async function prefetchThumbs(){ return window.ThumbPrefetch.prefetchThumbs(); }
  function scheduleThumbPrefetch(){ return window.ThumbPrefetch.scheduleThumbPrefetch(); }
  // Tree is usable — flip the url-state gate and kick thumb prefetch together
  // so no loadFolders path can enable one without the other.
  function markTreeReady(){
    treeLoaded = true;
    scheduleThumbPrefetch();
  }

  function _persistTreeSnapshot(data, opts){
    try {
      const _raw = _rawFoldersText.get(data);
      try { _rawFoldersText.delete(data); } catch(e){}
      const _OT = window.OfflineTree;
      if (_raw && _OT && typeof _OT.putJson === "function"){
        const _txt = typeof _raw === "string" ? _raw : _raw.text;
        void _OT.putJson(_txt, Object.assign({}, opts, {treeEnvelope: !!(typeof _raw === "object" && _raw.treeEnvelope)})).catch(function(){});
      } else {
        void window.OfflineTree.putTree(data, opts).catch(function(){});
      }
    } catch(e) { console.warn(e); }
  }

  let _loadFoldersGen = 0;
  async function loadFolders(opts, deps){
    const _d = _resolveDeps(deps);
    const _fetch = _d.fetch;
    const preservePath = !!(opts && opts.preservePath);
    const _wantedPath = preservePath ? currentBrowsePath : null;
    const _loadGen = ++_loadFoldersGen;
    _indexGen++;
    // Interface contract: restoreTree(tree, {shallow}). shallow:true skips
    // isSameTree compare (none here) and OfflineTree.putTree (full-only per
    // routes.py:287-290), keeps treeLoaded=false with no markTreeReady /
    // prefetch / applyUrlState, and never calls nodeMatchesFilters
    // (getBrowseChildren guard + 読込中 badge instead).
    async function restoreTree(data, opts){
      const shallow = !!(opts && opts.shallow);
      if (_loadGen !== _loadFoldersGen) return false;
      if (shallow){
        tree = data;
        setFullPathsAndIndex(tree, true);
        treeLoaded = false;
      } else {
        const map = await setFullPathsAndIndexAsync(data);
        if (!map || _loadGen !== _loadFoldersGen) return false;
        tree = data;
      }
      _shallowMode = shallow;
      try { window.__cc_singleCoverFirst = shallow; } catch(e) { console.warn(e); }
      invalidateLeafPaths();
      // Todo 5: first paint before IndexedDB — folder names/counts must not
      // wait on refreshOfflineMap (OfflineStore.getAll). The offline map
      // lands afterwards and the second renderView converges the buttons
      // (transient un-stored state is acceptable; final state identical).
      let nextPath = _wantedPath;
      if (_wantedPath && !getNodeAt(_wantedPath)) nextPath = null;
      currentBrowsePath = nextPath;
      updateBrowseHeader();
      renderView();
      updateShallowBadge();
      // The map refresh can sit behind a settle wait (~12s worst) while the
      // user is mid-gesture — loadFolders' stage-2 fetch must not queue
      // behind it, so don't await: converge the badges when the map lands.
      try { refreshOfflineMap().then(()=>{ if (_loadGen === _loadFoldersGen){ renderView(); updateShallowBadge(); } }); } catch(e) { console.warn(e); }
      if (shallow) return true;
      if (_loadGen !== _loadFoldersGen) return false;
      markTreeReady();
      // Drain the shallow-window queue: clicks stashed the single latest
      // nav (kind view|browse — last write wins); URL deep-links (#/browse
      // drill-down, #/view open) stayed pending because applyUrlState
      // early-returns while !treeLoaded — one re-run now that the full
      // tree resolves them.
      const queuedNav = _pendingNav; _pendingNav = null;
      if (queuedNav && queuedNav.kind === "view") { loadImages(queuedNav.path); }
      else if (typeof location !== "undefined" && location.hash) { applyUrlState(); }
      if (queuedNav && queuedNav.kind === "browse" && _pendingNav === null &&
          !(typeof location !== "undefined" && location.hash)){
        // visibility-pending browse queue (no URL written for it) — keep it
        // stashed for the ready-edge drain instead of dropping it; a hash
        // always wins (URL is the canonical queue for deep-links).
        _pendingNav = queuedNav;
      }
      return true;
    }
    // Stage 1: shallow first paint (direct children only, no facets).
    let shallowOk = false;
    try {
      const res = await _fetch(globalThis.BookqApi.treeUrl({depth: 1}), {credentials: 'same-origin'});
      if (_loadGen !== _loadFoldersGen) return;
      if (!res.ok) throw new Error("http "+res.status);
      const data = await readFoldersResponse(res, 1, () => _loadGen === _loadFoldersGen);
      if (_loadGen !== _loadFoldersGen) return;
      if (!Array.isArray(data)) throw new Error("Malformed folders response");
      if (!await restoreTree(data, {shallow:true})) return;
      shallowOk = true;
    } catch(e){ console.warn("shallow folders failed, falling through to full", e); }
    // Stage 2: full tree in the background — final DOM identical to full-only.
    const _applyFullRes = async function(res){
      const data = await readFoldersResponse(res, null, () => _loadGen === _loadFoldersGen);
      if (_loadGen !== _loadFoldersGen) return false;
      if (!Array.isArray(data)) throw new Error("Malformed folders response");
      // Render first, persist in the background — the fetched `data` drives the
      // tree, IDB persistence only matters for the next reload, so it must
      // never delay first paint.
      if (!await restoreTree(data, {shallow:false})) return false;
      if (_loadGen !== _loadFoldersGen) return false;
      // Stamp only a genuinely-online load: an offline cold start must keep
      // _onlineRevalidateLast=0 so the reconnect revalidation fires instead
      // of being eaten by the freshness gate.
      if (typeof navigator === 'undefined' || navigator.onLine) _onlineRevalidateLast = Date.now();
      _persistTreeSnapshot(data, {defer:true});
      // Shallow-warmup idle fill (plan T4): hand the full-tree ETag to the
      // render.js poll loop so background-filled archive covers land without
      // a reload. The filled tree re-enters via restoreTree (existing path).
      try {
        var _fillPollEtag = null;
        try { _fillPollEtag = (res.headers && res.headers.get) ? res.headers.get("ETag") : null; } catch(_fillPollEtagErr) {}
        if (window.FillPoll) window.FillPoll.noteFullLoad({etag: _fillPollEtag, onTree: function(t, rawText, rawOpts){
          if (typeof rawText === "string"){ try { _rawFoldersText.set(t, {text: rawText, treeEnvelope: !!(rawOpts && rawOpts.treeEnvelope)}); } catch(_fillPollSetErr) {} }
          restoreTree(t, {shallow:false}).then(function(ok){
            if (ok) _persistTreeSnapshot(t);
          }).catch(function(e){ console.warn(e); });
        }});
      } catch(e) { console.warn(e); }
      return true;
    };
    // 202 = the server index is still building: the shallow grid already
    // painted, so retry the bare fetch on a short clock until the build
    // lands instead of parking a multi-second request or falling back to
    // the stale snapshot. Bounded so a wedged build can't spin forever.
    let _fullRetry = 0;
    const _scheduleFullRetry = function(){
      if (++_fullRetry > 40) return;
      setTimeout(async function(){
        if (_loadGen !== _loadFoldersGen) return;
        try {
          const res2 = await _fetch(globalThis.BookqApi.endpoints.tree, {credentials: 'same-origin'});
          if (_loadGen !== _loadFoldersGen) return;
          if (res2.status === 202){ _scheduleFullRetry(); return; }
          if (!res2.ok) throw new Error("http "+res2.status);
          await _applyFullRes(res2);
        } catch(e){ console.warn("full folders retry failed", e); _scheduleFullRetry(); }
      }, Math.min(1500 * _fullRetry, 5000));
    };
    try {
      const res = await _fetch(globalThis.BookqApi.endpoints.tree, {credentials: 'same-origin'});
      if (_loadGen !== _loadFoldersGen) return;
      if (res.status === 202){
        _scheduleFullRetry();
        if (shallowOk){ updateShallowBadge(); return; }
        // No shallow paint either — fall through to the recovery path so a
        // stale snapshot (if any) shows while the retry keeps working.
        throw new Error("index building");
      }
      if (!res.ok) throw new Error("http "+res.status);
      await _applyFullRes(res);
    } catch(e){
      // Recovery work is only valid while this load generation still owns the
      // pipeline — a newer loadFolders must never see stale marks/renders.
      if (_loadGen !== _loadFoldersGen) return;
      if (shallowOk){
        // Full failed after a shallow paint: stay on the shallow grid with
        // the waiting badge — treeLoaded stays false so neither prefetch nor
        // applyUrlState can leak a half tree into the viewer.
        updateShallowBadge();
        return;
      }
      let recovered = false;
      try {
        const snap = await window.OfflineTree.getTree();
        if (_loadGen !== _loadFoldersGen) return;
        if (Array.isArray(snap) && snap.length>0) {
          // A false return means this load was superseded mid-restore — bail
          // instead of treating it as recovered (no mark/render below).
          if (!await restoreTree(snap)) return;
          recovered = true;
        }
      } catch(e) { console.warn(e); }
      if (!recovered) {
        if (_loadGen !== _loadFoldersGen) return;
        if (tree && tree.length) {
          try { await refreshOfflineMap(); } catch(e) { console.warn(e); }
          if (_loadGen !== _loadFoldersGen) return;
          renderView();
          markTreeReady();
          // Stale tree rendered — resolve so applyUrlState() still restores the view.
        } else {
          tileGrid.innerHTML = '<div style="color:#c44;padding:20px;font-size:13px">フォルダ一覧を読み込めませんでした</div>';
          throw e;
        }
      }
    }
  }

  function findNodeByPath(nodes, targetPath){
	    for (const node of nodes){
	      if (node._fullPath === targetPath) return node;
	      if (node.children && targetPath.startsWith(node._fullPath + "/")){
	        const found = findNodeByPath(node.children, targetPath);
	        if (found) return found;
	      }
	    }
	    return null;
	  }

  function getParentPath(path){
    // Return the PARENT folder's _fullPath for the node at path, or null when
    // path is null / not found / a top-level node. Tree walk mirroring
    // findNodeByPath — NEVER string-split the path: flattened child names like
    // "wrap/inner/vol1" contain "/" and segment-pop would emit a dead path.
    if (!path) return null;
    return findParentPath(tree, path, null);
  }

  function findParentPath(nodes, targetPath, parentPath){
    for (const node of nodes){
      if (node._fullPath === targetPath) return parentPath;
      if (node.children && targetPath.startsWith(node._fullPath + "/")){
        const found = findParentPath(node.children, targetPath, node._fullPath);
        if (found !== null) return found;
      }
    }
    return null;
  }

  // Browse-level parent of a _fullPath: getParentPath tree walk when the node
  // is live, else the nearest live ancestor. NEVER split("/") — flattened node
  // names like "wrap/inner" make the textual parent a dead path, and callers
  // that fell back to null on a dead parent jumped to root instead of one
  // level up.
  function browseParentOf(path){
    const node = path ? getNodeAt(path) : null;
    return node ? getParentPath(path) : resolveAliveAncestor(path);
  }
  try { if (typeof window !== 'undefined'){ window.getParentPath = getParentPath; window.findNodeByPath = findNodeByPath; window.getSiblingLeafFolders = getSiblingLeafFolders; window.getBrowseChildren = getBrowseChildren; window.buildNodeMap = buildNodeMap; window.setFullPathsAndIndex = setFullPathsAndIndex; window.setFullPathsAndIndexAsync = setFullPathsAndIndexAsync; window.emptyBrowseMessage = emptyBrowseMessage; window.progressBarHtml = progressBarHtml; window._coverUrl = _coverUrl; } } catch(e) { console.warn(e); }


  // ── Search / Filter / Facets ──
  // search/facets.js (window.SearchFacets) owns facet loading and search,
  // including the !res.ok HTTP-error handling. app.js only delegates.
  async function loadFacets(path, deps) { return window.SearchFacets.loadFacets(path, deps); }
  async function doSearch(deps) { return window.SearchFacets.doSearch(deps); }

  // ── H4(a): keyboard activation of browse items ──
  // Tiles/cards/list-items are focusable divs (tabindex=0 role=button); the
  // click handlers live per-render on the elements, so Enter/Space just
  // re-dispatches a click on the focused item. Space must preventDefault or
  // the page scrolls while "pressing" the item.
  tileGrid.addEventListener("keydown", e => {
    if (e.key !== "Enter" && e.key !== " ") return;
    // .offline-btn and .ux-pop buttons are real <button>s inside the tile:
    // let their native Enter/Space activation fire the click (delegated
    // listener / popover handlers) instead of re-clicking the whole tile.
    if(e.target.closest && e.target.closest('.offline-btn, .ux-pop, .visibility-toggle')) return;
    const item = e.target.closest(".tile, .card, .item[data-path]");
    if (!item) return;
    e.preventDefault(); // every repeat too, or held Space scrolls the page
    if (e.repeat) return; // native buttons don't re-activate on held-key OS repeat
    item.click();
  });

  // ── Filter chips / Bottom sheet ──
  const filterChips = document.getElementById("filter-chips");
  const bottomSheet = document.getElementById("bottom-sheet");
  const sheetOverlay = document.getElementById("bottom-sheet-overlay");
  const filterBtn = document.getElementById("filter-btn");
  const sheetCloseBtn = document.getElementById("bottom-sheet-close");
  const filterResetBtn = document.getElementById("filter-reset-btn");
  const filterApplyBtn = document.getElementById("filter-apply-btn");
  const sortRadios = document.querySelectorAll('input[name="sort"]');

  function updateFilterChips() {
    const chips = [];
    for (const artist of activeFilters.artist) {
      chips.push(`<span class="chip"><span class="chip-label">🎨${escapeHtml(artist)}</span><button class="chip-remove" data-filter="artist" data-value="${escapeHtml(artist)}" aria-label="フィルタを削除">✕</button></span>`);
    }
    for (const tag of activeFilters.tag) {
      chips.push(`<span class="chip"><span class="chip-label">🏷️${escapeHtml(tag)}</span><button class="chip-remove" data-filter="tag" data-value="${escapeHtml(tag)}" aria-label="フィルタを削除">✕</button></span>`);
    }
    if (chips.length > 0) {
      filterChips.innerHTML = chips.join('');
      filterChips.classList.remove("hidden");
      filterChips.querySelectorAll(".chip-remove").forEach(btn => {
        btn.addEventListener("click", e => {
          e.stopPropagation();
          const filter = btn.dataset.filter;
          const value = btn.dataset.value;
          if (filter && value) {
            activeFilters[filter] = activeFilters[filter].filter(v => v !== value);
            updateFilterChips();
            clearTimeout(searchTimer);
            searchTimer = setTimeout(doSearch, 300);
          }
        });
      });
    } else {
      filterChips.classList.add("hidden");
    }
  }

  function renderFacetOptions() {
    // Artists
    const artistsContainer = document.querySelector("#facet-artists .facet-chips");
    if (artistsContainer) {
      artistsContainer.innerHTML = (facets.artists || []).map(a => {
        const active = activeFilters.artist.includes(a) ? ' active' : '';
        return `<span class="chip${active}" data-facet="artist" data-value="${escapeHtml(a)}" tabindex="0" role="button" aria-pressed="${active ? 'true' : 'false'}">${escapeHtml(a)}</span>`;
      }).join('') || '<span style="color:#555;font-size:12px">No artists</span>';
      artistsContainer.querySelectorAll('.chip').forEach(el => {
        el.addEventListener('click', () => {
          const val = el.dataset.value;
          const facet = el.dataset.facet;
          if (!val || !facet) return;
          const idx = activeFilters[facet].indexOf(val);
          if (idx >= 0) activeFilters[facet].splice(idx, 1);
          else activeFilters[facet].push(val);
          el.classList.toggle('active');
          el.setAttribute('aria-pressed', el.classList.contains('active'));
        });
      });
    }
    // Tags
    const tagsContainer = document.querySelector("#facet-tags .facet-chips");
    if (tagsContainer) {
      tagsContainer.innerHTML = (facets.tags || []).map(t => {
        const active = activeFilters.tag.includes(t) ? ' active' : '';
        return `<span class="chip${active}" data-facet="tag" data-value="${escapeHtml(t)}" tabindex="0" role="button" aria-pressed="${active ? 'true' : 'false'}">${escapeHtml(t)}</span>`;
      }).join('') || '<span style="color:#555;font-size:12px">No tags</span>';
      tagsContainer.querySelectorAll('.chip').forEach(el => {
        el.addEventListener('click', () => {
          const val = el.dataset.value;
          const facet = el.dataset.facet;
          if (!val || !facet) return;
          const idx = activeFilters[facet].indexOf(val);
          if (idx >= 0) activeFilters[facet].splice(idx, 1);
          else activeFilters[facet].push(val);
          el.classList.toggle('active');
          el.setAttribute('aria-pressed', el.classList.contains('active'));
        });
      });
    }
    // Sort radios
    sortRadios.forEach(radio => {
      radio.checked = (radio.value === activeFilters.sort);
    });
    filterBtn.classList.toggle('active',
      activeFilters.artist.length > 0 || activeFilters.tag.length > 0);
  }

  function openFilterSheet() {
    document.querySelectorAll(".facet-chips").forEach(c => { c.innerHTML = ""; });
    // In search mode /api/search applies filters library-wide, so the facet
    // sheet must list whole-library options — not whatever folder was last
    // browsed (a stale currentBrowsePath would scope options to its subtree).
    loadFacets(searchMode ? "" : currentBrowsePath);
    setInert(bottomSheet, false);
    // H4(c): aria-modal=true promises the background isn't reachable while
    // the sheet is open — the sidebar sits behind it (H1 keeps it open), so
    // it must go inert too, or Tab walks straight out of the "dialog".
    setInert(sidebar, true);
    sheetOverlay.classList.remove("hidden");
    sheetOverlay.classList.add("visible");
    bottomSheet.classList.remove("hidden");
    // Trigger layout then slide in
    requestAnimationFrame(() => {
      bottomSheet.classList.add("open");
      sheetCloseBtn.focus();
    });
  }

  function closeFilterSheet() {
    bottomSheet.classList.remove("open");
    // H4(b): the sheet leaves the tab order / a11y tree as soon as it starts
    // closing — the .hidden class is applied 350ms later on the slide-out.
    setInert(bottomSheet, true);
    // Restore whatever inert state the sidebar's own open/closed class
    // implies (it was forced inert in openFilterSheet regardless of that).
    syncSidebarA11y();
    filterBtn.focus();
    sheetOverlay.classList.remove("visible");
    setTimeout(() => {
      bottomSheet.classList.add("hidden");
      sheetOverlay.classList.add("hidden");
    }, 350);
  }

  filterBtn.addEventListener("click", e => { e.stopPropagation(); openFilterSheet(); });
  sheetCloseBtn.addEventListener("click", closeFilterSheet);
  sheetOverlay.addEventListener("click", closeFilterSheet);

  // H4(a): keyboard activation of facet chips — the chips' own click handlers
  // are attached per-render, so Enter/Space re-dispatches a click on the
  // focused chip. Space must preventDefault or the page scrolls.
  bottomSheet.addEventListener("keydown", e => {
    if (e.key !== "Enter" && e.key !== " ") return;
    const chip = e.target.closest(".chip[data-facet]");
    if (!chip) return;
    e.preventDefault(); // every repeat too, or held Space scrolls the page
    if (e.repeat) return; // native buttons don't re-activate on held-key OS repeat
    chip.click();
  });

  filterResetBtn.addEventListener("click", () => {
    activeFilters = { artist: [], tag: [], sort: 'relevance' };
    sortRadios.forEach(r => r.checked = r.value === 'relevance');
    renderFacetOptions();
    updateFilterChips();
    if (searchMode) {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(doSearch, 300);
    } else {
      renderView();
    }
  });

  filterApplyBtn.addEventListener("click", () => {
    // Read sort radio
    sortRadios.forEach(r => { if (r.checked) activeFilters.sort = r.value; });
    closeFilterSheet();
    updateFilterChips();
    if (searchMode) {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(doSearch, 300);
    } else {
      renderView();
    }
  });

  // ── unified render ──
  // opts.scrollAnchor: undefined = 位置を決めない（再描画・モード切替・検索）、
  // null = 上端、path = そのタイルを上端に。指定できるのは
  // navigateToFolder / restoreBrowse / resetToTop の 3 経路だけ（設計 §3.1）。
  function renderView(opts){
    // 判定不能（loading / error 無snapshot）の一覧ブロックは renderer 内の
    // blockedGridHtml 門が担う — renderBrowse 直叩き経路も同じ門を通る。
    tileGrid.classList.remove("list-mode", "card-mode");
    sidebar.classList.remove("tile-full");
    viewer.classList.remove("tile-full");
    sidebar.classList.toggle("tile-full", viewMode !== "list");
    viewer.classList.toggle("tile-full", viewMode !== "list");
    tileGrid.classList.toggle("list-mode", viewMode === "list");
    tileGrid.classList.toggle("card-mode", viewMode === "card");
    window.BrowseRender.renderBrowse({
      mode: viewMode,
      results: searchMode ? searchResults : null,
      scrollAnchor: (opts && 'scrollAnchor' in opts) ? opts.scrollAnchor : undefined,
    });
    updateResumeButton();
  }
  window.renderView = renderView;
  try {
    window.getNodeAt = getNodeAt;
    window.getParentPath = getParentPath;
    window.findNodeByPath = findNodeByPath;
    window.resetStrip = resetStrip;
    window.teardownViewer = teardownViewer;
    window.updateBrowseHeader = updateBrowseHeader;
    window.syncSidebarA11y = syncSidebarA11y;
  } catch(e) { console.warn(e); }
  try {
    Object.defineProperty(window, 'searchMode', {get(){return searchMode;}, set(v){searchMode=v;}, configurable:true});
    Object.defineProperty(window, 'searchResults', {get(){return searchResults;}, set(v){searchResults=v;}, configurable:true});
    Object.defineProperty(window, 'activeFilters', {get(){return activeFilters;}, set(v){activeFilters=v;}, configurable:true});
    Object.defineProperty(window, 'facets', {get(){return facets;}, set(v){facets=v;}, configurable:true});
    Object.defineProperty(window, 'facetLoadGen', {get(){return facetLoadGen;}, set(v){facetLoadGen=v;}, configurable:true});
    Object.defineProperty(window, 'searchTimer', {get(){return searchTimer;}, set(v){searchTimer=v;}, configurable:true});
  } catch(e) { console.warn(e); }

  // ── cross-chapter helpers ──

  // Structural parent search (handles scanner.py flattened chains)
  function findParentOf(nodes, targetFullPath, parent) {
    for (const node of nodes) {
      if (node._fullPath === targetFullPath) return parent;
      if (node.children) {
        const found = findParentOf(node.children, targetFullPath, node);
        if (found !== undefined) return found;
      }
    }
    return undefined;
  }

  // Physical directory depth of a folder relative to the downloads root,
  // counted from the raw path rather than the post-flatten tree: scanner.py's
  // single-child flattening merges wrapper folders into a node's name (e.g.
  // "wrap/inner/vol1"), so a real multi-chapter series can end up looking
  // shallow in the tree even though its raw path is deep. fullPath always
  // preserves every physical segment, flattened or not.
  //
  // Folders at depth 0 or 1 are broad groupings (e.g. an author or category
  // folder) whose leaf children are usually unrelated one-shot works, not
  // chapters of the same series -- concatenating them would merge unrelated
  // works. A real multi-chapter series' own chapters live one level deeper
  // (under a folder dedicated to that series), so this only suppresses the
  // shallow case.
  const SIBLING_AUTOLOAD_MIN_DEPTH = 2;

  // Get sibling leaf folders (chapters) with natural sort
  try { if (typeof window !== 'undefined' && typeof SIBLING_AUTOLOAD_MIN_DEPTH !== 'undefined') window.SIBLING_AUTOLOAD_MIN_DEPTH = SIBLING_AUTOLOAD_MIN_DEPTH; } catch(e) { console.warn(e); }
  function getSiblingLeafFolders(fullPath) {
    if (fullPath.split("/").length - 1 < SIBLING_AUTOLOAD_MIN_DEPTH) {
      const parts = fullPath.split("/");
      return [{ name: parts[parts.length - 1], fullPath }];
    }

    const parentNode = findParentOf(tree, fullPath, null);

    // If no parent (root level), return only current folder
    if (!parentNode) {
      const parts = fullPath.split("/");
      return [{ name: parts[parts.length - 1], fullPath }];
    }

    const siblings = parentNode.children;
    const leafFolders = (siblings || []).filter(n =>
      n.type === "folder" && folderHasOnlyImages(n)
    );
    if (leafFolders.length <= 1) {
      const parts = fullPath.split("/");
      return [{ name: parts[parts.length - 1], fullPath }];
    }
    return leafFolders
      .map(n => ({ name: n.name, fullPath: n._fullPath }));
  }

  // ── transient toast (S-F2) — role=alert live region, auto-dismiss ──
  // Variant classes are styled in style.css: .err = red error, .ok = neutral emphasis.
  let toastTimer = null;
  function showToast(message, variant){
    const toast = document.getElementById("toast");
    if (!toast) return;
    toast.textContent = message;
    toast.classList.remove("err", "ok");
    if (variant) toast.classList.add(variant);
    toast.classList.add("visible");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove("visible"), 5000);
  }
  function showErrorToast(message){ showToast(message, "err"); }
  function showToastSuccess(message){ showToast(message, "ok"); }
  try { window.showToast = showToast; window.showErrorToast = showErrorToast; window.showToastSuccess = showToastSuccess; } catch(e) { console.warn(e); }

  // Reset to a navigable root browse state — shared by the empty-images
  // branch and the permanent-failure branch of loadImages' catch block below,
  // so the reset invariant can't drift between the two copies.
  function resetToNavigableBrowseRoot(){
    clearViewerState();
    urlRestoring = false;
    currentFolder = null; chapterList = []; chapterBoundaries = [];
    currentBrowsePath = null; urlStackReplace(location.pathname + location.search);
    searchMode = false;
    const si = document.getElementById("search-input");
    if (si) si.value = "";
    sidebar.classList.add("open"); viewer.classList.add("sidebar-open");
    syncSidebarA11y();
    renderView();
  }

  // 非表示対象への移動・閲覧要求の共通拒否: トップへ静かに戻す。
  // 理由表示・全表示への誘導は出さない。
  function redirectHiddenToTop(){
    // 1) in-flight の非同期ロード・復元を世代で無効化
    try { urlRestoreGen++; urlRestoring = false; } catch(e){}
    try { window.facetLoadGen++; } catch(e){}
    try { if (window.SearchFacets && typeof window.SearchFacets.invalidateSearch === "function") window.SearchFacets.invalidateSearch(); } catch(e){}
    // 2) shallow/判定待ちキュー・検索状態をクリア
    _pendingNav = null;
    searchMode = false; searchResults = [];
    clearTimeout(searchTimer);
    if (searchInput) searchInput.value = "";
    // 3) viewer を先に teardown — PairedView の solo 復元 hook が解除時に
    //    隠した画像を再生成しないよう pairStrip を先に捨てる
    teardownViewer();
    // 4) ペアがあれば通常の LEAVE 経路で解除（資格情報削除・pending 解除は既存処理）
    try { if (window.PairController && typeof window.PairController.leave === "function") window.PairController.leave(); } catch(e){}
    // 5) 既存 resetToTop でトップ一覧・上端スクロールへ
    resetToTop();
    // 6) URL を canonical なトップへ replace（history entry は増やさない）。
    //    list/card 中に裸 URL へ戻すと mode と URL がずれる — browseHash で
    //    現在 viewMode の suffix を維持する（tile では空文字＝従来通り）。
    try {
      var _topUrl = location.pathname + location.search + browseHash("", viewMode);
      history.replaceState(null, "", _topUrl);
      urlStackReplace(_topUrl);
    } catch(e){}
    // 7) ペア解除がサイドバー状態を触る可能性があるため最終状態をここで確定
    sidebar.classList.add("open"); viewer.classList.add("sidebar-open");
    syncSidebarA11y();
  }
  try { window.redirectHiddenToTop = redirectHiddenToTop; } catch(e) { console.warn(e); }

  async function loadImages(fullPath){
    var FV = window.FolderVisibility;
    if (FV){
      // 判定不能（loading / error 無snapshot）は open をキュー — ready/
      // readonly 遷移時に drain される。error も同じく保留する（隠れて
      // いるかもしれない本を開かない）。hidden だけ静かにトップへ。
      var _g = FV.guardPath(fullPath);
      if (_g === "pending"){
        _pendingNav = {kind: "view", path: fullPath};
        const _emp = document.getElementById("empty");
        if (_emp){
          _emp.innerHTML = (FV.readyState === "error") ? "<p>取得失敗 — 再試行してください</p>" : "<p>読込中…</p>";
          _emp.style.display = "flex";
        }
        return;
      }
      if (_g === "hidden"){ redirectHiddenToTop(); return; }
    }
    if (_shallowMode){
      // Contract: never run on a children==[] shallow node — queue until the
      // full tree lands (the hash stays the queue for deep-links; clicks
      // stash here) and reuse the existing #empty waiting div.
      _pendingNav = {kind: "view", path: fullPath};
      const _empty = document.getElementById("empty");
      if (_empty){ _empty.innerHTML = "<p>読込中…</p>"; _empty.style.display = "flex"; }
      return;
    }
    // 別の本を開いた＝ビューア復帰の文脈は切れた（一覧のアンカーには使わない）
    try { window.BrowseScroll.clearViewerReturn(); } catch(e) { console.warn(e); }
    // Opening a different work ends a paired view (LEAVE-equivalent): the new
    // book rebuilds the strip from scratch, so no solo-state restore needed.
    if (pairStrip){
      pairStrip = null;
      window.PairedView.clear();
      imgContainer.classList.remove("pair-mode");
      imgContainer.classList.remove("pair-blank");
    }
    currentFolder = fullPath;
    const myGen = ++urlRestoreGen;      // every loadImages invocation claims the newest restore token
    // A load starting while the overview is open (deep link, sidebar pick)
    // would leave the previous book's grid on screen over the new one.
    window.Overview.close();
    const entrySt = parseViewerHash();  // the URL the load STARTED from — decides replace vs push at success-end
    const wasRestore = urlRestoring;    // a deep-link restore had this set by applyUrlState a microtask earlier
    urlRestoring = false;               // a direct user open clears the restore intent
    currentIdx = 0;
    images = [];
    resetStrip();
    // レイアウト設定は本をまたいで引き継がない（決定 Q17）。URL 復元時だけ URL の値を採る。
    {
      const fromUrl = wasRestore && entrySt && entrySt.folder === fullPath;
      landscapeSpread = fromUrl && entrySt.s === 1 ? false : true;
      spreadOffset = fromUrl && entrySt.o === 1 ? 1 : 0;
    }
    updateOrientation(true); // force: URL wins over the last-known viewport size (決定 Q17)
    folderName.textContent = fullPath;
    counter.textContent = "";
    empty.style.display = "none";
    imgContainer.classList.add("loading");
    spinner.style.display = "block";
    pageSliderWrap.classList.remove("visible");
    closeSidebar();

    try {
      const siblings = getSiblingLeafFolders(fullPath);
      // Mixed-folder fallback: a folder with its own loose images that is not
      // a leaf chapter (e.g. v02 = loose m1..m3 + sub/) is absent from the
      // hasOnly sibling list, so cross-chapter mode would clamp it to chapter 0
      // (clickedChapterIdx === -1 below). Read its direct loose images as its
      // own single chapter instead ({name, folder} items, leaf chapter name,
      // boundaries=[0]) — tree direct image children only, no fetch, no walk.
      const selfNode = getNodeAt(fullPath);
      const isSelfMixed = !!(selfNode && folderHasImages(selfNode) && !folderHasOnlyImages(selfNode));
      const isCrossChapter = siblings.length > 1 && !isSelfMixed;

      if (isCrossChapter) {
        // Cross-chapter mode: concatenate all sibling chapter images
        const allImages = [];
        const boundaries = [];
        const chapters = [];

        for (const sib of siblings) {
          boundaries.push(allImages.length);
          const node = findNodeByPath(tree, sib.fullPath);
          if (node && folderHasOnlyImages(node)) {
            const chapterImages = node.children.map(c => ({
              name: c.name,
              folder: sib.fullPath
            }));
            chapters.push({ name: sib.name, fullPath: sib.fullPath, images: chapterImages });
            allImages.push(...chapterImages);
          }
        }

        // Offset-aware spacer padding for spread mode
        const spreadOff = pageMode === 'spread' ? spreadOffset : 0;
        const spacersNeeded = [];
        for (let c = 0; c < chapters.length; c++) {
          const startIdx = boundaries[c] + spacersNeeded.reduce((a, b) => a + b, 0);
          const pad = (startIdx - spreadOff + 2) % 2;
          spacersNeeded.push(pad);
          for (let p = 0; p < pad; p++) {
            allImages.splice(boundaries[c], 0, {name: "__spacer__", folder: "", isSpacer: true});
            for (let j = c + 1; j < boundaries.length; j++) boundaries[j]++;
          }
          boundaries[c] += pad;
        }

        images = allImages;
        chapterList = chapters;
        chapterBoundaries = boundaries;
        // Chapter divider labels, windowed like strip cells: only chapters
        // near the opened one get a label. All 130 labels of a huge book
        // would span the full book width (~1.4M px here) inside #img-track,
        // ballooning its composited layer and slowing every slide on a
        // tile-GPU device (#27). Distant labels are hundreds of thousands of
        // px off-viewport, so windowing is display-identical; the per-turn
        // window job below re-syncs the window as chapters change. Created
        // after ccIdx below (needs the clamped chapter index).
      } else {
        // Single-folder mode (backward compatible)
        const node = findNodeByPath(tree, fullPath);
        if (node && folderHasOnlyImages(node)){
          images = node.children.map(c => ({name: c.name, folder: fullPath}));
        } else if (isSelfMixed) {
          // Mixed direct-only: the folder's own loose images are exactly its
          // direct image children — no /api/images fetch, no recursive walk,
          // no dedupe (tree shape is backend-uniform, and
          // /image/<folder>/<basename> serves each entry).
          images = selfNode.children
            .filter(c => c.type === "image")
            .map(c => ({name: c.name, folder: fullPath}));
        } else {
          const res = await fetch(globalThis.BookqApi.imagesUrl(fullPath));
          if (!res.ok) {
            const err = new Error("Failed to load folder (HTTP " + res.status + ")");
            err.status = res.status;
            throw err;
          }
          const filenames = globalThis.BookqApi.imagesOf(await res.json());
          if (!filenames) {
            const err = new Error("Malformed folder response");
            err.status = 0;
            throw err;
          }
          if (myGen !== urlRestoreGen) return; // superseded by a newer load — touch nothing
          images = filenames.map(name => ({name, folder: fullPath}));
        }
        chapterList = [{ name: fullPath.split("/").pop(), fullPath, images }];
        chapterBoundaries = [0];
      }

      // await 後の再判定: 取得中に通常モードで禁止対象になった本は開かない
      // （positive hidden のみ — error/unknown では既に開いた読書を壊さない）
      if (FV && !FV.showAll && FV.isHidden(fullPath)){ redirectHiddenToTop(); return; }
      // Build spacer-mapping arrays
      realToGlobalIdx = [];
      globalToRealIdx = [];
      spacerCount = 0;
      for (let g = 0; g < images.length; g++) {
        if (images[g].isSpacer) {
          globalToRealIdx.push(-1);
          spacerCount++;
        } else {
          globalToRealIdx.push(g - spacerCount);
          realToGlobalIdx.push(g);
        }
      }
      realImageCount = images.length - spacerCount;

      if (images.length > 0) {
        rebuildSpreads();
        const clickedChapterIdx = chapterList.findIndex(c => c.fullPath === fullPath);
        const urlState = parseViewerHash();
        const saved = (urlState && urlState.folder === fullPath) ? { page: urlState.page } : ReadingProgress.get(fullPath);
        const ccIdx = clickedChapterIdx >= 0 ? clickedChapterIdx : 0; // URL feature makes -1 reachable (deep link to a non-leaf sibling); clamp to first chapter
        ensureChapterLabels(ccIdx);
        const chapterImgs = chapterList[ccIdx] ? chapterList[ccIdx].images : [];
        // saved.page is the chapter-local index measured from the chapter's
        // first REAL page — same semantics as ReadingProgress and as encoded by
        // currentViewerState (getCurrentChapterInfo's localIdx). chapterBoundaries
        // always point past any leading spacer (the padding loop above does
        // boundaries[c] += pad), so localIdx 0 is never a spacer and the valid
        // range is [0, chapterImgs.length - 1]; out-of-range falls back to 0.
        const chapterStart = chapterBoundaries[ccIdx];
        const savedLocalIdx = (saved && saved.page >= 0 && saved.page < chapterImgs.length) ? saved.page : 0;
        const startGlobalIdx = chapterStart + savedLocalIdx;
        currentIdx = startGlobalIdx;
        syncChapterSlider(startGlobalIdx, {resetMax:true});
        pageSliderWrap.classList.add("visible");
        urlSyncPaused = true;
        try {
          snapTo(startGlobalIdx, false); // initial open — the URL write is done below
        } finally {
          urlSyncPaused = false; // can't stick true even if snapTo internals throw
        }
        showHint();
        // Build the overview grid while the reader looks at page 1 — G then
        // opens instantly. overview.js owns the G key and Escape itself.
        // No `window.Overview &&` guard: it is a hard dependency loaded before
        // app.js in index.html's defer order (see test_offline_core_unit).
        window.Overview.prebuild();
        if (myGen !== urlRestoreGen) { /* unreachable — the continuation guard already returned; keep as a backstop */ }
        else {
          // NOTE: entrySt is the capture from loadImages ENTRY — never re-parse
          // here, or urlStayed is always true and the race guard is disabled.
          const st = parseViewerHash(); const cur = currentViewerState();
          const urlStayed = (!entrySt && !st) || (entrySt && st && entrySt.folder === st.folder);
          if (wasRestore && st && cur && urlStayed) { replaceViewerState(); }
          else if (urlStayed) { pushViewerState(); }
          else { /* URL moved during the load — a newer navigation owns the URL */ }
        }
      } else {
        if (myGen !== urlRestoreGen) return; // a superseded load must not mutate state
        empty.innerHTML = "<p>📭 このフォルダに画像はありません</p>";
        empty.style.display = "flex";
        imgContainer.classList.remove("loading");
        spinner.style.display = "none";
        // The gen guard above makes this the newest load, so the hash is ours:
        // clear it unconditionally — a hash still naming the PREVIOUS book would
        // lie about the browse/error state now on screen. The no-hash URL is
        // canonical root, so the browse level/stack and any search must reset
        // too (stale-stack hazard) — see resetToNavigableBrowseRoot().
        resetToNavigableBrowseRoot();
      }
    } catch(e){
      if (myGen !== urlRestoreGen) return; // a superseded load must not mutate state
      empty.innerHTML = "<p>⚠️ 画像を読み込めませんでした</p>";
      empty.style.display = "flex";
      imgContainer.classList.remove("loading");
      spinner.style.display = "none";
      const status = e && e.status;
      // 404/401/403 are permanent for this URL — the folder is gone, the
      // session is dead, or access is forbidden — no in-app retry can ever
      // succeed, so reset to a navigable state instead of stranding the user
      // on a dead error screen.
      if (status === 404 || status === 401 || status === 403) {
        const messages = {
          404: "フォルダが見つかりません (404)",
          401: "セッションが切れました。再読み込みしてください (401)",
          403: "アクセスが拒否されました (403)",
        };
        showErrorToast(messages[status]);
        resetToNavigableBrowseRoot();
      } else {
        // Transient failure (network / 5xx / malformed body): notify and keep
        // the error state + URL — a reload or retry can still recover. Drop
        // the previous book's chapter state so the error state is internally
        // consistent (images is already []); currentFolder still names the
        // failed folder and the hash is left in place.
        chapterList = []; chapterBoundaries = [];
        if (status === undefined) {
          // fetch() itself threw — a real network failure.
          showErrorToast("ネットワークエラーです。もう一度お試しください");
        } else if (status === 0) {
          // Response arrived but its body wasn't the expected shape.
          showErrorToast("予期しない応答です。もう一度お試しください");
        } else {
          showErrorToast("サーバーエラー (" + status + ")");
        }
      }
    }
  }
  try { window.loadImages = loadImages; } catch(e) { console.warn(e); }

  // ── helpers ──
  function getCurrentChapterInfo(globalIdx) {
    let chapterIdx = 0;
    for (let i = 1; i < chapterBoundaries.length; i++) {
      if (globalIdx >= chapterBoundaries[i]) chapterIdx = i;
      else break;
    }
    const chapter = chapterList[chapterIdx];
    const localIdx = globalIdx - chapterBoundaries[chapterIdx];
    return { chapterIdx, chapterName: chapter ? chapter.name : "", localIdx };
  }
  try { window.getCurrentChapterInfo = getCurrentChapterInfo; } catch(e) { console.warn(e); }

  function getUrl(idx){
    // Encode per path segment — image names may carry flattened "sub/dir/" prefixes.
    const img = images[idx];
    // !img: a once-listener (load/error) on a pooled <img> can outlive the
    // state array it indexed into — a paired teardown/loadImages that swaps
    // `images` mid-flight must not turn the stale event into a TypeError.
    if (!img || img.isSpacer) return "";
    return window.OfflineCore.readerUrl(img.folder, img.name);
  }
  try { window.getUrl = getUrl; } catch(e) { console.warn(e); }

  function updateStripPositions(){
    for (const k in spreadEls){
      const s = Number(k);
      const el = spreadEls[s];
      const spread = spreads[s];
      if (!spread) continue;
      const m = spreadMetrics(s);
      el.style.width = m.width + "px";
      el.style.height = m.height + "px";
      el.style.left = spreadLeft(s) + "px";
      const renderPages = renderPagesForSpread(s);
      renderPages.forEach((idx, i) => {
        const cell = stripCells[idx];
        if (cell){
          cell.style.width = m.pageWidths[i] + "px";
          cell.style.height = m.height + "px";
        }
      });
      const blankCell = pairBlankCells[s];
      if (blankCell){
        blankCell.style.width = (m.pageWidths[0] || m.width) + "px";
        blankCell.style.height = m.height + "px";
      }
      el.classList.toggle("pair-blank", !!(pairStrip && pairStrip.active && !renderPages.length));
    }
    // Chapter divider labels sit centred in the gap to the right of the
    // spread that opens the chapter (manga RTL: the previous chapter is to
    // the right). The gap itself may be wider than CHAPTER_GAP_PX (監査 V3
    // — gapBefore() widens it so neither neighbour peeks), so centre on the
    // actual computed gap, not the CHAPTER_GAP_PX floor. Labels are windowed
    // (ensureChapterLabels), so iterate the live set via its chapter index.
    for (const el of chapterLabels) {
      const ci = Number(el.dataset.chapterIdx);
      const s = spreadOf(chapterBoundaries[ci]);
      const m = spreadMetrics(s);
      el.style.left = (spreadLeft(s) + m.width + gapBefore(s) / 2) + "px";
    }
  }

  // Chapter-label windowing (see loadImages): chapters far from the page
  // being read have no visible label; keep the set in sync here, next to
  // the other per-turn window maintenance (evict/markCurrent).
  const LABEL_WINDOW = 2; // chapters each side of the current chapter
  function ensureChapterLabels(centerChapterIdx){
    if (chapterList.length <= 1) return;
    const lo = Math.max(1, centerChapterIdx - LABEL_WINDOW);
    const hi = Math.min(chapterList.length - 1, centerChapterIdx + LABEL_WINDOW);
    const want = new Set();
    for (let i = lo; i <= hi; i++) want.add(i);
    for (let i = chapterLabels.length - 1; i >= 0; i--) {
      const ci = Number(chapterLabels[i].dataset.chapterIdx);
      if (!want.has(ci)) { chapterLabels[i].remove(); chapterLabels.splice(i, 1); }
    }
    const have = new Set(chapterLabels.map(el => Number(el.dataset.chapterIdx)));
    for (let i = lo; i <= hi; i++) {
      if (!have.has(i)) {
        const label = document.createElement("div");
        label.className = "chapter-divider";
        label.textContent = chapterList[i].name;
        label.dataset.chapterIdx = String(i);
        track.appendChild(label);
        chapterLabels.push(label);
      }
    }
  }
  // Track transform that centers the spread containing page idx.
  // screenLeft(s) = translateX + spreadLeft(s), and we want that to equal
  // (viewW - width(s)) / 2.
  function pageOffsetPx(idx){
    const s = spreadOf(idx);
    return (viewW - spreadMetrics(s).width) / 2 - spreadLeft(s);
  }

  // ── strip image management: per-index page-cell + <img> lazily created ──

  function ensureSpreadEl(s){
    if (spreadEls[s]) return spreadEls[s];
    const el = document.createElement("div");
    el.className = "spread-container";
    el.style.gap = GAP_INNER + "px";
    spreadEls[s] = el;
    track.appendChild(el);
    return el;
  }

  // Cells are created lazily and in arbitrary order (scroll-back re-creates the
  // right page after the left one), so insert by position rather than append.
  function placeCell(cell, idx){
    const s = spreadOf(idx);
    // pos inside the spread = its RENDER position (paired: always 0 — one page).
    const pos = spreads[s] ? Math.max(0, renderPagesForSpread(s).indexOf(idx)) : 0;
    cell.dataset.pos = String(pos);
    const el = ensureSpreadEl(s);
    // Inverse of ensureBlankCell's sibling sweep: a spread gaining a real
    // page must drop its blank placeholder. pairBlankCells is keyed by
    // spread number, so after SWAP_SIDES flips this device's rendered side
    // the stale blank would otherwise mask the image cell indefinitely.
    const staleBlank = pairBlankCells[s];
    if (staleBlank){
      staleBlank.remove();
      delete pairBlankCells[s];
      el.classList.remove("pair-blank");
    }
    let ref = null;
    for (const child of el.children){
      if (Number(child.dataset.pos) > pos){ ref = child; break; }
    }
    el.insertBefore(cell, ref);
  }

  // Re-home every live cell after the spread grouping changes (1P⇄2P, cover
  // shift, orientation). Empties are dropped so no stale container lingers.
  function regroupCells(){
    for (const k of Object.keys(spreadEls)){
      spreadEls[k].remove();
      delete spreadEls[k];
    }
    for (const k of Object.keys(stripCells)) placeCell(stripCells[k], Number(k));
    // Blank cells live outside stripCells — re-home them the same way.
    for (const k of Object.keys(pairBlankCells)){
      const s = Number(k);
      pairBlankCells[s].remove();
      delete pairBlankCells[s];
      ensureBlankCell(s);
    }
  }

  // The blank-side placeholder: a distinguishable empty cell inside the spread
  // container — zero <img>, zero src, not the spinner and not an image error.
  // Paired only, and only for a spread this device renders nothing for.
  function ensureBlankCell(s){
    if (!(pairStrip && pairStrip.active)) return null;
    if (pairBlankCells[s]) return pairBlankCells[s];
    const cell = document.createElement("div");
    cell.className = "page-cell pair-blank";
    cell.dataset.pos = "0";
    const el = ensureSpreadEl(s);
    // A blank spread must never keep a leftover page cell beside it.
    for (const child of el.querySelectorAll(".page-cell:not(.pair-blank)")) child.remove();
    el.appendChild(cell);
    el.classList.add("pair-blank");
    pairBlankCells[s] = cell;
    return cell;
  }

  // Returns true when the measured aspect differs from what layout assumed.
  function recordAR(idx, img){
    if (!img.naturalWidth || !img.naturalHeight) return false;
    const ar = img.naturalWidth / img.naturalHeight;
    lastAR = ar;
    if (pageAR[idx] === ar) return false;
    pageAR[idx] = ar;
    delete spreadSizeCache[spreadOf(idx)];
    return true;
  }

  function ensureStripImage(idx){
    if (idx < 0 || idx >= images.length) return null;
    const imgData = images[idx];
    // Spacers exist only to fix spread parity at a chapter boundary; they are
    // dropped from `spreads`, so they get no DOM node at all.
    if (imgData && imgData.isSpacer) return null;
    // Paired: an index this device does not render (the peer's page, or any
    // page of a spread whose assigned side is blank) must never get image DOM
    // — that's what keeps the adjacent peer page off the viewport edge.
    if (pairStrip && pairStrip.active && !pairStrip.renderSet.has(idx)) return null;
    if (stripImgs[idx]) return stripImgs[idx];
    const cell = document.createElement("div");
    cell.className = "page-cell";
    // Acquire from pool instead of creating a new <img> — eliminates GC
    // pressure from repeated create/remove cycles during page turns.
    const img = imagePool.acquire();
    img.style.setProperty("--i", String(idx));
    // Both listeners check that this <img> still belongs to `idx`. They are
    // {once:true}, but an image released to imagePool before it ever loaded
    // (a deferred cell evicted before its fetch started, or an in-flight fetch
    // cut by src="") keeps its pending `load` listener; the pool then hands
    // the same element to another page and the next successful load fires the
    // stale closure too — recordAR(oldIdx, img) would write the NEW page's
    // aspect ratio into pageAR[oldIdx] and drop the wrong spread's size cache.
    img.addEventListener("load", () => {
      if (stripImgs[idx] !== img) return;   // recycled: this load belongs to another page now
      // Size before revealing, so a corrected aspect never flashes at the reader.
      if (recordAR(idx, img)){
        updateStripPositions();
        // Only the anchor's own size shifts the view; neighbours are absorbed by
        // the relative positioning. Never touch the transform mid-slide — that
        // would cancel the page turn; settle() re-applies it.
        if (!sliding && spreadOf(idx) === anchorSpread) applyTrackTransform(currentIdx, 0, false);
      }
      img.classList.add("loaded");
      // Freshly fetched and still adjacent — decode now rather than letting the
      // first post-turn raster pay it (Gecko decodes lazily at paint).
      if (Math.abs(idx - currentIdx) <= 2) predecode(img);
    }, {once:true});
    img.addEventListener("error", () => { if (stripImgs[idx] === img) img.classList.add("loaded"); }, {once:true});
    // Fetch priority by distance: the page the reader will see next must not
    // share bandwidth equally with the seven pages behind it. Without this, a
    // fast forward tap lands on a still-downloading page (previous pages are
    // already cached, which is why "next" felt slower than "previous").
    img.fetchPriority = Math.abs(idx - currentIdx) <= 1 ? "high" : "low";
    // Staged prefetch: only the current page and its immediate neighbours start
    // fetching right away. The rest of the window gets its src from
    // flushDeferredFetches() once the NEXT page has landed (or after a short
    // fallback), so a forward tap never waits behind seven far-ahead pages.
    if (Math.abs(idx - currentIdx) <= 1) img.src = getUrl(idx);
    else img.dataset.deferredSrc = getUrl(idx);
    cell.appendChild(img);
    stripImgs[idx] = img;
    stripCells[idx] = cell;
    placeCell(cell, idx);
    return img;
  }
  function isImgReady(img){ return !!(img && img.complete && img.naturalWidth > 0); }
  // Kick an off-critical-path decode ahead of the turn. decode() awaits the
  // in-flight fetch AND decodes, so the first raster after the slide doesn't
  // pay it — decoding=async is only a hint, this actually schedules the work.
  // No-op without src (still-deferred cells) or decode support; the rejection
  // on a cleared/evicted src is swallowed.
  function predecode(img){
    if (!img || !img.src || !img.decode) return;
    try { const p = img.decode(); if (p && p.catch) p.catch(() => {}); } catch(e){}
  }
  function startDeferred(img){
    if (!img || !img.dataset.deferredSrc) return;
    img.src = img.dataset.deferredSrc;
    delete img.dataset.deferredSrc;
  }
  // Start every still-deferred fetch, nearest to `idx` first.
  function flushDeferredFetches(idx){
    Object.keys(stripImgs)
      .map(Number)
      .filter(i => stripImgs[i] && stripImgs[i].dataset.deferredSrc)
      .sort((a, b) => Math.abs(a - idx) - Math.abs(b - idx))
      .forEach(i => startDeferred(stripImgs[i]));
  }
  let deferredFlushTimer = 0;
  // Called after ensureWindow on every turn: neighbours fetch now; the rest of
  // the window waits for the next page (idx+1, the one a forward tap shows)
  // to finish, capped by DEFERRED_FLUSH_MS so a slow page cannot starve the
  // prefetch. Also re-ranks fetchPriority as the reader moves.
  const DEFERRED_FLUSH_MS = 400;
  function scheduleDeferredFetches(idx){
    clearTimeout(deferredFlushTimer);
    for (const k in stripImgs){
      const img = stripImgs[k], d = Math.abs(Number(k) - idx);
      if (!img) continue;
      if (d <= 1) startDeferred(img);
      if (d <= 2) predecode(img);
      if (!isImgReady(img)) img.fetchPriority = d <= 1 ? "high" : "low";
    }
    const next = stripImgs[idx + 1];
    const flush = () => { clearTimeout(deferredFlushTimer); flushDeferredFetches(idx); };
    if (next && !isImgReady(next)) {
      next.addEventListener("load", flush, {once:true});
      next.addEventListener("error", flush, {once:true});
      deferredFlushTimer = setTimeout(flush, DEFERRED_FLUSH_MS);
    } else {
      flush();
    }
  }

  function ensureWindow(idx){
    if (!images.length) return;
    const lo = Math.max(0, idx - PREFETCH_BEHIND);
    const hi = Math.min(images.length - 1, idx + PREFETCH_AHEAD);
    for (let i = lo; i <= hi; i++){
      if (pairStrip && pairStrip.active){
        if (pairStrip.renderSet.has(i)){ ensureStripImage(i); continue; }
        const s = spreadOf(i);
        const sp = spreads[s];
        // The anchor of a blank-for-me spread still gets its placeholder cell.
        if (sp && sp.pages[0] === i && !renderPagesForSpread(s).length) ensureBlankCell(s);
      } else {
        ensureStripImage(i);
      }
    }
  }

  function evictStrip(idx){
    for (const k of Object.keys(stripImgs)){
      const i = Number(k);
      // Paired: also drop any cell for an index this device no longer renders
      // (post-SWAP leftovers) — the old side's DOM/src must not linger.
      const nonRendered = pairStrip && pairStrip.active && !pairStrip.renderSet.has(i);
      if (i < idx - EVICT_BEHIND || i > idx + EVICT_AHEAD || nonRendered){
        const img = stripImgs[k];
        const cell = stripCells[k];
        if (cell) cell.remove();
        if (img) imagePool.release(img);  // return to pool, not delete
        delete stripImgs[k];
        delete stripCells[k];
        // pageAR[i] is deliberately kept — scrolling back must not re-measure.
      }
    }
    if (pairStrip && pairStrip.active){
      const curS = spreadOf(idx);
      for (const k of Object.keys(pairBlankCells)){
        if (Math.abs(Number(k) - curS) > EVICT_AHEAD){
          pairBlankCells[k].remove();
          delete pairBlankCells[k];
        }
      }
    }
    for (const k of Object.keys(spreadEls)){
      if (!spreadEls[k].children.length){
        spreadEls[k].remove();
        delete spreadEls[k];
      }
    }
  }

  // Page images fade in with an opacity transition and those transitionend
  // events BUBBLE to the track. Anything waiting for a page turn to finish must
  // ignore them, or an image fading in mid-slide ends the slide early.
  function isTrackSlideEnd(e){
    return !e || (e.target === track && e.propertyName === "transform");
  }

  // Apply the track's transform for the given idx (with optional live dragPx).
  function applyTrackTransform(idx, dragPx, animate){
    if (animate){
      track.style.transition = "transform .3s cubic-bezier(0.25,0.1,0.25,1.0)";
      // Flush the transition change with the OLD transform still in place. Setting
      // both in one tick leaves the before-change style at transition:none (every
      // settled slide ends there) and the browser then starts no transition.
      track.offsetHeight;
    } else {
      track.style.transition = "none";
    }
    const off = pageOffsetPx(idx);
    track.style.transform = "translateX(" + (off + (dragPx || 0)) + "px)";
  }

  // Move the anchor to idx without moving anything on screen. Re-basing shifts
  // every spread's `left` by a constant and the track transform by the same
  // constant in the opposite direction, so the result is visually identical.
  function rebaseAnchor(idx){
    anchorSpread = spreadOf(idx);
    updateStripPositions();
    applyTrackTransform(idx, 0, false);
  }

  function snapTo(idx, animate=true){
    if (idx < 0 || idx >= images.length) return;
    if (window.Zoom.isActive()) window.Zoom.reset(true);
    // Paired: idx is the spread ANCHOR; dispIdx is the page this device
    // actually renders of it (-1 = the assigned side is blank → no <img>).
    const dispIdx = displayIdxFor(idx);
    currentIdx = idx;
    syncChapterSlider(idx);
    imgContainer.classList.toggle("pair-blank", !!(pairStrip && pairStrip.active && dispIdx < 0));
    // Chapter-aware counter and folder name
    if (pairStrip && pairStrip.active){
      // Paired counter tracks the DISPLAYED page — own ref's position in its
      // chapter — and a blank side gets its own unmistakable marker (never
      // the spinner, never the image-error ⚠).
      const info = getCurrentChapterInfo(idx);
      if (dispIdx >= 0){
        counter.textContent = (dispIdx - chapterBoundaries[info.chapterIdx] + 1) + " / " + getChapterRealCount(info.chapterIdx);
      } else {
        counter.textContent = "空白";
      }
      folderName.textContent = info.chapterName || (currentFolder ? currentFolder.split("/").pop() : "");
    } else if (chapterList.length > 1) {
      const realIdx = globalToRealIdx[idx] !== undefined ? globalToRealIdx[idx] : -1;
      if (realIdx >= 0) {
        const info = getCurrentChapterInfo(idx);
        counter.textContent = (getChapterLocalRealIdx(idx) + 1) + " / " + getChapterRealCount(info.chapterIdx);
        folderName.textContent = info.chapterName;
      } else {
        counter.textContent = (idx + 1) + " / " + images.length;
      }
    } else {
      counter.textContent = (idx + 1) + " / " + images.length;
      folderName.textContent = currentFolder ? currentFolder.split("/").pop() : "";
    }
    // Last-read snapshot for pair bootstrap: opening the sidebar runs
    // returnToBrowse() which clears currentFolder/images, so the pair
    // controller's openWork bootstrap needs this memory of what was on
    // screen. Solo only — a paired session knows its own work already.
    if (!(pairStrip && pairStrip.active)){
      const pg = images[idx];
      if (currentFolder && pg && !pg.isSpacer && typeof pg.name === "string"){
        try {
          const ci = chapterList.length > 1 ? getCurrentChapterInfo(idx) : null;
          const leaf = (ci && chapterList[ci.chapterIdx] && chapterList[ci.chapterIdx].fullPath) || currentFolder;
          window.lastReaderPos = {workFolder: currentFolder, folder: leaf, name: pg.name};
        } catch(e) { /* snapshot is best-effort */ }
      }
    }
    // Per-chapter progress save. Skipped during the initial open (urlSyncPaused)
    // so a deep-link visit doesn't overwrite the stored resume point on sight —
    // the resume point moves on the first real page turn.
    if (!urlSyncPaused) {
      if (currentFolder && chapterList.length > 1) {
        const info = getCurrentChapterInfo(idx);
        const chapter = chapterList[info.chapterIdx];
        if (chapter) ReadingProgress.saveDebounced(chapter.fullPath, info.localIdx, chapter.images.length);
      } else if (currentFolder) {
        ReadingProgress.saveDebounced(currentFolder, idx, images.length);
      }
      scheduleUrlSync();
    }
    const perfEntry = perfOn ? performance.now() : 0;
    // Acknowledge first, load later. The synchronous path only does what the
    // reader must see this frame: counter/slider (above), the target page's own
    // cell (so the strip has a frame to move to), and the move itself (rAF
    // below). Creating the rest of the prefetch window, evicting, and kicking
    // off image fetches happen after that first frame has been committed.
    // Only this device's assigned page gets image DOM; a blank side gets the
    // placeholder cell (zero <img>, zero stale src) instead.
    if (dispIdx >= 0){
      ensureStripImage(dispIdx);
      startDeferred(stripImgs[dispIdx]);
    } else if (pairStrip && pairStrip.active){
      ensureBlankCell(spreadOf(idx));
    }
    const perfAfterWindow = perfOn ? performance.now() : 0;
    updateStripPositions();
    perfRecordTurn(idx, perfEntry, perfAfterWindow, perfOn ? performance.now() : 0);
    const windowGen = ++windowWorkGen;
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (windowGen !== windowWorkGen) return;   // a newer turn owns the window now
      ensureWindow(idx);
      scheduleDeferredFetches(idx);
      evictStrip(idx);
      ensureChapterLabels(getCurrentChapterInfo(idx).chapterIdx);
      updateStripPositions();
      // Keep the prebuilt overview's `.cur` cell on the page actually being
      // read: Zoom.driftTarget() is that cell's centre, so a stale one aims the
      // shrink drift at the page the book was opened on. It lives in THIS
      // deferred job, not on snapTo's synchronous path: scrollCurToThird reads
      // offsetTop/clientHeight/getBoundingClientRect on a laid-out grid of up
      // to 700 cells right after updateStripPositions dirtied layout — a forced
      // reflow per tap that the acknowledging frame would have to pay for. Its
      // only consumer is the next pinch, which cannot land in the same frame.
      // No `window.Overview &&` guard: it is a hard dependency loaded before
      // app.js (see test_offline_core_unit).
      const dIdx = displayIdxFor(idx);
      window.Overview.markCurrent(dIdx >= 0 ? dIdx : idx);
      if (window.__BOOKQ_TEST_MODE__ === 'true') window.__windowWorkDone = (window.__windowWorkDone || 0) + 1;
    }));
    // Burst mode: a turn that arrives while the previous slide is still in
    // flight cuts instantly instead of restarting the .3s transition from the
    // mid-flight position. Measured before this: at 70ms taps the strip sat a
    // constant 0.8 page behind currentIdx, only one transitionend fired for a
    // 15-tap burst, and it kept sliding ~250ms after the last tap. The first
    // tap of a burst still slides; only the pile-up is cut. The test is
    // time-based, not `sliding`: an instant cut clears `sliding`, so a flag
    // check alone would let every other tap of a burst animate again.
    // Only user-visible (animated) turns open a burst window: programmatic
    // instant snaps (initial open, deep-link restore, orientation, reclaim)
    // must not turn the reader's next single tap into a cut.
    if (animate) {
      const now = performance.now();
      if (sliding || now - lastTurnAt < SLIDE_MS) animate = false;
      lastTurnAt = now;
    }
    const gen = ++snapGen;
    requestAnimationFrame(() => {
      if (gen !== snapGen) return;   // superseded by a newer snap or a drag
      updateStripPositions();
      if (!animate){ sliding = false; rebaseAnchor(idx); return; } // the superseded settle() is gen-guarded and never clears `sliding`
      // Animate in the CURRENT anchor frame, then re-base once the slide has
      // landed. Re-basing first would cancel the movement: the containers' left
      // jumps by the same amount the transform would have travelled, leaving
      // nothing to animate and turning the page turn into an instant cut.
      applyTrackTransform(idx, 0, true);
      sliding = true;
      let settled = false;
      const settle = (e) => {
        if (!isTrackSlideEnd(e)) return;
        if (settled || gen !== snapGen) return;
        settled = true;
        sliding = false;
        track.removeEventListener("transitionend", settle);
        rebaseAnchor(idx);
      };
      track.addEventListener("transitionend", settle);
      setTimeout(settle, 380);  // fallback: transitionend won't fire for a no-op move
    });
    // Hide spinner once current page image is decoded
    const cur = dispIdx >= 0 ? stripImgs[dispIdx] : null;
    const onCurError = () => {
      // H7: a warning is only legitimate for a genuine load failure of the
      // CURRENT page's image. Recycled images fire stale `error` events from
      // aborted fetches while decoding fine — naturalWidth > 0 means the
      // bitmap is there, so those must not append ⚠.
      if (idx !== currentIdx) return;
      if (cur.naturalWidth !== 0) return;
      const dIdx = dispIdx >= 0 ? dispIdx : idx;
      // No images[dIdx]: the state array moved on (failed load, paired
      // teardown) — there is no page left to warn about. Spacer entries DO
      // exist in the array, so they keep the old src-compare path below.
      if (!images[dIdx]) return;
      if (cur.getAttribute("src") !== getUrl(dIdx)) return;
      imgContainer.classList.remove("loading");
      spinner.style.display = "none";
      let displayIdx, total;
      if (pairStrip && pairStrip.active){
        const info = getCurrentChapterInfo(idx);
        displayIdx = dIdx - chapterBoundaries[info.chapterIdx] + 1;
        total = getChapterRealCount(info.chapterIdx);
      } else if (chapterList.length > 1) {
        const info = getCurrentChapterInfo(idx);
        displayIdx = getChapterLocalRealIdx(idx) + 1;
        total = getChapterRealCount(info.chapterIdx);
      } else {
        displayIdx = idx + 1;
        total = images.length;
      }
      counter.textContent = displayIdx + " / " + total + " ⚠";
    };
    if (pairStrip && pairStrip.active && dispIdx < 0){
      // Blank side: nothing to decode — hide the spinner, keep the distinct
      // .pair-blank state (never the ⚠ error or the spinner).
      imgContainer.classList.remove("loading");
      spinner.style.display = "none";
    } else if (cur){
      const done = () => {
        if (idx === currentIdx){
          imgContainer.classList.remove("loading");
          spinner.style.display = "none";
        }
      };
      // H8: `complete` is true before the fetch task even starts (spec: no
      // request made yet), so it alone is NOT a decode signal — require an
      // actual decoded size, or the spinner disappears for the whole fetch
      // and the reader stares at a blank screen.
      if (cur.complete && cur.naturalWidth > 0) done();
      else {
        cur.addEventListener("load", done, {once:true});
        cur.addEventListener("error", onCurError, {once:true});
        // A prefetched image that already failed before this snapTo (its
        // error event fired while we were elsewhere) gets the same
        // treatment; ensureStripImage marks failed loads with "loaded".
        if (cur.complete && cur.classList.contains("loaded")) onCurError();
      }
    }
    // else: no cell yet (spacer / not generated). H8: do NOT hide the
    // spinner here — the current page's image is still fetching/decoding.
    // (Overview.markCurrent runs in the deferred window job above.)
  }
  // The live page-turn entry point. window.snapTo is spread.js's dormant twin,
  // so viewer/zoom.js reclaims an interrupted slide through this name instead.
  try { window.viewerSnapTo = snapTo; } catch(e) { console.warn(e); }

  function setTrackOffset(idx, dragPx){
    track.style.transition = "none";
    updateStripPositions();
    applyTrackTransform(idx, dragPx, false);
  }

  // Navigation moves by spread, so solo covers, odd trailing pages and chapter
  // spacers need no special-casing — they are already folded into `spreads`.
  // Chapter crossing happens only with opts.crossChapter (tap zones); keys clamp to the current chapter.
  function goNext(opts){
    if (!images.length) return;
    const crossChapter = !!(opts && opts.crossChapter === true);
    const s = spreadOf(currentIdx);
    if (s + 1 >= spreads.length) return;
    let t = s + 1;
    if (!crossChapter && spreads.length){
      const range = getChapterSpreadRange(getCurrentChapterInfo(currentIdx).chapterIdx);
      t = Math.max(range.lo, Math.min(t, range.hi));
      if (t === s) return;
    }
    snapTo(spreads[t].pages[0]);
  }

  function goPrev(opts){
    if (!images.length) return;
    const crossChapter = !!(opts && opts.crossChapter === true);
    const s = spreadOf(currentIdx);
    if (s <= 0) return;
    let t = s - 1;
    if (!crossChapter && spreads.length){
      const range = getChapterSpreadRange(getCurrentChapterInfo(currentIdx).chapterIdx);
      t = Math.max(range.lo, Math.min(t, range.hi));
      if (t === s) return;
    }
    snapTo(spreads[t].pages[0]);
  }

  // ── navigation port (viewer/navigation.js — dual-reader S02) ──
  // Every user movement below is a typed intent handed to
  // ViewerNavigation.request(). soloNavHandler is the DEFAULT handler:
  // it reproduces the pre-port behaviour byte-for-byte, so an installed
  // comm/pairing handler receives the identical intents a solo session
  // would have applied locally. Committed remote state returns through
  // ViewerNavigation.applyCommittedState → applyCommittedNav — never
  // through request(), so it can never echo as a fresh user intent.
  // Intent payloads carry PageRef {folder, name} values — no DOM nodes,
  // no local globalIdx.
  function navFromChapter(){
    if (!images.length || !chapterList.length) return null;
    const ch = chapterList[getCurrentChapterInfo(currentIdx).chapterIdx];
    return ch ? ch.fullPath : (currentFolder || null);
  }
  function navPageRefAt(idx){
    const im = images[idx];
    return (im && !im.isSpacer) ? {folder: im.folder, name: im.name} : null;
  }
  function navIndexForRef(ref){
    if (!ref || typeof ref !== "object") return -1;
    for (let i = 0; i < images.length; i++){
      const im = images[i];
      if (im && !im.isSpacer && im.folder === ref.folder && im.name === ref.name) return i;
    }
    return -1;
  }
  function navTurn(delta, crossChapter, via){
    window.ViewerNavigation.request({type: "turn", delta: delta, crossChapter: crossChapter, fromChapter: navFromChapter(), via: via});
  }
  function navGotoIdx(idx, via, opts){
    const page = navPageRefAt(idx);
    if (!page) return;
    window.ViewerNavigation.request(Object.assign({type: "goto", page: page, via: via}, opts));
  }
  function navOpenWork(folder, via){
    window.ViewerNavigation.request({type: "openWork", folder: folder, via: via});
  }

  // The swipe-commit display path: the strip is already dragged under the
  // finger, so a committed turn animates from the dragged position and the
  // real snap lands on a deferred finalizer. The finalizer is generation-
  // guarded — a remote committed state (applyCommittedState) supersedes it.
  function commitSwipeTurn(delta){
    const curSpread = spreadOf(currentIdx);
    const maxSpread = Math.max(0, spreads.length - 1);
    let newSpread = Math.max(0, Math.min(curSpread + delta, maxSpread));
    if (spreads.length) {
      const cr = getChapterSpreadRange(getCurrentChapterInfo(currentIdx).chapterIdx);
      newSpread = Math.max(cr.lo, Math.min(newSpread, cr.hi));
    }
    const newIdx = spreads[newSpread] ? spreads[newSpread].pages[0] : currentIdx;
    if (newIdx === currentIdx) { snapTo(newIdx); return; }  // clamped at a chapter/book edge: bounce-back
    // Animate the track smoothly to the target page's position using the
    // CURRENT base coordinate system — then re-base invisibly.
    ensureWindow(newIdx);
    scheduleDeferredFetches(newIdx); // ensureStripImage judged "near" against the OLD currentIdx
    const targetOffset = pageOffsetPx(newIdx);
    track.style.transition = "transform .25s cubic-bezier(0.25,0.1,0.25,1.0)";
    track.offsetHeight; // force reflow so transition applies from current position
    track.style.transform = "translateX(" + targetOffset + "px)";
    const navTok = window.ViewerNavigation.generation();
    let finalized = false;
    const finalize = () => {
      if (finalized) return;
      finalized = true;
      track.removeEventListener("transitionend", finalize);
      if (navTok !== window.ViewerNavigation.generation()) return; // superseded by a committed state
      track.style.transition = "none";
      snapTo(newIdx, false);
    };
    track.addEventListener("transitionend", finalize);
    setTimeout(finalize, 260);  // fallback if transitionend doesn't fire
  }

  // The default (solo) request handler — the same code that used to run
  // inline at each input site, dispatched by intent.
  function soloNavHandler(intent){
    switch (intent.type) {
      case "turn":
        if (intent.via === "swipe") { commitSwipeTurn(intent.delta); return; }
        if (intent.delta > 0) goNext({crossChapter: intent.crossChapter});
        else goPrev({crossChapter: intent.crossChapter});
        return;
      case "goto": {
        let idx = navIndexForRef(intent.page);
        if (idx < 0) return;
        // Paired: navigation moves by shared spread — normalize to the spread
        // anchor; the display side renders this device's projected page.
        if (pairStrip && pairStrip.active){
          const sp = spreads[spreadOf(idx)];
          if (sp && sp.pages.length) idx = sp.pages[0];
        }
        // Overview's exit needs its thumbnail crossfade over the new page;
        // every other goto is a plain snap (animated unless told otherwise).
        if (intent.via === "overview") { snapTo(idx, false); window.Overview.crossfade(window.pairedDisplayIdx(idx)); return; }
        snapTo(idx, intent.animate !== false);
        return;
      }
      case "setOffset":
        // Paired: offset is session state (S05), never a local layout change.
        if (pairStrip && pairStrip.active) return;
        spreadOffset = intent.offset;
        rebuildSpreads();
        regroupCells();
        updateStripPositions();
        syncChapterSlider(currentIdx, {resetMax:true});
        snapTo(currentIdx, false);
        updateShiftButtonStyle();
        scheduleUrlSync();
        return;
      case "openWork": {
        const folder = intent.folder;
        var FV = window.FolderVisibility;
        // positive hidden のみ拒否 — 判定不能時は loadImages 自身がキューする
        if (FV && FV.readyState !== "loading" && !FV.showAll && folder && FV.isHidden(folder)){ redirectHiddenToTop(); return; }
        if (intent.via === "resume") {
          // Continue-reading: a saved folder that is no longer a leaf steps
          // out to its browse parent first, then opens on a beat (unchanged
          // resume behaviour).
          const node = getNodeAt(folder);
          if (!(node && folderHasOnlyImages(node))) {
            currentBrowsePath = browseParentOf(folder);
            updateBrowseHeader();
            renderView();
            writeBrowseState();
            setTimeout(() => { currentFolder = folder; closeSidebar(); loadImages(folder); }, 100);
            return;
          }
        }
        currentFolder = folder;
        closeSidebar();
        loadImages(folder);
        return;
      }
      case "browse":
        returnToBrowse();
        return;
    }
  }

  // ── paired rendering (dual-reader S06, #81) ──
  // A committed paired snapshot swaps the strip's content tables for the
  // shared manifest's: `spreads` becomes the wire spread table (idx-mapped,
  // wire id preserved, anchor = pages[0] = the spread's first real page —
  // the shared anchor), and renderPagesForSpread() projects each spread to
  // this device's ONE page ([] for a blank side). The solo tables are
  // snapshotted once on entry and restored verbatim by exitPairedToSolo —
  // LEAVE must hand the reader back the exact pre-pair layout.

  // The set of global page indices this device renders — recomputed on every
  // committed apply so a SWAP flips it without touching the spread table.
  function rebuildPairRenderSet(){
    const set = new Set();
    if (pairStrip && pairStrip.active){
      for (const sp of spreads){
        const own = pairStrip.side === "right" ? sp.rightIdx : sp.leftIdx;
        if (typeof own === "number" && own >= 0) set.add(own);
      }
    }
    pairStrip.renderSet = set;
  }

  // A swap (or a first apply with a stale window) can leave cells alive for
  // indices this device no longer renders — drop them now rather than waiting
  // for evictStrip, so the old side's DOM/src cannot linger on screen.
  function purgeNonRenderedCells(){
    if (!(pairStrip && pairStrip.active)) return;
    for (const k of Object.keys(stripImgs)){
      const i = Number(k);
      if (!pairStrip.renderSet.has(i)){
        const cell = stripCells[k];
        if (cell) cell.remove();
        const img = stripImgs[k];
        if (img) imagePool.release(img);
        delete stripImgs[k];
        delete stripCells[k];
      }
    }
  }

  // Rebuild the strip's tables from the shared manifest. Wire spreads become
  // idx-mapped spreads ({id, chapterFolder, rightIdx, leftIdx, pages,
  // chapterStart}); images/chapterList come from the manifest, never a fetch.
  function enterPairStrip(manifest, desc){
    if (!pairStrip){
      pairStrip = {
        active: true, side: desc.side, spreadId: null, renderSet: new Set(),
        offset: 0,
        solo: {
          images: images, chapterList: chapterList, chapterBoundaries: chapterBoundaries,
          spreads: spreads, idxToSpread: idxToSpread,
          realToGlobalIdx: realToGlobalIdx, globalToRealIdx: globalToRealIdx,
          spacerCount: spacerCount, realImageCount: realImageCount,
          currentIdx: currentIdx, currentFolder: currentFolder,
          pageMode: pageMode, landscapeSpread: landscapeSpread,
          spreadOffset: spreadOffset, anchorSpread: anchorSpread,
        },
      };
      imgContainer.classList.add("pair-mode");
    }
    pairStrip.active = true;
    // The session's shared offset rides the manifest — the ◀ control reads
    // it to ask for the toggle (SET_OFFSET), never a local rebuild.
    pairStrip.offset = (manifest && typeof manifest.offset === "number") ? manifest.offset : 0;
    updateShiftButtonStyle();
    const wireSpreads = (manifest && (manifest.spreadTable || manifest.spreads)) || [];
    let pageRefs = (manifest && Array.isArray(manifest.pageRefs)) ? manifest.pageRefs : null;
    if (!pageRefs){
      // Minimal manifests may carry only the spread table — derive the page
      // list in spread order (right then left), deduplicated.
      pageRefs = [];
      const seen = new Set();
      for (const ws of wireSpreads){
        for (const r of [ws.right, ws.left]){
          if (r){
            const k = r.folder + "\x00" + r.name;
            if (!seen.has(k)){ seen.add(k); pageRefs.push(r); }
          }
        }
      }
    }
    const refIdx = new Map();
    pageRefs.forEach((r, i) => refIdx.set(r.folder + "\x00" + r.name, i));
    images = pageRefs.map(r => ({folder: r.folder, name: r.name}));
    const manifestChapters = (manifest && Array.isArray(manifest.chapters) && manifest.chapters.length)
      ? manifest.chapters : null;
    chapterList = [];
    chapterBoundaries = [];
    let acc = 0;
    if (manifestChapters){
      for (const c of manifestChapters){
        const pages = (c.pages || []).map(p => typeof p === "string"
          ? {folder: c.folder, name: p}
          : {folder: (p && p.folder !== undefined) ? p.folder : c.folder, name: p && p.name !== undefined ? p.name : p});
        chapterList.push({
          name: c.name || String(c.folder || "").split("/").pop(),
          fullPath: c.folder,
          images: pages,
        });
        chapterBoundaries.push(acc);
        acc += pages.length;
      }
    } else {
      // No chapter table: derive chapter runs from consecutive chapterFolder.
      let cur = null;
      for (const ws of wireSpreads){
        if (!cur || cur.fullPath !== ws.chapterFolder){
          cur = {name: String(ws.chapterFolder || "").split("/").pop(), fullPath: ws.chapterFolder, images: []};
          chapterList.push(cur);
          chapterBoundaries.push(acc);
        }
        for (const r of [ws.right, ws.left]) if (r){ cur.images.push({folder: r.folder, name: r.name}); acc++; }
      }
    }
    realToGlobalIdx = [];
    globalToRealIdx = [];
    spacerCount = 0;
    for (let i = 0; i < images.length; i++){ realToGlobalIdx.push(i); globalToRealIdx.push(i); }
    realImageCount = images.length;
    spreads = [];
    idxToSpread = new Array(images.length);
    wireSpreads.forEach((ws, si) => {
      const rIdx = ws.right ? refIdx.get(ws.right.folder + "\x00" + ws.right.name) : -1;
      const lIdx = ws.left ? refIdx.get(ws.left.folder + "\x00" + ws.left.name) : -1;
      const pages = [];
      if (typeof rIdx === "number" && rIdx >= 0) pages.push(rIdx);
      if (typeof lIdx === "number" && lIdx >= 0) pages.push(lIdx);
      for (const i of pages) idxToSpread[i] = si;
      const prev = si > 0 ? wireSpreads[si - 1] : null;
      spreads.push({
        id: ws.id,
        chapterFolder: ws.chapterFolder,
        rightIdx: (typeof rIdx === "number" ? rIdx : -1),
        leftIdx: (typeof lIdx === "number" ? lIdx : -1),
        pages: pages,
        chapterStart: !prev || prev.chapterFolder !== ws.chapterFolder,
      });
    });
    if (manifest && typeof manifest.folder === "string" && manifest.folder) currentFolder = manifest.folder;
    resetStrip();
    imgContainer.classList.add("loading");
    spinner.style.display = "block";
    empty.style.display = "none";
    pageSliderWrap.classList.add("visible");
    closeSidebar();
    window.Overview.close();
    window.Overview.prebuild();
  }

  // The display side of a committed paired state: update the projection, then
  // snap to the shared spread's anchor. Must never call request().
  function applyPairedCommit(desc, manifest){
    const entered = !pairStrip || !pairStrip.active || desc.manifestChanged;
    if (entered){
      enterPairStrip(manifest, desc);
    }
    pairStrip.side = desc.side;
    pairStrip.spreadId = desc.spread.id;
    rebuildPairRenderSet();
    purgeNonRenderedCells();
    const sp = spreads[desc.spreadIndex];
    const anchorIdx = sp && sp.pages.length ? sp.pages[0] : -1;
    if (anchorIdx < 0) return;   // unresolvable anchor: state recorded, no display move
    // The pair strip builds its own images/spreads/chapters from the shared
    // manifest, but the page slider's `max` is only written on solo entry
    // (syncChapterSlider resetMax / chapter change). A member that never
    // opened the work solo keeps max=0 — the slider cannot even be dragged.
    // Every (re)entry / manifest swap re-arms it against the shared table.
    if (entered) syncChapterSlider(anchorIdx, {resetMax: true});
    if (dragActive) abortDrag(); // a commit supersedes a half-finished drag
    snapTo(anchorIdx, false);
    // Write location.hash NOW — snapTo only schedules the usual 200ms
    // debounce, but a commit can land while boot is still inside its
    // shallow→full loadFolders window. An applyUrlState (the boot's own
    // post-load re-run, or a stray hashchange) that sees an EMPTY hash
    // alongside live viewer state reads it as "no view" and resetToTop's
    // teardown out from under the committed display. replaceViewerState is
    // a replaceState write — no hashchange event, and snapTo's debounced
    // re-write hits the same-URL early return.
    if (typeof window.replaceViewerState === "function") window.replaceViewerState();
  }

  // LEAVE-equivalent: restore the pre-pair solo layout verbatim and resume
  // normal rendering. Registered as PairedView's leave hook and also used
  // for leave-kind committed applies ({paired:false} / phase 'ended').
  function exitPairedToSolo(){
    window.PairedView.clear();
    imgContainer.classList.remove("pair-mode");
    imgContainer.classList.remove("pair-blank");
    if (!pairStrip) return;
    const solo = pairStrip.solo;
    // 全表示で開いていた solo 作品は通常モードでは非表示対象 — 復元せず
    // トップへ。pairStrip は先に捨てる（redirect → leave hook の再入防止）。
    pairStrip = null;
    var _fvs = window.FolderVisibility;
    if (_fvs && !_fvs.showAll && solo.currentFolder &&
        typeof _fvs.isHidden === "function" && _fvs.isHidden(solo.currentFolder)){
      redirectHiddenToTop();
      return;
    }
    images = solo.images;
    chapterList = solo.chapterList;
    chapterBoundaries = solo.chapterBoundaries;
    realToGlobalIdx = solo.realToGlobalIdx;
    globalToRealIdx = solo.globalToRealIdx;
    spacerCount = solo.spacerCount;
    realImageCount = solo.realImageCount;
    spreads = solo.spreads;
    idxToSpread = solo.idxToSpread;
    currentIdx = solo.currentIdx;
    currentFolder = solo.currentFolder;
    pageMode = solo.pageMode;
    landscapeSpread = solo.landscapeSpread;
    spreadOffset = solo.spreadOffset;
    anchorSpread = solo.anchorSpread;
    resetStrip();
    if (images.length){
      // updateOrientation rebuilds positions and re-snaps the restored page;
      // it is also the single place that rewrites body.className for the
      // current viewport, so the paired classes cannot linger anywhere.
      updateOrientation(true);
      pageSliderWrap.classList.add("visible");
    } else {
      teardownViewer();
    }
  }

  // applyCommittedState's display side: a paired commit projects the shared
  // spread to this device's ONE page (pairedView.js decides side/ref — no
  // pairing recompute here); a plain commit resolves its anchor to a local
  // page index and snaps there. §3.6: the anchor is the spread's first real
  // page. Receives typed PageRef/Spread values — never a local globalIdx.
  // Must never call request() (receive-only channel).
  function applyCommittedNav(snapshot, manifest, options){
    // 非表示 root の commit は通常モードで反映しない — PairedView.apply /
    // DOM 反映より前に、PairController._applyCommitted と同じ O(1) 判定で
    // 同期を解除してトップへ戻す（redirectHiddenToTop が teardown → leave →
    // resetToTop の順を担う）。folder を解決できない manifest は拒否しない。
    var FV = window.FolderVisibility;
    if (FV && !FV.showAll &&
        (FV.readyState === "loading" || !FV.hasSnapshot)){
      // 判定不能（idle/loading/error 無snapshot — guardPath の pending と
      // 同じ条件）— commit を適用も拒否もしない。PairController._applyCommitted
      // が最新1件を保持し、ready/readonly エッジでこの同じ経路へ戻す。
      return;
    }
    if (FV && FV.readyState !== "loading" &&
        typeof FV.committedFolderHidden === "function" &&
        FV.committedFolderHidden(manifest, snapshot)){
      redirectHiddenToTop();
      return;
    }
    const desc = window.PairedView.apply(snapshot, manifest, options);
    if (desc.kind === "leave"){ exitPairedToSolo(); return; }
    if (desc.kind === "paired"){ applyPairedCommit(desc, manifest); return; }
    if (desc.kind === "error") return;   // malformed commit: gen bumped, no display move
    let ref = null;
    const sid = snapshot && snapshot.spreadId;
    const plan = manifest && manifest.spreads;
    if (sid && Array.isArray(plan)){
      for (let i = 0; i < plan.length; i++){
        const s = plan[i];
        if (s && s.id === sid) { ref = s.right || s.left || null; break; }
      }
    }
    if (!ref && manifest && manifest.page) ref = manifest.page;
    const idx = navIndexForRef(ref);
    if (idx < 0) return;              // unresolvable commit: gen bumped, no display move
    if (dragActive) abortDrag();      // a commit supersedes a half-finished drag
    snapTo(idx, false);
  }

  try {
    window.ViewerNavigation.setDefaultHandler(soloNavHandler);
    window.ViewerNavigation.onCommittedState(applyCommittedNav);
    // PairedView.leave() (LEAVE-equivalent, S09 wires the real trigger) routes
    // here so the pre-pair solo layout comes back on screen.
    window.PairedView.setHooks({ leave: exitPairedToSolo });
  } catch(e) { console.warn(e); }

  function showHint(){
    hint.classList.add("show");
    clearTimeout(hintTimer);
    hintTimer = setTimeout(() => hint.classList.remove("show"), 3000);
  }

  searchInput.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(doSearch, 300);
  });

  // ── on-device page-turn profiler (?perf=1) ──
  // Desktop Chromium/WebKit and the Simulator show no left/right asymmetry;
  // this overlay exists to get numbers off a real iPhone: per turn it prints the
  // direction, tap→first-frame latency, whether the target image was already
  // decoded at tap time, and how long the first paint of the target took.
  // (perfOn / perfTapAt / perfTapX are declared with the snapTo state above.)
  if (perfOn){
    const box = document.createElement("pre");
    box.id = "perf-overlay";
    box.style.cssText = "position:fixed;left:8px;bottom:80px;z-index:99;margin:0;padding:6px 8px;background:rgba(0,0,0,.75);color:#8f8;font:11px/1.35 monospace;pointer-events:none;max-width:70vw;white-space:pre-wrap";
    document.body.appendChild(box);
    imgContainer.addEventListener("touchstart", e => { perfTapAt = performance.now(); perfTapX = e.touches[0] ? e.touches[0].clientX : 0; }, {capture:true, passive:true});
    window.__perfLine = (s) => { box.textContent = (s + "\n" + box.textContent).split("\n").slice(0, 8).join("\n"); };
    // Rolling FPS + worst-frame readout, in its own element so it doesn't
    // churn the turn log. Slide and tile scrolling both drive rAF, so one
    // meter covers viewer and browse alike.
    const fpsBox = document.createElement("pre");
    fpsBox.id = "perf-fps";
    fpsBox.style.cssText = "position:fixed;left:8px;bottom:60px;z-index:99;margin:0;padding:4px 8px;background:rgba(0,0,0,.75);color:#8ff;font:12px/1.2 monospace;pointer-events:none";
    document.body.appendChild(fpsBox);
    let fN = 0, fStart = performance.now(), fWorst = 0, fPrev = fStart;
    requestAnimationFrame(function fpsTick(){
      const now = performance.now(), dt = now - fPrev; fPrev = now; fN++;
      if (dt > fWorst) fWorst = dt;
      const el = now - fStart;
      if (el >= 500){
        var st = 0; try { var _sc = document.getElementById('sidebar-scroll'); st = _sc ? _sc.scrollTop : window.scrollY; } catch(e){}
        var g = window.__bookqThumbgate;
        fpsBox.textContent = "fps " + Math.round(fN * 1000 / el) + " worst " + Math.round(fWorst) + "ms view:" + (viewJpeg ? "jpeg" : "orig") + " st:" + Math.round(st) + " g:" + (g === undefined ? "-" : g) + " v12";
        fN = 0; fStart = now; fWorst = 0;
      }
      requestAnimationFrame(fpsTick);
    });
    // >50ms main-thread stalls. Chrome only — Firefox lacks the entry type and
    // throws (caught); there the worst-frame number above is the jank proxy.
    try {
      new PerformanceObserver((list) => {
        for (const e of list.getEntries()) window.__perfLine("longtask " + Math.round(e.duration) + "ms");
      }).observe({type:"longtask"});
    } catch(e){}
  }
  function perfRecordTurn(idx, entry, afterWindow, afterLayout){
    if (!perfOn || !perfTapAt) return;
    const tapAt = perfTapAt; perfTapAt = 0;
    const sync = `snap@${Math.round(entry - tapAt)} win${Math.round(afterWindow - entry)} lay${Math.round(afterLayout - afterWindow)}`;
    const dir = perfTapX < window.innerWidth * 0.30 ? "next(L)" : (perfTapX > window.innerWidth * 0.70 ? "prev(R)" : "center");
    const img = stripImgs[idx];
    const readyAtTap = isImgReady(img);
    const t0 = getComputedStyle(track).transform;
    let frames = 0;
    const tick = () => {
      frames++;
      if (getComputedStyle(track).transform !== t0 || frames > 60){
        const moveMs = Math.round(performance.now() - tapAt);
        const finish = (label) => window.__perfLine(`${dir} #${idx+1} move${moveMs} ${sync} img:${readyAtTap ? "ok" : "MISSING"} ${label}`);
        if (readyAtTap) finish("");
        else if (img) {
          const t1 = performance.now();
          const onReady = () => finish(`img ready +${Math.round(performance.now() - tapAt)}ms (${Math.round(performance.now() - t1)}ms after move)`);
          if (isImgReady(img)) onReady(); else { img.addEventListener("load", onReady, {once:true}); img.addEventListener("error", () => finish("img ERROR"), {once:true}); }
        } else finish("no cell");
        return;
      }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }

  // ── touch/click zones ──
  // Tap-zone suppression guards the zones from firing when a synthesized click follows a touch.
  // WHY a deadline, not boolean+setTimeout: WebKit synthesizes the tap's click
  // up to ~400ms after touchend when the gesture looked drag-like, and a timer
  // that flips a boolean can expire before that queued click is dispatched
  // (the timer and the click race each other). Comparing the clock at dispatch
  // time has no race — the deadline is acted on exactly once, whenever the
  // click finally lands.
  let suppressClickUntil = 0;
  const suppressClickFor = ms => { suppressClickUntil = performance.now() + ms; };
  const suppressClickNow = () => performance.now() < suppressClickUntil;
  // viewer/zoom.js drives the page turn at the pan clamp edge. It must go
  // through THESE functions, not window.goNext/goPrev — those are spread.js's
  // dormant twins and turn nothing.
  try {
    window.suppressClickFor = suppressClickFor;
    // viewer/zoom.js drives the page turn at the pan clamp edge — it is a
    // user movement request like the tap zones (crossChapter), so it goes
    // through the navigation port rather than calling goNext/goPrev.
    window.viewerGoNext = () => { if (images.length) navTurn(1, true, "edge"); };
    window.viewerGoPrev = () => { if (images.length) navTurn(-1, true, "edge"); };
    if (window.__BOOKQ_TEST_MODE__ === 'true') window.__suppressClickActive = () => suppressClickNow();
    if (window.__BOOKQ_TEST_MODE__ === 'true') window.__tapForward = () => tapForward;
    // classifyTouchEnd is a hoisted declaration below (tap-vs-swipe line) —
    // available here at setup time. __lastTouchEndVia records the decided-
    // touchend classification for tests (CDP timing cannot force dt<=1).
    if (window.__BOOKQ_TEST_MODE__ === 'true') window.__classifyTouchEnd = classifyTouchEnd;
    if (window.__BOOKQ_TEST_MODE__ === 'true') window.__lastTouchEndVia = null;
    if (window.__BOOKQ_TEST_MODE__ === 'true') window.__toggleToolbarZone = toggleToolbarZone;
  } catch(e) { console.warn(e); }
  // One resolver for "which zone is under x", shared by the touchend tap path
  // (instant) and the click handlers (mouse / keyboard activation, and the
  // fallback when a touch never produced a touchend we could act on). Zone
  // widths mirror the CSS: left 30% next, right 30% previous, middle toggle.
  function zoneActionAt(clientX){
    if (!images.length) return;
    const rect = imgContainer.getBoundingClientRect();
    const frac = (clientX - rect.left) / Math.max(1, rect.width);
    if (frac < 0.30){ if (tapForward === 'left') navTurn(1, true, "tap"); else navTurn(-1, true, "tap"); }
    else if (frac > 0.70){ if (tapForward === 'left') navTurn(-1, true, "tap"); else navTurn(1, true, "tap"); }
    else toggleToolbarZone();
  }
  function toggleToolbarZone(){
    // Zoomed, the center zone is the way back to 1.0 (CSS narrows it to the
    // middle 20%). It ignores suppression on purpose: the pan that preceded
    // the tap is exactly what arms the suppression, and the reader still
    // needs one reliable way out of the zoom.
    if (window.Zoom.isActive()) { window.Zoom.reset(true); return; }
    toolbar.classList.toggle("hidden"); spreadControls.classList.toggle("hidden"); tapmodeControls.classList.toggle("hidden");
    // pairUI owns #pair-status and may build it after the first hide —
    // mirror the toolbar's state rather than blindly toggling so they can
    // never drift apart.
    const pairStatus = document.getElementById("pair-status");
    if (pairStatus) pairStatus.classList.toggle("hidden", toolbar.classList.contains("hidden"));
  }
  function setTapForward(m, opts){
    tapForward = m;
    const left = (m === 'left');
    // Both buttons stay tappable at all times: the active mode is indicated
    // by aria-pressed + CSS, never by disabled. Tapping the already-active
    // mode re-affirms (toast + keep mapping); tapping the other switches.
    tapmodeLeft.disabled = false;
    tapmodeRight.disabled = false;
    tapmodeLeft.setAttribute("aria-pressed", String(left));
    tapmodeRight.setAttribute("aria-pressed", String(!left));
    tapmodeLeft.removeAttribute("aria-disabled");
    tapmodeRight.removeAttribute("aria-disabled");
    if (document.activeElement === tapmodeLeft && tapmodeLeft.disabled) tapmodeRight.focus();
    if (document.activeElement === tapmodeRight && tapmodeRight.disabled) tapmodeLeft.focus();
    zonePrev.title = left ? "次のページ" : "前のページ";
    zonePrev.setAttribute("aria-label", left ? "次のページ" : "前のページ");
    zoneNext.title = left ? "前のページ" : "次のページ";
    zoneNext.setAttribute("aria-label", left ? "前のページ" : "次のページ");
    if (!opts || opts.notify !== false) showToast(left ? "タップ送り: 左" : "タップ送り: 右");
  }
  tapmodeLeft.addEventListener("click", e => { if (suppressClickNow()) return; e.stopPropagation(); setTapForward('left'); });
  tapmodeRight.addEventListener("click", e => { if (suppressClickNow()) return; e.stopPropagation(); setTapForward('right'); });
  setTapForward('left', {notify:false});
  zonePrev.addEventListener("click", e => { if (suppressClickNow()) return; e.stopPropagation(); if (tapForward === 'left') navTurn(1, true, "tap"); else navTurn(-1, true, "tap"); });
  zoneToggle.addEventListener("click", e => {
    e.stopPropagation();
    // toggleToolbarZone owns the zoomed branch (and the reason it must ignore
    // suppression); this handler only adds the un-zoomed suppress guard.
    if (suppressClickNow() && !window.Zoom.isActive()) return;
    toggleToolbarZone();
  });
  zoneNext.addEventListener("click", e => { if (suppressClickNow()) return; e.stopPropagation(); if (tapForward === 'left') navTurn(-1, true, "tap"); else navTurn(1, true, "tap"); });

  // ── keyboard ──
  document.addEventListener("keydown", e => {
    if (e.target.tagName === "INPUT") return;
    switch(e.key){
      case "ArrowRight": if (images.length) navTurn(-1, false, "key"); break;
      case "ArrowLeft": if (images.length) navTurn(1, false, "key"); break;
      case "f": case "F":
  if (sidebar.classList.contains("open")) {
    closeSidebar();
  } else {
    openSidebar();
    if (searchMode) window.SearchFacets.renderSearchList();
    else if (viewMode === 'tile') window.BrowseRender.renderTiles();
    else if (viewMode === 'list') window.BrowseRender.renderList();
    else if (viewMode === 'card') window.BrowseRender.renderCards();
  }
  break;
      case "Home": if(images.length && spreads.length){ const r = getChapterSpreadRange(getCurrentChapterInfo(currentIdx).chapterIdx); navGotoIdx(spreads[r.lo].pages[0], "key", {animate:false}); } break;
      case "End": if(images.length && spreads.length){ const r = getChapterSpreadRange(getCurrentChapterInfo(currentIdx).chapterIdx); navGotoIdx(spreads[r.hi].pages[0], "key"); } break;
    }
  });

  pageSlider.addEventListener("input", () => {
    if (!images.length) return;
    const sliderVal = Number(pageSlider.value);
    const newIdx = chapterSliderToIdx(currentChapterIdx, sliderVal);
    navGotoIdx(newIdx, "slider");
  });

  let hDragState = 'idle';
  let bottomPull = 0;
  let hDragX = 0;
  let hRaf = null;
  let hHapticFired = false;
  let ptrStartY = 0, ptrStartX = 0, startScrollTop = 0;
  let ptrLocked = null;
  let ptrRaf = null;

  // ── level transition layers (iOS-style whole-screen slide) ──
  // Pop (edge swipe / ↩ tap) and push (tile tap) share one underlay slot: a
  // #sidebar clone pinned UNDER the live panel (viewer z:1 < underlay z:9 <
  // #sidebar z:10). The clone keeps every inner id so the stylesheet applies
  // verbatim; getElementById still resolves to the LIVE nodes because the
  // real #sidebar precedes the clone in document order.
  let navUnderlay = null;       // host div in the main DOM — transforms/z land here
  let navUnderlaySlot = null;   // shadow root (fallback: host) the clone lives in
  let navAnimBusy = false;
  let navAnimTimer = 0;
  let navAnimDone = null;   // completion callback of the in-flight anim
  let _navShadowCss = null;
  const NAV_UNDERLAY_PARALLAX = 0.3; // underlay rests -30% left, slides to 0
  function _navWidth(){ return (sidebar && sidebar.clientWidth) || window.innerWidth || 280; }
  function _scheduleNavDone(fn, ms){
    navAnimDone = fn;
    navAnimTimer = setTimeout(function(){
      navAnimTimer = 0;
      var f = navAnimDone; navAnimDone = null;
      if (f) f();
    }, ms);
  }
  // A fresh touch during a nav anim fast-forwards it — the new gesture owns
  // the panel now, so finish the transition instantly rather than eat input.
  function _finishNavAnimNow(){
    if (!navAnimDone) return;
    var f = navAnimDone; navAnimDone = null;
    if (navAnimTimer) { clearTimeout(navAnimTimer); navAnimTimer = 0; }
    f();
  }
  // The underlay clone lives in a shadow root so its copied ids never reach
  // document.getElementById / querySelector (or Playwright locators) during the
  // ~240ms it exists; the serialized page stylesheet keeps it pixel-identical.
  function _navShadowStyles(){
    if (_navShadowCss !== null) return _navShadowCss;
    let css = '';
    try {
      for (let i = 0; i < document.styleSheets.length; i++){
        let rules;
        try { rules = document.styleSheets[i].cssRules; } catch(e) { continue; }
        for (let j = 0; j < rules.length; j++) css += rules[j].cssText + '\n';
      }
    } catch(e) { console.warn(e); }
    _navShadowCss = css;
    return css;
  }
  function _mountUnderlayHost(){
    const host = document.createElement('div');
    host.id = 'nav-underlay';
    host.setAttribute('aria-hidden', 'true');
    host.setAttribute('inert', '');
    host.style.cssText = 'position:fixed;inset:0;z-index:9;pointer-events:none;will-change:transform;transition:none';
    navUnderlaySlot = host;   // degraded: ids collide, visuals still work
    try {
      const shadow = host.attachShadow({mode:'closed'});   // closed: locators/queries must not reach the clone
      const st = document.createElement('style');
      st.textContent = _navShadowStyles();
      shadow.appendChild(st);
      navUnderlaySlot = shadow;
    } catch(e) { console.warn(e); }
    document.body.appendChild(host);
    return host;
  }
  function mountNavUnderlay(patch){
    if (navUnderlay || !sidebar) return null;
    const host = _mountUnderlayHost();
    const u = sidebar.cloneNode(true);
    u.style.transform = '';          // never inherit a mid-gesture translate
    u.style.transition = 'none';     // per-rAF parallax writes must not lag
    if (patch) { try { patch(u); } catch(e) { console.warn(e); } }
    navUnderlaySlot.appendChild(u);
    navUnderlay = host;
    return host;
  }
  function teardownNavUnderlay(){
    if (navUnderlay) { try { navUnderlay.remove(); } catch(e) { console.warn(e); } navUnderlay = null; }
    navUnderlaySlot = null;
  }
  function _clearNavInlineStyles(){
    if (sidebar) {
      sidebar.style.transition = '';
      sidebar.style.transform = '';
      sidebar.style.willChange = '';
      sidebar.style.pointerEvents = '';
    }
    if (resumeBtn) {
      resumeBtn.style.transition = '';
      resumeBtn.style.transform = '';
    }
  }
  // The resume pill is a sibling of #sidebar — it rides the moving panel via
  // a composed transform (its CSS transform is the translateX(-50%) centring).
  function _resumeNavAnim(t, tr){
    if (!resumeBtn) return;
    if (resumeBtn.classList.contains('hidden')) {
      resumeBtn.style.transition = '';
      resumeBtn.style.transform = '';
      return;
    }
    if (tr !== undefined) resumeBtn.style.transition = tr;
    resumeBtn.style.transform = t;
  }
  function _savedScrollFor(path){
    try {
      const key = getScrollKey(path);
      return (scrollPositions[key] !== undefined) ? scrollPositions[key] : 0;
    } catch(e) { return 0; }
  }
  // Pop underlay: the PARENT level, built as a skeleton clone — header /
  // search / chips / footer are shallow clones (small), while #tile-grid is
  // rebuilt via buildLevelHtml so a full child grid is never copied and
  // immediately discarded at drag-arm.
  function _mountPopUnderlay(parent){
    if (navUnderlay || !sidebar) return null;
    const host = _mountUnderlayHost();
    const u = sidebar.cloneNode(false);         // attrs/classes/id, no children
    u.style.transform = '';
    u.style.transition = 'none';   // per-rAF parallax writes must not lag
    const header = document.getElementById('sidebar-header');
    if (header) {
      const hc = header.cloneNode(true);
      const h = hc.querySelector('#browse-header');
      if (h) {
        if (!parent) h.innerHTML = '<span id="browse-root-icon">\uD83D\uDCC1</span> フォルダ';
        else { const n = getNodeAt(parent); h.innerHTML = '↩ ' + escapeHtml(n ? n.name : parent); }
      }
      u.appendChild(hc);
    }
    const vs = document.getElementById('visibility-status');
    if (vs) u.appendChild(vs.cloneNode(true));
    const ss = sidebarScroll.cloneNode(false);  // scroller attrs/classes only
    const si = document.getElementById('search');
    if (si) {
      const sic = si.cloneNode(true);
      const liveInput = document.getElementById('search-input');
      const uInput = sic.querySelector('#search-input');
      if (liveInput && uInput) uInput.value = liveInput.value;
      ss.appendChild(sic);
    }
    const chips = document.getElementById('filter-chips');
    if (chips) ss.appendChild(chips.cloneNode(true));
    const g = tileGrid ? tileGrid.cloneNode(false) : document.createElement('div');   // keeps list-mode/card-mode classes
    if (!g.id) g.id = 'tile-grid';
    let outCount;
    if (window.BrowseRender && typeof window.BrowseRender.buildLevelHtml === 'function') {
      const out = window.BrowseRender.buildLevelHtml(parent, viewMode);
      g.innerHTML = out.html;
      outCount = out.count;
    }
    ss.appendChild(g);
    u.appendChild(ss);
    const footer = document.getElementById('sidebar-footer');
    if (footer) {
      const fc = footer.cloneNode(true);
      const cnt = fc.querySelector('#folder-count');
      if (cnt && outCount !== undefined) cnt.textContent = outCount + ' 件';
      u.appendChild(fc);
    }
    navUnderlaySlot.appendChild(u);
    ss.scrollTop = _savedScrollFor(parent);     // after append: writes stick
    navUnderlay = host;
    return host;
  }
  function ensurePopUnderlay(){
    if (navUnderlay || navAnimBusy) return;
    if (window.searchMode) return;
    if (prefersReducedMotion()) return;
    const parent = browseParentOf(currentBrowsePath);
    const u = _mountPopUnderlay(parent);
    if (u) u.style.transform = 'translate3d(' + (-NAV_UNDERLAY_PARALLAX * _navWidth()) + 'px,0,0)';
  }
  // Shared landing for swipe-commit and ↩-tap pops: the live panel has
  // already slid off right; swap its content to the parent invisibly and
  // drop the underlay that was standing in for it.
  function _finishPop(parent){
    currentBrowsePath = parent;
    updateBrowseHeader();
    renderView();
    writeBrowseStateWith(parent);
    const key = getScrollKey(currentBrowsePath);
    if (scrollPositions[key] !== undefined){
      requestAnimationFrame(() => { sidebarScroll.scrollTop = scrollPositions[key]; });
    }
    _clearNavInlineStyles();
    teardownNavUnderlay();
    navAnimBusy = false;
    hDragState = 'idle';
    hDragX = 0;
    ptrLocked = null;
    hHapticFired = false;
    applyVisual();
  }
  function _startPopCommit(parent){
    const w = _navWidth();
    navAnimBusy = true;
    const tr = 'transform 220ms cubic-bezier(0,0,0.2,1)';
    sidebar.style.transition = tr;
    sidebar.style.transform = 'translate3d(' + w + 'px,0,0)';
    sidebar.style.willChange = 'transform';
    sidebar.style.pointerEvents = 'none';
    if (navUnderlay) {
      navUnderlay.style.transition = tr;
      navUnderlay.style.transform = 'translate3d(0,0,0)';
    }
    _resumeNavAnim('translateX(-50%) translate3d(' + w + 'px,0,0)', tr);
    _scheduleNavDone(function(){ _finishPop(parent); }, 220);
  }
  // ↩ tap pop: same slide as the swipe commit, starting from rest.
  function _startPopAnim(parent){
    if (navAnimBusy || !sidebar || !sidebar.classList.contains('open')) return false;
    if (prefersReducedMotion()) return false;
    if (window.searchMode) return false;
    const u = _mountPopUnderlay(parent);
    if (!u) return false;
    u.style.transform = 'translate3d(' + (-NAV_UNDERLAY_PARALLAX * _navWidth()) + 'px,0,0)';
    void u.offsetWidth;   // commit the -30% start so its transition can run
    _startPopCommit(parent);
    return true;
  }
  // Push: the new level was just rendered — slide the live panel in from the
  // right over a static clone of the level that was on screen.
  function _startPushAnim(under){
    const w = _navWidth();
    navAnimBusy = true;
    const tr = 'transform 240ms cubic-bezier(0,0,0.2,1)';
    sidebar.style.transition = 'none';
    sidebar.style.transform = 'translate3d(' + w + 'px,0,0)';
    sidebar.style.willChange = 'transform';
    sidebar.style.pointerEvents = 'none';
    _resumeNavAnim('translateX(-50%) translate3d(' + w + 'px,0,0)', 'none');
    void sidebar.offsetWidth;   // commit the off-screen start before the transition arms
    sidebar.style.transition = tr;
    sidebar.style.transform = 'translate3d(0px,0,0)';
    _resumeNavAnim('', tr);
    if (under) {
      under.style.transition = tr;
      under.style.transform = 'translate3d(' + (-NAV_UNDERLAY_PARALLAX * w) + 'px,0,0)';
    }
    _scheduleNavDone(function(){
      _clearNavInlineStyles();
      teardownNavUnderlay();
      navAnimBusy = false;
    }, 240);
  }
  function applyVisual(){
    const prefersReduced = prefersReducedMotion();
    if (tileGrid) {
      // #tile-grid's transform is now only the bottom rubber band — the
      // back-swipe h-drag moves the whole #sidebar panel instead.
      if (prefersReduced || bottomPull <= 0) {
        tileGrid.style.transform = '';
        tileGrid.style.opacity = '';
        tileGrid.style.willChange = '';
      } else {
        tileGrid.style.transform = 'translate3d(0,' + (-bottomPull) + 'px,0)';
        tileGrid.style.opacity = '';
        tileGrid.style.willChange = 'transform';
      }
    }
    if (!sidebar || navAnimBusy) return;   // anims own the inline styles
    if (prefersReduced || (hDragState !== 'draggingH' && hDragState !== 'ready_to_pop')) {
      sidebar.style.transition = '';
      sidebar.style.transform = '';
      sidebar.style.willChange = '';
      if (navUnderlay) { navUnderlay.style.transition = ''; navUnderlay.style.transform = ''; }
      _resumeNavAnim('');
      return;
    }
    // Whole-screen finger-follow: the panel tracks dx 1:1 while the parent
    // underlay parallaxes -30% → 0 behind it.
    const w = _navWidth();
    const x = Math.min(Math.max(hDragX, 0), w);
    sidebar.style.transition = 'none';
    sidebar.style.transform = 'translate3d(' + x + 'px,0,0)';
    sidebar.style.willChange = 'transform';
    if (navUnderlay) {
      const p = w > 0 ? Math.min(x / w, 1) : 1;
      navUnderlay.style.transform = 'translate3d(' + ((p - 1) * NAV_UNDERLAY_PARALLAX * w) + 'px,0,0)';
    }
    _resumeNavAnim(x > 0 ? 'translateX(-50%) translate3d(' + x + 'px,0,0)' : '');
  }
  function ptrSnapBack(){
    hDragState = 'idle';
    hDragX = 0;
    bottomPull = 0;
    ptrLocked = null;
    hHapticFired = false;
    if (ptrRaf) { cancelAnimationFrame(ptrRaf); ptrRaf = null; }
    if (hRaf) { cancelAnimationFrame(hRaf); hRaf = null; }
    _finishNavAnimNow();
    teardownNavUnderlay();
    _clearNavInlineStyles();
    if (tileGrid) tileGrid.style.transition = 'transform ' + PTR_SNAP_MS + 'ms ease-out, opacity ' + PTR_SNAP_MS + 'ms ease-out';
    applyVisual();
    setTimeout(function(){
      if(tileGrid){ tileGrid.style.transition=''; tileGrid.style.willChange=''; }
    }, PTR_SNAP_MS);
  }
  let ptrAtBottom = false;
  let tmBlockArmed = false;
  let suppressGridClick = false;
  function updatePtrAtBottom(){
    ptrAtBottom = sidebarScroll.scrollTop + sidebarScroll.clientHeight >= sidebarScroll.scrollHeight - 1;
  }
  sidebarScroll.addEventListener('scroll', updatePtrAtBottom, {passive: true});
  window.addEventListener('resize', updatePtrAtBottom);
  // preventDefault must come from a non-passive listener, but a non-passive
  // touchmove on the scroller forces every real-finger drag to block on the
  // main thread. Keep the tracking listener passive and arm a preventer only
  // while a gesture actually needs it (h-drag / bottom overscroll pull).
  function tmBlocker(e){ if (e.cancelable) e.preventDefault(); }
  function armTmBlock(){ if(!tmBlockArmed){ tmBlockArmed = true; sidebarScroll.addEventListener('touchmove', tmBlocker, {passive: false}); } }
  function disarmTmBlock(){ if(tmBlockArmed){ tmBlockArmed = false; sidebarScroll.removeEventListener('touchmove', tmBlocker); } }
  // Non-committing cleanup shared by every "gesture context changed" path.
  // Drops the armed preventer and the drag/pull bookkeeping so a leaked state
  // can never keep scroll suppressed.
  function resetBrowseGesture(finishAnim){
    // Fast-forward any in-flight nav anim — a fresh gesture owns the panel.
    // MutationObserver sweeps pass finishAnim:false — a re-render that lands
    // mid-anim must not kill the transition itself.
    if (finishAnim !== false) _finishNavAnimNow();
    disarmTmBlock();
    hDragState = 'idle';
    hDragX = 0;
    bottomPull = 0;
    ptrLocked = null;
    hHapticFired = false;
    if (hRaf) { cancelAnimationFrame(hRaf); hRaf = null; }
    if (ptrRaf) { cancelAnimationFrame(ptrRaf); ptrRaf = null; }
    // A mid-gesture re-render detaches touch delivery; the h-drag's panel
    // preview and its underlay go with it (a running nav anim owns them).
    if (!navAnimBusy) { teardownNavUnderlay(); _clearNavInlineStyles(); }
    applyVisual();
  }
  sidebarScroll.addEventListener('touchstart', function(e){
    // Always reset first — even when this start is multi-touch
    // (e.touches.length !== 1). If a previous gesture's touchend never
    // reached us (its target was detached by a re-render, or the OS
    // interrupted the gesture), tmBlockArmed would otherwise stay armed and
    // keep preventDefault-ing every touchmove, permanently freezing scroll.
    resetBrowseGesture();
    if (!e.touches || e.touches.length !== 1) return;
    if (!e.target.closest('#sidebar') && !e.target.closest('#sidebar-scroll')) return;
    ptrStartY = e.touches[0].clientY;
    ptrStartX = e.touches[0].clientX;
    startScrollTop = sidebarScroll.scrollTop;
    updatePtrAtBottom();
    suppressGridClick = false;
  }, {passive: true});
  sidebarScroll.addEventListener('touchmove', function(e){
    if (!e.touches || e.touches.length !== 1) return;
    if (navAnimBusy) return;   // a commit/cancel/push anim owns the panel
    if (bottomPull === 0 && hDragState === 'idle' && hDragX === 0) {
      if (!ptrAtBottom) {
        const startXGate = ptrStartX < 24 || currentBrowsePath !== null;
        if (!startXGate) return;
      }
    }
    const curY = e.touches[0].clientY;
    const curX = e.touches[0].clientX;
    const dx = curX - ptrStartX;
    const dy = curY - ptrStartY;
    // Direction lock: 'h' only on a DELIBERATE horizontal intent (|dx|>14
    // and dominant by 1.4x). The old rule (|dx|>=|dy| at the first 8px)
    // caught natural diagonal flicks — inside a folder hGate is always
    // true, so the gesture then suppressed vertical scroll AND armed the
    // blocking tmBlocker (~25fps under a real finger). Ambiguous starts
    // stay undecided instead of forcing 'v', so a small first move can't
    // kill an actual back-swipe either.
    if (!ptrLocked && (Math.abs(dx) > 8 || Math.abs(dy) > 8)) {
      // 'h' only while the browser hasn't claimed this gesture: once native
      // scrolling starts, touchmove arrives cancelable:false and the armed
      // blocker couldn't preventDefault it anyway — the list would keep
      // scrolling while the back-swipe preview also moved. Leaving
      // ptrLocked null there means every later move re-checks harmlessly.
      if (Math.abs(dx) > 14 && Math.abs(dx) > Math.abs(dy) * 1.4 && e.cancelable !== false) ptrLocked = 'h';
      else if (Math.abs(dy) >= Math.abs(dx)) ptrLocked = 'v';
    }
    if (ptrLocked === 'h') {
      const hGate = ptrStartX < 24 || currentBrowsePath !== null;
      if (!hGate) return;
      // The 'h' lock is provisional while unarmed: a flick that started
      // diagonal and turned clearly vertical belongs to native scrolling.
      // Once draggingH arms, preventDefault owns the gesture — no way back.
      if (hDragState === 'idle' && Math.abs(dy) > Math.abs(dx) * 1.5) {
        ptrLocked = 'v';
        hDragX = 0;
      } else {
        hDragX = dx;
        if (hDragX < 0) hDragX = 0;
        if (hDragState === 'idle' && dx > H_DRAG_ARM && Math.abs(dx) > Math.abs(dy)) {
          hDragState = 'draggingH';
          // Mount the parent underlay and pin the panel's transition off
          // before the first rAF write, or the CSS .35s open/close
          // transition would lag the finger's first frame.
          ensurePopUnderlay();
          if (sidebar) { sidebar.style.transition = 'none'; sidebar.style.willChange = 'transform'; }
        }
        if (hDragState === 'draggingH' || hDragState === 'ready_to_pop') {
          if (hDragX >= H_SWIPE_THRESHOLD && hDragState === 'draggingH') {
            hDragState = 'ready_to_pop';
            if (!hHapticFired) {
              try {
                const prefersReduced = prefersReducedMotion();
                if (!prefersReduced && 'vibrate' in navigator) navigator.vibrate(10);
              } catch(_) { console.warn(_); }
              hHapticFired = true;
            }
          } else if (hDragState === 'ready_to_pop' && hDragX < H_SWIPE_DEACTIVATE) {
            hDragState = 'draggingH';
          }
          armTmBlock();
          if (hRaf) cancelAnimationFrame(hRaf);
          hRaf = requestAnimationFrame(function(){ hRaf = null; applyVisual(); });
        }
        return;
      }
    }
    if (ptrLocked && ptrLocked !== 'v') return;
    const atBottom = ptrAtBottom;
    if (atBottom && dy < 0 && ptrLocked === 'v') {
      const rawUp = -dy;
      bottomPull = 18 * (1 - Math.exp(-rawUp / 40));
      armTmBlock();
      if (ptrRaf) cancelAnimationFrame(ptrRaf);
      ptrRaf = requestAnimationFrame(function(){ ptrRaf = null; applyVisual(); });
      return;
    } else {
      if (bottomPull > 0) {
        bottomPull = 0;
        disarmTmBlock();
        if (ptrRaf) cancelAnimationFrame(ptrRaf);
        ptrRaf = requestAnimationFrame(function(){ ptrRaf = null; applyVisual(); });
      }
    }
  }, {passive: true});
  function handlePtrEnd(){
    if (hDragState !== 'idle' || bottomPull > 0) {
      suppressGridClick = true;
      setTimeout(function(){ suppressGridClick = false; }, 400);
    }
    disarmTmBlock();
    if (ptrRaf) { cancelAnimationFrame(ptrRaf); ptrRaf = null; }
    if (hRaf) { cancelAnimationFrame(hRaf); hRaf = null; }
    if (hDragState === 'ready_to_pop' && hDragX >= H_SWIPE_THRESHOLD) {
      const prefersReducedH = prefersReducedMotion();
      const parent = browseParentOf(currentBrowsePath);
      if (prefersReducedH) {
        currentBrowsePath = parent;
        updateBrowseHeader();
        renderView();
        writeBrowseStateWith(currentBrowsePath);
        hDragState = 'idle';
        hDragX = 0;
        ptrLocked = null;
        hHapticFired = false;
        teardownNavUnderlay();
        _clearNavInlineStyles();
        if (tileGrid) { tileGrid.style.transform = ''; tileGrid.style.opacity = ''; tileGrid.style.willChange = ''; }
        applyVisual();
      } else {
        _startPopCommit(parent);
      }
      return;
    } else if (hDragState === 'draggingH' || hDragState === 'ready_to_pop') {
      const hadH = hDragX > 0;
      hDragState = 'idle';
      hDragX = 0;
      ptrLocked = null;
      hHapticFired = false;
      const prefersReducedH2 = prefersReducedMotion();
      if (hadH) {
        if (prefersReducedH2) {
          teardownNavUnderlay();
          _clearNavInlineStyles();
          applyVisual();
          setTimeout(function(){ applyVisual(); }, 180);
        } else {
          // Snap back: the whole panel returns to rest and the parent
          // underlay retreats to its -30% rest, then both come down.
          const w = _navWidth();
          navAnimBusy = true;
          const tr = 'transform 180ms ease-out';
          sidebar.style.transition = tr;
          sidebar.style.transform = 'translate3d(0,0,0)';
          if (navUnderlay) {
            navUnderlay.style.transition = tr;
            navUnderlay.style.transform = 'translate3d(' + (-NAV_UNDERLAY_PARALLAX * w) + 'px,0,0)';
          }
          _resumeNavAnim('', tr);
          _scheduleNavDone(function(){
            _clearNavInlineStyles();
            teardownNavUnderlay();
            navAnimBusy = false;
            applyVisual();
          }, 180);
        }
      } else {
        teardownNavUnderlay();
        applyVisual();
      }
      return;
    }
    const hadPull = bottomPull > 0;
    hDragState = 'idle';
    hDragX = 0;
    const curBottom = bottomPull;
    bottomPull = 0;
    ptrLocked = null;
    hHapticFired = false;
    if (hadPull || curBottom > 0) {
      if (tileGrid) tileGrid.style.transition = 'transform ' + PTR_SNAP_MS + 'ms ease-out, opacity ' + PTR_SNAP_MS + 'ms ease-out';
      applyVisual();
      setTimeout(function(){ if(tileGrid) { tileGrid.style.transition=''; tileGrid.style.willChange=''; } }, PTR_SNAP_MS);
    } else {
      applyVisual();
    }
  }
  function reseedForSurvivingTouch(e){
    // Partial release: a finger is still down and the gesture continues for
    // it. The survivor must not inherit the original finger's deltas — a
    // stale start point could lock it 'h' instantly and re-arm the blocker.
    resetBrowseGesture();
    const t = e.touches[0];
    ptrStartX = t.clientX;
    ptrStartY = t.clientY;
    startScrollTop = sidebarScroll.scrollTop;
    updatePtrAtBottom();
  }
  sidebarScroll.addEventListener('touchend', function(e){
    if (e.touches && e.touches.length > 0) { reseedForSurvivingTouch(e); return; }
    handlePtrEnd();
  }, {passive: true});
  sidebarScroll.addEventListener('touchcancel', function(e){
    if (e.touches && e.touches.length > 0) { reseedForSurvivingTouch(e); return; }
    handlePtrEnd();
  }, {passive: true});
  // If the grid is rebuilt mid-gesture the touch's target is detached and its
  // touchend/touchcancel never reaches us — sweep any leaked blocker/visuals.
  if (tileGrid && window.MutationObserver) {
    new MutationObserver(function(){
      if (tmBlockArmed || hDragState !== 'idle' || bottomPull > 0 || hDragX !== 0 || ptrLocked) {
        resetBrowseGesture(false);
      }
    }).observe(tileGrid, {childList: true});
  }
  // A drag that armed the gesture visuals must not resolve as a tap on the
  // tile under the release point. The old non-passive touchmove did this via
  // preventDefault; with the passive tracker we swallow the click ourselves.
  if (tileGrid) tileGrid.addEventListener('click', function(e){
    if (!suppressGridClick) return;
    suppressGridClick = false;
    e.stopPropagation();
    if (e.cancelable) e.preventDefault();
  }, {capture: true});
  if(window.__BOOKQ_TEST_MODE__==='true'){ window.__ptrDebug={get hState(){return hDragState}, get bVisual(){return bottomPull}, get hX(){return hDragX}, get tmArmed(){return tmBlockArmed}, get ptrLock(){return ptrLocked}, get H_THRESH(){return H_SWIPE_THRESHOLD}, get H_DEACT(){return H_SWIPE_DEACTIVATE}, get underlay(){return !!navUnderlay}, get underHeader(){ try { return navUnderlaySlot ? (navUnderlaySlot.querySelector('#browse-header')||{}).textContent : null; } catch(e){ return null; } }, get navBusy(){return navAnimBusy}, injectH(v){ _finishNavAnimNow(); if(typeof v==='number'){ hDragX=v; hDragState=v>=H_SWIPE_THRESHOLD?'ready_to_pop':v>0?'draggingH':'idle'; if(v>0) ensurePopUnderlay(); else teardownNavUnderlay(); applyVisual(); } else { hDragState=v; applyVisual(); } }, injectBottom(v){ bottomPull=v; applyVisual(); }}; window.__testFilters={get filters(){return activeFilters}, set filters(v){activeFilters=v; try{updateFilterChips(); renderView();} catch(_) { console.warn(_); }}, clear(){activeFilters={artist:[],tag:[],sort:'relevance'}; try{updateFilterChips(); renderView();} catch(_) { console.warn(_); }}, get count(){return document.querySelectorAll('.tile').length}}; window.__orientDebug = { get runs(){ return orientRuns; } }; }

  imgContainer.addEventListener("dblclick", e => e.preventDefault());
  // H6: do NOT preventDefault() on multi-touch here — that also cancels the
  // browser's native pinch-zoom gesture, defeating H6's viewport fix exactly
  // over the page image. It isn't needed to stop a swipe starting during a
  // pinch either: the drag-tracking touchstart below already bails out via
  // its own `e.touches.length !== 1` check.

  // One home for the tap-vs-swipe line: a gesture whose whole travel stayed
  // at or under TAP_MAX_TRAVEL is a tap, not a swipe. The same number now
  // owns the drift-tap classification AND the distance fallback below (a fast
  // flick can arrive with unmeasurable velocity — start+move dispatched
  // within the same ms collapses dt — so the tap band must never swallow a
  // flick that the velocity branch fails to see). 20 matches the velocity
  // branch's |dxFinal| > 20 cut.
  const TAP_MAX_TRAVEL = 20;
  let dragActive = false, dragStartX = 0, dragStartY = 0, dragDecided = false, dragPx = 0;
  let dragTouchId = null;
  let viewerSwipeReady = false;
  let viewerHapticFired = false;
  let dragMaxTravel = 0;   // horizontal travel so far, for the tap test above
  let moveHistory = [];

  // Pure classifier for the decided-touchend gesture, extracted so tests can
  // pin every branch deterministically (CDP cannot deliver sub-~16px moves
  // and cannot force the dt<=1 same-ms case the branches exist for). Returns
  // {advanced, via} with via in 'swipe-ready' | 'velocity' | 'distance' |
  // 'drift-tap' | 'bounce'. Contract: TAP_MAX_TRAVEL owns the tap band
  // (travel <= cut is a tap); velocity/distance require |dxFinal| > cut;
  // `known` is sample-count based, so a multi-sample same-ms history
  // (velocity 0, known true) after a long travel bounces instead of falling
  // back — the hysteresis rule (test_viewer_swipe_hysteresis). The distance
  // branch is defense-in-depth for genuinely single-sample histories, which
  // the decided path cannot currently produce.
  function classifyTouchEnd(g){
    let advanced = 0, via = 'bounce';
    if (g.swipeReady) {
      if (g.dragPx > 0) advanced = 1;
      else if (g.dragPx < 0) advanced = -1;
      else if (g.dxFinal > 0) advanced = 1;
      else if (g.dxFinal < 0) advanced = -1;
      via = 'swipe-ready';
    } else if (Math.abs(g.velocity) > 0.05 && Math.abs(g.dxFinal) > TAP_MAX_TRAVEL
      && Math.sign(g.velocity) === Math.sign(g.dxFinal)) {
      advanced = g.velocity > 0 ? 1 : -1;
      via = 'velocity';
    } else if (!g.known && g.travel >= TAP_MAX_TRAVEL && Math.abs(g.dxFinal) > TAP_MAX_TRAVEL) {
      // Distance fallback: the flick was real (far enough) but the speedometer
      // produced no measurement (single-sample history or same-ms dispatch).
      // Direction by where the gesture ENDED, never by the starting zone.
      advanced = Math.sign(g.dxFinal);
      via = 'distance';
    }
    if (advanced === 0 && g.travel <= TAP_MAX_TRAVEL) via = 'drift-tap';
    return {advanced, via};
  }

  // preventDefault-ing touchend removes the browser's tap side effects along
  // with the synthesized click. The side effects other code depends on are
  // restored explicitly:
  const dropTapFocus = () => {
    // Scoped to focus already inside the gesture surface: a keyboard user
    // tabbed to a toolbar control who then touches the screen must keep
    // their focus — preventDefault() already stops the touch itself from
    // moving focus, so there is nothing outside the viewer to clean up.
    try {
      const a = document.activeElement;
      if (a && a !== document.body && typeof a.blur === 'function' && imgContainer.contains(a)) a.blur();
    } catch(e) {}
  };
  // offline/storeAdapter keeps a document-level 'click' closer for its
  // popovers / delete confirms; a viewer tap used to reach it through the
  // synthesized click, so keep delivering that dismissal.
  const dismissOutsidePopovers = () => {
    try { document.body.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true })); } catch(e) {}
  };

  // Give up on the current drag AND put the strip back at rest. Clearing the
  // flags alone is not enough: the offset written by the last touchmove stays
  // on screen, and a later touchend would commit a dxFinal measured from a
  // gesture the reader has already turned into something else.
  function abortDrag(){
    dragActive = false; dragTouchId = null;
    dragDecided = false; dragPx = 0; dragMaxTravel = 0;
    viewerSwipeReady = false; viewerHapticFired = false;
    moveHistory = [];
    setTrackOffset(currentIdx, 0);
  }

  // A LONE touch that landed within PALM_EDGE of the screen is a palm to
  // ViewerGesture until it moves ARM_PX, so its touchstart arms nothing. If it
  // then moves, it was the swipe finger all along (a thumb starting from the
  // right bezel is the natural RTL page-turn) — remember it so touchmove can
  // arm the drag late. Cleared when a real drag arms or the touch ends.
  let pendingPalmId = null;
  const findTouch = window.ViewerGesture.findTouch;
  function armDrag(t0, startX, startY){
    dragTouchId = t0.identifier;
    pendingPalmId = null;
    track.style.transition = "none";
    snapGen++;
    // Killing the transition freezes the strip on the slide's TARGET (the
    // inline transform is already the target; only the animation was pending),
    // but leaves `anchorSpread` on the spread the slide started from. Re-base
    // so the strip is at a true rest before anything else touches it — the
    // touch may well turn out to be the first finger of a pinch, and zoom.js
    // cannot reclaim the slide itself: a two-finger press arrives as two
    // separate touchstarts, and this one has already cleared `sliding` by the
    // time the pinch arms. Re-basing is visually identity.
    if (sliding) { sliding = false; rebaseAnchor(currentIdx); }
    dragStartX = startX;
    dragStartY = startY;
    dragActive = true; dragDecided = false; dragPx = 0; dragMaxTravel = 0;
    viewerSwipeReady = false;
    viewerHapticFired = false;
    moveHistory = [{x: t0.clientX, t: performance.now()}];
  }

  imgContainer.addEventListener("touchstart", e => {
    if (!images.length) return;
    if (window.Zoom.isActive()) return; // zoomed: zoom.js owns 1-finger pan (Task 5)
    const act = window.ViewerGesture.activeTouches(e);
    // A second finger landed mid-swipe: the reader is starting a pinch, not
    // continuing the drag. Abort it here rather than leaving dragActive true.
    if (act.length !== 1){
      if (dragActive) abortDrag();
      dragActive = false; dragTouchId = null;
      pendingPalmId = (act.length === 0 && e.touches.length === 1) ? e.touches[0].identifier : null;
      return;
    }
    // A palm landing on the bezel mid-swipe is hidden by activeTouches, so this
    // touchstart is still "one finger" — but it is the SAME finger that is
    // already dragging. Re-arming would reset dragStartX to wherever the finger
    // happens to be now and the strip would visibly jump back by that much.
    if (dragActive && act[0].identifier === dragTouchId) return;
    armDrag(act[0], act[0].clientX, act[0].clientY);
  }, {passive: true});

  imgContainer.addEventListener("touchmove", e => {
    if (window.Zoom.isActive()) return; // zoomed: zoom.js owns 1-finger pan (Task 5)
    if (!dragActive) {
      // Late arming: the lone edge-started touch has moved ARM_PX and stopped
      // being a palm — with no touchstart of its own. Measure the drag from
      // where it LANDED (ViewerGesture.startOf), so the promotion travel is
      // not charged to the reader.
      if (pendingPalmId === null || e.touches.length !== 1) return;
      const act = window.ViewerGesture.activeTouches(e);
      if (act.length !== 1 || act[0].identifier !== pendingPalmId) return;
      const s0 = window.ViewerGesture.startOf(pendingPalmId);
      armDrag(act[0], s0 ? s0.x : act[0].clientX, s0 ? s0.y : act[0].clientY);
    }
    // A resting palm that has now moved past ARM_PX is promoted to an active
    // touch here, with no touchstart of its own — the drag has become a pinch.
    // Aborting (rather than a bare `return`) is what keeps a later touchend
    // from committing the stale dxFinal this drag had accumulated.
    if (window.ViewerGesture.activeTouches(e).length !== 1) { abortDrag(); return; }
    const t0 = findTouch(e.touches, dragTouchId);
    if (!t0) return;
    const dx = t0.clientX - dragStartX;
    const dy = t0.clientY - dragStartY;
    if (!dragDecided){
      // Wait until intent is clearly horizontal or vertical.
      const ax = Math.abs(dx), ay = Math.abs(dy);
      if (ax < 8 && ay < 8) return;
      if (ax > ay) dragDecided = true;
      else { dragActive = false; return; }   // vertical → release to native
    }
    e.preventDefault();
    dragPx = dx;
    if (Math.abs(dx) > dragMaxTravel) dragMaxTravel = Math.abs(dx);
    let minIdx = 0, maxIdx = 0;
    if (spreads.length) {
      const cr = getChapterSpreadRange(getCurrentChapterInfo(currentIdx).chapterIdx);
      minIdx = spreads[cr.lo].pages[0];
      maxIdx = spreads[cr.hi].pages[0];
    }
    if (currentIdx >= maxIdx && dragPx > 0) dragPx *= 0.35;
    if (currentIdx <= minIdx && dragPx < 0) dragPx *= 0.35;
    const absPx = Math.abs(dragPx);
    const vThresh = Math.min(window.innerWidth * 0.10, VIEWER_SWIPE_THRESHOLD);
    const vDeact = Math.max(0, vThresh - 12);
    if (!viewerSwipeReady && absPx >= vThresh) {
      viewerSwipeReady = true;
      if (!viewerHapticFired) {
        try {
          const prefersReduced = prefersReducedMotion();
          if (!prefersReduced && 'vibrate' in navigator) navigator.vibrate(10);
        } catch(_) { console.warn(_); }
        viewerHapticFired = true;
      }
    } else if (viewerSwipeReady && absPx < vDeact) {
      viewerSwipeReady = false;
    }
    moveHistory.push({x: t0.clientX, t: performance.now()});
    if (moveHistory.length > 6) moveHistory.shift();
    setTrackOffset(currentIdx, dragPx);
  }, {passive: false});

  imgContainer.addEventListener("touchend", e => {
    if (window.Zoom.isActive()) return; // zoomed: zoom.js owns 1-finger pan (Task 5)
    if (!dragActive){
      // A never-promoted edge touch lifting: forget it, or the NEXT edge-started
      // touch could not register as pending. (Its tap still reaches the zones
      // through the synthesized click — nothing here suppressed it.)
      if (pendingPalmId !== null && findTouch(e.changedTouches, pendingPalmId)) pendingPalmId = null;
      return;
    }
    const released = findTouch(e.changedTouches, dragTouchId);
    if (!released) return;               // a palm lifted, the swipe finger is still down
    dragActive = false; dragTouchId = null;
    if (!dragDecided) {
      // A tap. Act on it NOW instead of waiting for the synthesized click:
      // WebKit delivers `click` ~40ms after touchend (measured in the iOS
      // Simulator), and that gap is the whole "did it register?" feeling on a
      // burst. preventDefault kills the synthesized click outright (measured
      // in WebKit); the suppression deadline is only a backstop after this.
      moveHistory = [];
      e.preventDefault();
      dropTapFocus();
      zoneActionAt(released.clientX);
      dismissOutsidePopovers();
      suppressClickFor(400);
      if (window.__BOOKQ_TEST_MODE__ === 'true') window.__lastTouchEndVia = 'tap';
      return;
    }
    const dxFinal = released.clientX - dragStartX;
    const threshold = Math.min(window.innerWidth * 0.10, 70);
    const curSpread = spreadOf(currentIdx);
    const maxSpread = Math.max(0, spreads.length - 1);

    // ── Calculate swipe velocity from recent move history ──
    // Skip the touchstart sample if we have enough move data (avoids
    // diluting velocity when the finger rests before swiping).
    let swipeVelocity = 0; // px/ms
    // velocityKnown means the speedometer HELD samples (length >= 2), NOT that
    // it measured: same-ms dispatch collapses dt and leaves velocity 0. That
    // must stay "known": a multi-sample same-ms history (e.g. the pull-back
    // cancel in test_viewer_swipe_hysteresis) has PROVEN the gesture is not a
    // flick and must bounce, never fall back to the distance branch below.
    // Gating this on dt instead would re-read a measured pull-back as a
    // forward flick by dxFinal alone. The distance fallback below stays gated
    // on genuinely single-sample histories (length < 2) — defense-in-depth
    // that the decided path cannot currently produce (armDrag seeds one
    // sample and the deciding move pushes another), pinned by classifier
    // unit tests rather than deleted.
    const velocityKnown = moveHistory.length >= 2;
    if (velocityKnown) {
      const startIdx = moveHistory.length > 3 ? 1 : 0;
      const first = moveHistory[startIdx];
      const last = moveHistory[moveHistory.length - 1];
      const dt = last.t - first.t;
      if (dt > 1) swipeVelocity = (last.x - first.x) / dt;
    }
    moveHistory = [];

    // Decided-touchend classification lives in classifyTouchEnd (above), so
    // the tap band, the velocity/distance cuts, and the pull-back rule have a
    // single home that tests pin directly via window.__classifyTouchEnd.
    const commit = classifyTouchEnd({
      swipeReady: viewerSwipeReady, dragPx,
      velocity: swipeVelocity, known: velocityKnown,
      dxFinal, travel: dragMaxTravel,
    });
    const advanced = commit.advanced;
    if (window.__BOOKQ_TEST_MODE__ === 'true') window.__lastTouchEndVia = commit.via;
    const idxBefore = currentIdx;

    if (commit.via === 'drift-tap') {
      // A planar tap, by travel — the single tap test. dragMaxTravel guards
      // the gesture as a whole, not just where the finger happened to stop: a
      // fast 100px drag pulled back to 10px must not re-classify as a tap
      // just because the END of the journey is small (a pull-back cancel).
      // The zone resolves from the LANDING spot, consistent with the
      // stationary-tap path above.
      e.preventDefault();
      dropTapFocus();
      dismissOutsidePopovers();
      zoneActionAt(released.clientX);
      // The strip was dragged up to TAP_MAX_TRAVEL px sideways by touchmove —
      // a turn re-anchors it, but a center-zone tap or a clamp-edge early
      // return turns nothing and would leave the offset behind.
      if (currentIdx === idxBefore) snapTo(currentIdx);
      suppressClickFor(400);
      return;
    }
    let newSpread = Math.max(0, Math.min(curSpread + advanced, maxSpread));
    if (spreads.length) {
      const cr = getChapterSpreadRange(getCurrentChapterInfo(currentIdx).chapterIdx);
      newSpread = Math.max(cr.lo, Math.min(newSpread, cr.hi));
    }
    const newIdx = spreads[newSpread] ? spreads[newSpread].pages[0] : currentIdx;

    if (advanced !== 0) {
      // One committed swipe = one turn request. The display work (slide to
      // the target page + the deferred snapTo finalizer) runs inside the
      // navigation handler; a remote committed state can still supersede
      // that finalizer via ViewerNavigation.generation().
      window.ViewerNavigation.request({type: "turn", delta: advanced, crossChapter: false, fromChapter: navFromChapter(), via: "swipe"});
    }
    if (newIdx !== currentIdx) {
      e.preventDefault();
      dropTapFocus();
      dismissOutsidePopovers();
      suppressClickFor(400);
    } else {
      // Cancelled drag: WebKit can still synthesize a click for this gesture
      // and turn the page after the reader cancelled — preventDefault ends that.
      e.preventDefault();
      dismissOutsidePopovers();
      if (advanced === 0) snapTo(newIdx);   // same page: bounce-back animation
      // advanced !== 0 clamped at a chapter/book edge lands here too — the
      // handler's commitSwipeTurn already ran the same bounce-back inside
      // request() above.
    }
  });

  imgContainer.addEventListener("touchcancel", e => {
    if (window.Zoom.isActive()) return; // zoomed: zoom.js owns 1-finger pan (Task 5)
    if (!dragActive) {
      if (pendingPalmId !== null && findTouch(e.changedTouches, pendingPalmId)) pendingPalmId = null;
      return;
    }
    const released = findTouch(e.changedTouches, dragTouchId);
    if (!released) return;               // a palm lifted, the swipe finger is still down
    dragActive = false; dragTouchId = null;
    viewerSwipeReady = false;
    viewerHapticFired = false;
    moveHistory = [];
    if (dragDecided) snapTo(currentIdx);
  });

  // ── footer: toggle tile ↔ list ↔ card ──
  const footerBtn = document.getElementById("footer-refresh-btn");
  function updateFooterBtn(){
    const labels = { tile: "🎨", list: "📋", card: "🃏" };
    footerBtn.textContent = labels[viewMode] || "🎨";
    footerBtn.title = "表示: " + ({tile:"タイル",list:"リスト",card:"カード"}[viewMode] || viewMode);
  }
  try { window.updateFooterBtn = updateFooterBtn; } catch(e) { console.warn(e); }
  footerBtn.addEventListener("click", async () => {
    const modes = ["tile", "list", "card"];
    const idx = modes.indexOf(viewMode);
    viewMode = modes[(idx + 1) % modes.length];
    updateFooterBtn();
    try { await refreshOfflineMap(); } catch(e) { console.warn(e); }
    renderView();
    window.HashRouter.writeBrowseStateReplace();
  });
  updateFooterBtn();

  // ── continue-reading button ──
  function updateResumeButton() {
    if (!sidebar.classList.contains("open")) {
      hideResumeBtn();
      return;
    }
    const all = ReadingProgress.getAll();
    // 通常モードでは非表示 root の履歴を候補から外す（履歴データ自体は消さない）。
    const FV = window.FolderVisibility;
    const visOk = (FV && !FV.showAll) ? (p) => FV.isVisible(p) : () => true;
    const entries = Object.entries(all).filter(e => visOk(e[0])).sort((a, b) => (b[1].ts || 0) - (a[1].ts || 0));
    if (entries.length === 0) {
      hideResumeBtn();
      return;
    }
    const [folderPath, data] = entries[0];
    const bookName = folderPath.split("/").pop();
    resumeBtnText.textContent = `📖 続きから: ${truncateMiddle(bookName)} p.${data.page + 1}`;
    showResumeBtn();
    resumeBtn.onclick = () => navOpenWork(folderPath, "resume");
  }

  // ── server-held resume point: pull once at boot, merge by ts (LWW) ──
  // The server keeps ONE record per user. A record newer than the matching
  // local entry lands in localStorage (the pill and that tile's progress bar
  // both read the map, and loadImages resumes from it). A strictly newer
  // LOCAL newest (read while offline, or after a server restart wiped the
  // in-memory map) is pushed up so the server catches up — one extra PUT at
  // most per boot. Fires in parallel with loadFolders; a remote landing
  // mid-render still reaches the pill via updateResumeButton below and the
  // tile bar via a guarded renderView.
  // Single-flight: bfcache restore fires BOTH pageshow(persisted) and
  // visibilitychange(visible) — without the guard two pulls (and two
  // merge/render passes) run side by side.
  let _syncInFlight = null;
  function _syncRemoteProgress() {
    if (_syncInFlight) return _syncInFlight;
    _syncInFlight = _syncRemoteProgressRun()
      .finally(() => { _syncInFlight = null; });
    return _syncInFlight;
  }
  async function _syncRemoteProgressRun() {
    if (!window.ProgressSync) return;
    let res = null;
    try { res = await window.ProgressSync.pull(); } catch(e) { console.warn(e); }
    // pull() distinguishes "server empty" from "fetch failed" — only a
    // confirmed-empty server gets the local newest pushed back up. Pushing
    // blind after a failed GET would clobber a newer record another
    // device wrote (the server also ts-compares as a backstop).
    if (!res || !res.ok) return;
    const pos = res.position;
    const all = ReadingProgress.getAllSafe();
    let merged = false;
    if (pos) {
      const local = all[pos.folder];
      if (!local || (local.ts || 0) < (pos.ts || 0)) {
        try {
          ReadingProgress.mergeRemote(pos.folder, pos.page, pos.total || 0, pos.ts || 0);
          merged = true;
        } catch(e) { console.warn(e); }
      }
    }
    const entries = Object.entries(all);
    if (entries.length) {
      const [newestPath, newestData] = entries.sort((a, b) => (b[1].ts || 0) - (a[1].ts || 0))[0];
      if (newestData && typeof newestData.page === "number" && (!pos || (newestData.ts || 0) > (pos.ts || 0))) {
        window.ProgressSync.push({ folder: newestPath, page: newestData.page, total: newestData.total, ts: newestData.ts });
      }
    }
    if (merged) {
      updateResumeButton();
      if (!currentFolder && tree.length && sidebar.classList.contains("open")) renderView();
    }
  }

  // ── save progress on tab visibility change ──
  // save() only queues a 2s trailing-edge PUT, which dies with the page —
  // flush() sends it immediately (keepalive outlives the document). This
  // must run AFTER our own save: ProgressSync's self-registered listeners
  // fire earlier (document-level + defer order), so they can only flush
  // whatever was already pending.
  function _saveCurrentPosition() {
    if (!currentFolder || !images.length) return;
    let path, page, total;
    if (chapterList.length > 1) {
      const info = getCurrentChapterInfo(currentIdx);
      const chapter = chapterList[info.chapterIdx];
      if (!chapter) { if (window.ProgressSync) window.ProgressSync.flush(); return; }
      path = chapter.fullPath; page = info.localIdx; total = chapter.images.length;
    } else {
      path = currentFolder; page = currentIdx; total = images.length;
    }
    // An unchanged position must NOT be re-saved: save() would restamp the
    // localStorage ts to now, and after a reload (in-memory dedup gone) the
    // boot self-heal would push this stale view over another device's newer
    // record. Skipping the save also keeps the pulled remote entry ahead in
    // the local recency sort. flush() still runs so a real pending send
    // (e.g. an armed debounce from the last turn) goes out now.
    const prev = ReadingProgress.get(path);
    if (!prev || prev.page !== page || prev.total !== total) {
      ReadingProgress.save(path, page, total);
    }
    if (window.ProgressSync) window.ProgressSync.flush();
  }
  window.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') {
      _saveCurrentPosition();
    } else if (document.visibilityState === 'visible') {
      // Back in the foreground: another device may have moved the shared
      // slot while we were away — merge it BEFORE any further save here
      // can assert a stale position.
      _syncRemoteProgress();
    }
  });
  window.addEventListener('pagehide', _saveCurrentPosition);
  // bfcache restore: same re-pull as returning to the foreground.
  window.addEventListener('pageshow', (e) => { if (e.persisted) _syncRemoteProgress(); });

  // hash listener owned by browse/hash.js — do not add second listener here

  function shouldAutoSync(){
    if(!treeLoaded) return false;
    if(typeof navigator!=='undefined' && !navigator.onLine) return false;
    try{ if(sessionStorage.getItem('bookq_auto_sync_done')) return false; } catch(e) { console.warn(e); }
    try{ sessionStorage.setItem('bookq_auto_sync_done','1'); } catch(e) { console.warn(e); }
    return true;
  }
  function tryAutoSync(){
    try{
      if(!shouldAutoSync()) return;
      revalidateFoldersForOnline().catch(()=>{});
      scheduleThumbPrefetch();
    } catch(e) { console.warn(e); }
  }
  window.tryAutoSync = tryAutoSync;
  // Test/QA entry point. Legacy window.* aliases above stay for compat;
  // new harnesses should use this namespace instead of per-symbol globals.
  window.__BOOKQ_DEBUG = {
    revalidateFoldersForOnline, fetchLatestFolders, tryAutoSync, shouldAutoSync, prefetchThumbs,
    getParentPath, findNodeByPath, folderHasOnlyImages, getSiblingLeafFolders,
    SIBLING_AUTOLOAD_MIN_DEPTH, truncateMiddle,
    setFullPathsAndIndex,
    get etagByKey(){ return etagByKey; },
    setTree(v){ tree=v; setFullPathsAndIndex(tree); invalidateLeafPaths(); updateBrowseHeader(); renderView(); },
    getTree(){ return tree; },
    get treeLoaded(){ return treeLoaded; },
    set treeLoaded(v){ treeLoaded=v; },
    get shallow(){ return _shallowMode; },
  };

  // ── viewer footer version (once, silent fail) ──
  let _appVersionLoaded = false;
  async function loadAppVersion(){
    if(_appVersionLoaded) return;
    _appVersionLoaded = true;
    try{
      const r = await fetch(globalThis.BookqApi.endpoints.version, {cache:'no-store'});
      if(!r.ok) return;
      const j = await r.json();
      const version = j.version || "";
      const shaRaw = j.sha || "";
      const dateRaw = j.build_date || "";
      const sha = (shaRaw && shaRaw !== "unknown") ? shaRaw.slice(0,7) : "";
      const date = (dateRaw && dateRaw !== "unknown" && /^\d{4}-\d{2}-\d{2}/.test(dateRaw)) ? dateRaw.slice(0,10) : "";
      const parts = [version, sha, date].filter(Boolean);
      const text = parts.join(" ");
      const el = document.getElementById('app-version');
      if(el){
        el.textContent = text;
        el.title = `version: ${j.version||'unknown'}\nsha: ${j.sha||'unknown'}\nbuild_date: ${j.build_date||'unknown'}`;
      }
    }catch{}
  }
  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', loadAppVersion, {once:true});
  } else {
    loadAppVersion();
  }

  // ── init ──
  updateOrientation();
  // Restore a deep link stashed across the OAuth round-trip (the login branch's
  // script writes bookq_return_hash). A stale stash from an abandoned hop
  // (e.g. access_denied) older than 10 minutes is discarded, not restored.
  let savedStash = null;
  try { savedStash = JSON.parse(sessionStorage.getItem("bookq_return_hash") || "null"); } catch(e) { console.warn(e); savedStash = null; }
  const savedHash = (savedStash && Date.now() - savedStash.ts < 10 * 60 * 1000) ? savedStash.hash : null;
  if (savedHash && !location.hash) history.replaceState(null, "", location.pathname + location.search + savedHash);
  sessionStorage.removeItem("bookq_return_hash");

  // ── folder visibility (非表示トップ root) wiring ──
  // onChange: 同じ snapshot/モードのまま再描画し、通常モードで現在の閲覧・
  // 待機中の移動が禁止対象になったらトップへ戻す。判断が ready/readonly
  // へ立つたびに drain する — 起動時だけでなく error→ready の復旧や
  // 再同期でも、キューした遷移をここで解放する（drain 先は各ガードを
  // 再評価するので hidden 対象はその先で拒否される）。
  function _visibilityOnChange(){
    try {
      var FV = window.FolderVisibility;
      if (FV){
        var _st = FV.readyState;
        if (_st === "ready" || _st === "readonly"){
          var qn = _pendingNav; _pendingNav = null;
          if (qn){
            if (qn.kind === "view"){ loadImages(qn.path); }
            else { navigateToFolder(qn.path); }
          }
          // pending-nav drain の後: 判定待ちで保持した pair commit も同じ
          // エッジで再適用（deny/適用は _applyCommitted が再評価する）。
          try {
            if (window.PairController &&
                typeof window.PairController.retryPendingCommit === "function")
              window.PairController.retryPendingCommit();
          } catch(e2){ console.warn(e2); }
          if (qn) return;
        }
        if (!FV.showAll && _st !== "loading"){
          var targets = [(_pendingNav && _pendingNav.path), currentFolder, currentBrowsePath];
          for (var i = 0; i < targets.length; i++){
            var p = targets[i];
            if (p && FV.isHidden(p)) { redirectHiddenToTop(); return; }
          }
        }
        // 検索結果はサーバ側で hidden roots を除いている — モード/スナップ
        // ショットの切替で一覧が変わるので検索中は再取得する（doSearch が
        // 自身で renderView まで行う）。
        if (searchMode && typeof doSearch === "function"){
          try { doSearch(); return; } catch(e2){ console.warn(e2); }
        }
      }
      renderView();
    } catch(e){ console.warn(e); }
  }
  if (window.FolderVisibility){
    window.FolderVisibility.init({ owner: window.__BOOKQ_VISIBILITY_OWNER__, snapshot: window.__BOOKQ_VISIBILITY_SNAPSHOT__, onChange: _visibilityOnChange });
    window.FolderVisibility.ready().then(function(){
      // 設定GET待ちに溜めた移動・復元を解放し、読込中表示を実グリッドへ置換
      var qn = _pendingNav; _pendingNav = null;
      if (qn){
        if (qn.kind === "view"){ loadImages(qn.path); }
        else { navigateToFolder(qn.path); }
      }
      renderView();
    });
    // wiring 完了の結合マーカー（watchdog の混在検査対象）
    window.__BOOKQ_VISIBILITY = true;
  }

  loadFolders().then(()=>{ (window.FolderVisibility ? window.FolderVisibility.ready() : Promise.resolve()).then(()=>{ applyUrlState(); tryAutoSync(); }); });
  loadFacets();
  // Server-held resume point: pull + merge in parallel with the tree load —
  // a deep-linked viewer already open is untouched (the merge only writes
  // localStorage), and the pill/tile refresh is guarded by merged.
  _syncRemoteProgress();
})();
