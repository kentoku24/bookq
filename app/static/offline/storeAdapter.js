/* bookq offline/storeAdapter — download/delete/progress & offline UI.
 * Plain global script: defines window.downloadWork, deleteWork, getOfflineBtnState,
 * offlineControlsHtml etc. Depends on window.tree, window.OfflineStore etc.
 */
(function () {
  var OfflineStore = window.OfflineStore;
  // escapeHtml / folderHasOnlyImages come from offline-core.js, which
  // index.html's defer chain loads before this file — single definition, no
  // local copy.
  var _escapeHtml = window.OfflineCore.escapeHtml;
  var folderHasOnlyImages = window.OfflineCore.folderHasOnlyImages;

  // ── leaf paths (pure in tree, memoized) ──
  function _resolveDeps(deps){
    deps = deps || {};
    return {
      fetch: deps.fetch || (typeof window !== 'undefined' && window.fetch ? window.fetch.bind(window) : fetch),
      caches: deps.caches || (typeof window !== 'undefined' ? window.caches : (typeof caches !== 'undefined' ? caches : undefined)),
      storage: deps.storage || (typeof navigator !== 'undefined' ? navigator.storage : undefined),
      OfflineStore: deps.OfflineStore || window.OfflineStore
    };
  }
  var leafPathsCache = new Map();
  function invalidateLeafPaths(){ leafPathsCache = new Map(); }
  function collectLeafPaths(node, out){
    if(folderHasOnlyImages(node)){ out.push(node._fullPath); return; }
    if(node.children){
      for(var c of node.children){ if(c.type==="folder") collectLeafPaths(c, out); }
    }
  }
  function computeLeafPathsFor(folder, deps){
    var tree = (deps && deps.tree !== undefined) ? deps.tree : (window.tree || []);
    var _depsGetNodeAt = (deps && deps.getNodeAt) ? deps.getNodeAt : null;
    function getNodeAt(p){
      if (_depsGetNodeAt) { try { return _depsGetNodeAt(p); } catch(e) { console.warn(e); } }
      if (window.getNodeAt) { try { return window.getNodeAt(p); } catch(e) { console.warn(e); } }
      // fallback walk
      function find(nodes, target){
        for(var n of nodes){
          if(n._fullPath===target) return n;
          if(n.children && target.startsWith(n._fullPath+"/")){
            var f=find(n.children,target); if(f) return f;
          }
        } return null;
      }
      return find(tree, folder);
    }
    var node = getNodeAt(folder);
    if(node){
      if(folderHasOnlyImages(node)) return [folder];
      var tmp=[];
      for(var c of (node.children||[])){ if(c.type==="folder") collectLeafPaths(c, tmp); }
      var hasDirectImages = node.children && node.children.some(function(c){return c.type==="image";});
      if(tmp.length===0) return [folder];
      var dedup=[...new Set(tmp)];
      if(hasDirectImages) dedup.unshift(folder);
      return dedup;
    }
    var leaves=[];
    (function walk(nodes){
      for(var n of nodes){
        if(n._fullPath===folder || n._fullPath.startsWith(folder+"/")){
          var isLeaf = folderHasOnlyImages(n);
          if(isLeaf) leaves.push(n._fullPath);
        }
        if(n.children) walk(n.children);
      }
    })(tree);
    if(leaves.length) return [...new Set(leaves)];
    return [folder];
  }
  function getLeafPathsFor(folder, deps){
    if(deps && deps.tree !== undefined) return computeLeafPathsFor(folder, deps);
    var cached = leafPathsCache.get(folder);
    if(cached) return cached;
    var computed = computeLeafPathsFor(folder, deps);
    leafPathsCache.set(folder, computed);
    return computed;
  }

  var downloadControllers = new Map();
  window.downloadControllers = downloadControllers;
  var OfflineBtnState = {
    IDLE: "idle",
    DOWNLOADING: "downloading",
    STOPPING: "stopping",
    COMPLETE: "complete",
    DELETING: "deleting",
    PARTIAL: "partial",
    CONFIRM_DELETE: "confirm-delete"
  };
  var _downloadingSet = new Set();
  var _stoppingSet = new Set();
  window._stoppingSet = _stoppingSet;
  var _listingSet = new Set();
  window._listingSet = _listingSet;
  var _deletingSet = new Set();
  window._deletingSet = _deletingSet;
  var _confirmMap = new Map();
  window._confirmMap = _confirmMap;

  // Need offlineMap etc from app — they live on window
  function _getOfflineMap(){ return window.offlineMap || {}; }
  function _getOfflineRecordMap(){ return window.offlineRecordMap || new Map(); }

  // ── Todo 4 ctx-aware read path (additive; write path untouched) ──
  // A render builds ONE ctx per renderView call (see browse/render.js
  // _buildRenderCtx): {leafMap, offlineMap, offlineRecordMap}. Per-tile code
  // then does map lookups only — no per-tile getLeafPathsFor walks, no O(L²)
  // leaves.some(startsWith) scans. The ctx is render-scoped (never cached
  // across renders), so there is nothing stale to invalidate: every
  // renderView triggered by _downloadingSet/progress events or a
  // refreshOfflineMap resolution rebuilds it, and mid-download renders
  // re-fetch the live window maps.
  function _leafMost(leaves){
    // Semantically identical to the old per-leaf
    // leaves.some(other => other!==lp && other.startsWith(lp+'/')) skip, but
    // O(total depth) via one ancestor-set build instead of O(L²).
    var ancestors = {};
    for(var i=0;i<leaves.length;i++){
      var lp = leaves[i];
      for(var s=lp.indexOf('/'); s>=0; s=lp.indexOf('/', s+1)){
        ancestors[lp.slice(0, s)] = true;
      }
    }
    return leaves.filter(function(lp){ return !ancestors[lp]; });
  }
  function _ctxLeaves(folder, ctx){
    if(ctx && ctx.leafMap && ctx.leafMap[folder]) return ctx.leafMap[folder];
    return getLeafPathsFor(folder);
  }
  function _ctxOfflineMap(ctx){ return (ctx && ctx.offlineMap) || _getOfflineMap(); }
  function _ctxRecordMap(ctx){
    var orm = (ctx && ctx.offlineRecordMap) || _getOfflineRecordMap();
    // Failure-matrix guard: a non-Map stub (or corrupt shape) degrades to
    // empty instead of throwing .get on undefined.
    if(orm && typeof orm.get === 'function') return orm;
    return new Map();
  }
  function getPartialProgressCtx(folder, ctx){
    var leaves = _ctxLeaves(folder, ctx);
    var cachedCount = 0, pageCount = 0;
    var cachedLeaves = 0;
    var orm = _ctxRecordMap(ctx);
    var om = _ctxOfflineMap(ctx);
    var top = _leafMost(leaves);
    for(var k=0;k<top.length;k++){
      var lp = top[k];
      var rec = orm.get(lp);
      if(rec){
        cachedCount += (rec.cachedCount || 0);
        pageCount += (rec.pageCount || 0);
        if((rec.cachedCount||0) > 0) cachedLeaves += 1;
      } else if(om[lp]){
        cachedLeaves += 1;
      }
    }
    if(pageCount===0){
      var total = leaves.length;
      var pct = total ? cachedLeaves/total : 0;
      return {cachedCount: cachedLeaves, pageCount: total, pct};
    }
    var pct2 = pageCount ? cachedCount/pageCount : 0;
    return {cachedCount, pageCount, pct: pct2};
  }
  function isFolderOfflineCtx(folder, ctx){
    var om = _ctxOfflineMap(ctx);
    if(om[folder]) return true;
    var leaves = _ctxLeaves(folder, ctx);
    if(leaves.length>1 || (leaves.length===1 && leaves[0]!==folder)){
      return leaves.length>0 && leaves.every(function(lp){ return !!om[lp]; });
    }
    return false;
  }
  function getOfflineBtnStateCtx(folder, ctx){
    if(_confirmMap.has(folder)){
      var k = _confirmMap.get(folder);
      if(k === 'delete') return OfflineBtnState.CONFIRM_DELETE;
    }
    if(_deletingSet.has(folder)) return OfflineBtnState.DELETING;
    if(_stoppingSet.has(folder)) return OfflineBtnState.STOPPING;
    if(_downloadingSet.has(folder)) return OfflineBtnState.DOWNLOADING;
    var prog = getPartialProgressCtx(folder, ctx);
    if(prog && prog.pct>0 && prog.pct<1) return OfflineBtnState.PARTIAL;
    if(isFolderOfflineCtx(folder, ctx)) return OfflineBtnState.COMPLETE;
    return OfflineBtnState.IDLE;
  }
  function getPartialProgress(folder){
    return getPartialProgressCtx(folder, null);
  }
  function isFolderOffline(folder){
    return isFolderOfflineCtx(folder, null);
  }
  function getOfflineBtnState(folder){
    return getOfflineBtnStateCtx(folder, null);
  }
  window.OfflineBtnState = OfflineBtnState;
  window.getPartialProgress = getPartialProgress;
  window.getPartialProgressCtx = getPartialProgressCtx;
  window.isFolderOfflineCtx = isFolderOfflineCtx;
  window.getOfflineBtnState = getOfflineBtnState;
  window.isFolderOffline = isFolderOffline;
  window.getLeafPathsFor = getLeafPathsFor;
  window.invalidateLeafPaths = invalidateLeafPaths;


  var _quotaModalPromise = null;
  function showQuotaConfirm(opts){
    var folder = opts && opts.folder, megabytes = opts && opts.megabytes;
    if(_quotaModalPromise) return _quotaModalPromise;
    _quotaModalPromise = new Promise(function(resolve){
      var backdrop=document.createElement('div');
      backdrop.className='quota-modal-backdrop';
      var card=document.createElement('div');
      card.className='quota-modal';
      card.setAttribute('role','dialog');
      card.setAttribute('aria-modal','true');
      card.setAttribute('aria-labelledby','quota-modal-title');
      card.innerHTML=
        '<h3 id="quota-modal-title" class="quota-modal-title">保存容量が不足しています</h3>'+
        '<p class="quota-modal-body">最も古い作品『'+_escapeHtml(folder)+'』（約 '+megabytes+' MB）を削除して続行しますか？</p>'+
        '<div class="quota-modal-actions">'+
          '<button type="button" class="quota-ok">削除して続行</button>'+
          '<button type="button" class="quota-cancel">キャンセル</button>'+
        '</div>';
      backdrop.appendChild(card);
      document.body.appendChild(backdrop);
      var settled=false;
      var done=function(val){
        if(settled) return;
        settled=true;
        document.removeEventListener('keydown', onKey, true);
        try{ backdrop.remove(); } catch(e) { console.warn(e); }
        _quotaModalPromise=null;
        resolve(val);
      };
      var onKey=function(ev){
        if(ev.key==='Escape'){ ev.preventDefault(); ev.stopPropagation(); done(false); }
      };
      document.addEventListener('keydown', onKey, true);
      backdrop.addEventListener('click',function(ev){ if(ev.target===backdrop) done(false); });
      backdrop.querySelector('.quota-ok').addEventListener('click',function(){done(true);});
      backdrop.querySelector('.quota-cancel').addEventListener('click',function(){done(false);});
      backdrop.querySelector('.quota-ok').focus();
    });
    return _quotaModalPromise;
  }
  window.showQuotaConfirm = showQuotaConfirm;

  function _updateProgress(folder,pct){
    if (window.updateProgress && window.updateProgress !== _updateProgress) {
      // delegate to app's implementation if it wraps correctly (after app loads)
      // but to avoid recursion, call directly if not same
      try { window.updateProgress(folder,pct); return; } catch(e) { console.warn(e); }
    }
    try{
      var esc = (typeof CSS !== 'undefined' && CSS.escape) ? CSS.escape(folder) : folder.replace(/["\\]/g,'\\$&');
      var els = document.querySelectorAll('.offline-btn[data-folder="'+esc+'"] .progress-ring');
      if(!els.length) els = document.querySelectorAll('.offline-btn[data-path="'+esc+'"] .progress-ring');
      if(!els.length) els = document.querySelectorAll('[data-folder="'+esc+'"] .progress-ring');
      if(!els.length) els = document.querySelectorAll('[data-path="'+esc+'"] .progress-ring');
      if(!els.length){
        els = Array.from(document.querySelectorAll('.offline-btn .progress-ring')).filter(function(ring){
          var btn = ring.closest('.offline-btn');
          return btn && btn.dataset.folder === folder;
        });
      }
      els.forEach(function(el){ el.style.setProperty('--progress', pct*360+'deg'); });
    } catch(e) { console.warn(e); }
  }

  async function downloadWork(folder, deps){
    var _d = _resolveDeps(deps);
    var _fetch = _d.fetch;
    var _caches = _d.caches || (typeof caches !== 'undefined' ? caches : undefined);
    var _Store = _d.OfflineStore || window.OfflineStore;
    var _persistFn = window.ensurePersist || window.__ensurePersist;
    if (_persistFn) try { _persistFn(_d); } catch(e) { console.warn(e); }
    if(downloadControllers.has(folder)) return;
    var controller = new AbortController();
    var signal = controller.signal;
    downloadControllers.set(folder, controller);
    _downloadingSet.add(folder);
    try{ if(window.renderView) window.renderView(); } catch(e) { console.warn(e); }
    var throwIfAborted = function(){ if(signal.aborted) throw new DOMException("The operation was aborted.", "AbortError"); };
    var leafPaths = getLeafPathsFor(folder, deps);
    var targets = leafPaths;
    var totalFiles = 0;
    var leafFileMap = new Map();
    async function fetchLeafFiles(lp){
      throwIfAborted();
      var res = await _fetch("/api/images?folder="+encodeURIComponent(lp), {signal});
      if(!res.ok) throw new Error("list failed "+res.status+" for "+lp);
      var files = await res.json();
      throwIfAborted();
      return files;
    }
    async function cacheLeaf(lp, files, cache, onFile){
      var bytes = 0;
      var cachedCount = 0;
      var responseBytes = async function(response){
        var headerLen = Number(response.headers.get('content-length')||0);
        if(headerLen) return headerLen;
        try { return (await response.clone().blob()).size; } catch(e) { if (window.__BOOKQ_DEBUG) console.warn(e); return 0; }
      };
      for(var i=0;i<files.length;i++){
        throwIfAborted();
        var f=files[i];
        var url=window.OfflineCore.imageUrl(lp, f);
        var cached = await cache.match(url);
        if(cached){
          var len = await responseBytes(cached);
          bytes += len;
          cachedCount++;
          if(onFile) onFile(i, len);
          continue;
        }
        var putSucceeded = false;
        var cacheClone = null;
        var sizeClone = null;
        var r = null;
        try{
          r=await _fetch(url,{credentials:'same-origin', signal});
          if(!r.ok) throw new Error("fetch "+f+" in "+lp);
          cacheClone=r.clone();
          sizeClone=r.clone();
          try{ await cache.put(url, cacheClone); putSucceeded=true; } catch(putErr){ throw putErr; }
          var len2 = await responseBytes(sizeClone);
          bytes += len2;
          cachedCount++;
          if(onFile) onFile(i, len2);
        }catch(e){
          if(e && e.name==='AbortError'){
            if(putSucceeded){
              try{ var cdel = await _caches.open(_Store.cacheNameFor(lp)); await cdel.delete(url); } catch(e) { console.warn(e); }
            }
            throw e;
          }
          throw e;
        }
      }
      return {bytes, cachedCount};
    }
    var done = 0;
    var leafBytesPartial = 0;
    var leafCachedPartial = 0;
    try {
      _listingSet.add(folder);
      try{ if(window.renderView) window.renderView(); } catch(e) { console.warn(e); }
      for(var lp of targets){
        throwIfAborted();
        var files = await fetchLeafFiles(lp);
        leafFileMap.set(lp, files);
        totalFiles += files.length;
      }
      _listingSet.delete(folder);
      try{ if(window.renderView) window.renderView(); } catch(e) { console.warn(e); }
      if(totalFiles===0){
        if(window.showErrorToast) window.showErrorToast('DL失敗: 画像が見つかりません');
        return;
      }
      for(var lp2 of targets){
        throwIfAborted();
        var files2 = leafFileMap.get(lp2) || [];
        if(!files2.length) continue;
        var cacheName = _Store.cacheNameFor(lp2);
        var cache = await _caches.open(cacheName);
        leafBytesPartial = 0;
        leafCachedPartial = 0;
        try{
          var result = await cacheLeaf(lp2, files2, cache, function(i, len){
            done++;
            leafBytesPartial += (len||0);
            leafCachedPartial++;
            _updateProgress(folder, done/totalFiles);
            if(lp2!==folder) _updateProgress(lp2, (i+1)/files2.length);
          });
          var existing = await _Store.get(lp2);
          await _Store.put({folder: lp2, cacheName, pageCount: files2.length, cachedCount: result.cachedCount, bytes: result.bytes, thumb: files2[0]||null, addedAt: existing ? existing.addedAt : Date.now(), lastReadAt: Date.now()});
        }catch(leafErr){
          if((leafErr && leafErr.name==='AbortError') || signal.aborted){
            if(leafCachedPartial>0){
              var existing2 = await _Store.get(lp2);
              var thumb = files2[0]||null;
              await _Store.put({folder: lp2, cacheName, pageCount: files2.length, cachedCount: leafCachedPartial, bytes: leafBytesPartial, thumb, addedAt: existing2 ? existing2.addedAt : Date.now(), lastReadAt: Date.now()});
            }
            throw leafErr;
          }
          throw leafErr;
        }
      }
      if(window.updateStorageBar) await window.updateStorageBar(_d);
      if(window.refreshOfflineMap) await window.refreshOfflineMap();
      if(window.renderView) window.renderView();
      if(window.showToastSuccess) window.showToastSuccess('DL完了');
      _updateProgress(folder, 1);
    } catch(e){
      if(e && (e.name==='AbortError' || signal.aborted)){
        try{ if(window.updateStorageBar) await window.updateStorageBar(_d); if(window.refreshOfflineMap) await window.refreshOfflineMap(); if(window.renderView) window.renderView(); } catch(e) { console.warn(e); }
        return;
      }
      if(e && e.name==='QuotaExceededError' && !signal.aborted){
        var oldest=await _Store.getOldest();
        if(oldest && await showQuotaConfirm({folder: oldest.folder, megabytes: Math.round(oldest.bytes/1e6)})){
          await _caches.delete(oldest.cacheName);
          await _Store.delete(oldest.folder);
          if(window.updateStorageBar) await window.updateStorageBar(_d);
          try {
            for(var lp3 of targets){
              var files3 = await fetchLeafFiles(lp3);
              if(!files3.length) continue;
              var cache3 = await _caches.open(_Store.cacheNameFor(lp3));
              var retryResult = await cacheLeaf(lp3, files3, cache3, null);
              var existing3 = await _Store.get(lp3);
              await _Store.put({folder: lp3, cacheName: _Store.cacheNameFor(lp3), pageCount: files3.length, cachedCount: retryResult.cachedCount, bytes: retryResult.bytes, thumb: files3[0]||null, addedAt: existing3 ? existing3.addedAt : Date.now(), lastReadAt: Date.now()});
            }
            if(window.updateStorageBar) await window.updateStorageBar(_d);
            if(window.refreshOfflineMap) await window.refreshOfflineMap();
            if(window.renderView) window.renderView();
          } catch(e2){
            if(e2 && (e2.name==='AbortError' || signal.aborted)) return;
            if(window.showErrorToast) window.showErrorToast('容量不足のため中断'); return;
          }
        } else { if(window.showErrorToast) window.showErrorToast('容量不足のため中断'); return; }
      } else if(!(e && e.name==='AbortError')){
        console.warn('downloadWork failed', e);
        if(window.showErrorToast) window.showErrorToast('DL失敗');
      }
    } finally {
      downloadControllers.delete(folder);
      _downloadingSet.delete(folder);
      _stoppingSet.delete(folder);
      _listingSet.delete(folder);
      try{ if(window.refreshOfflineMap) await window.refreshOfflineMap(); if(window.renderView) window.renderView(); } catch(e) { console.warn(e); }
    }
  }
  async function deleteWork(folder, deps){
    var _d = _resolveDeps(deps);
    var _caches = _d.caches || (typeof caches !== 'undefined' ? caches : undefined);
    var _Store = _d.OfflineStore || window.OfflineStore;
    if(_deletingSet.has(folder)) return;
    _deletingSet.add(folder);
    window._deletingSet = _deletingSet;
    try{ if(window.renderView) window.renderView(); } catch(e) { console.warn(e); }
    _updateProgress(folder, 0);
    var leafPaths = getLeafPathsFor(folder, deps);
    var targets = [...new Set([...leafPaths, folder])];
    var totalLeaves = targets.length || 1;
    var deleteError = null;
    try{
      for(var leafIdx=0; leafIdx<targets.length; leafIdx++){
        var t = targets[leafIdx];
        try{
          var rec=await _Store.get(t);
          var cn=rec ? rec.cacheName : _Store.cacheNameFor(t);
          await _caches.delete(cn);
          await _Store.delete(t);
        }catch(e){
          deleteError = e;
          break;
        }
        _updateProgress(folder, (leafIdx+1)/totalLeaves);
        if(t!==folder) _updateProgress(t, (leafIdx+1)/totalLeaves);
        await new Promise(function(r){setTimeout(r, 90);});
      }
      if(window.updateStorageBar) await window.updateStorageBar(_d);
      if(window.refreshOfflineMap) await window.refreshOfflineMap();
    } finally {
      _deletingSet.delete(folder);
      try{ if(window.refreshOfflineMap) await window.refreshOfflineMap(); } catch(e) { console.warn(e); }
      try{ if(window.renderView) window.renderView(); } catch(e) { console.warn(e); }
      _updateProgress(folder, 0);
      if(deleteError) {
        console.warn('deleteWork failed', deleteError);
        if(window.showErrorToast) window.showErrorToast('削除に失敗しました');
      } else {
        if(window.showToastSuccess) window.showToastSuccess('削除しました');
      }
    }
  }
  window.downloadWork = downloadWork;
  window.deleteWork = deleteWork;

  function offlineControlsHtmlCtx(path, ctx){
    var state = getOfflineBtnStateCtx(path, ctx);
    var labels = {};
    labels[OfflineBtnState.IDLE] = "ダウンロード";
    labels[OfflineBtnState.DOWNLOADING] = "ダウンロード中";
    labels[OfflineBtnState.STOPPING] = "停止";
    labels[OfflineBtnState.COMPLETE] = "完了";
    labels[OfflineBtnState.DELETING] = "削除中";
    labels[OfflineBtnState.PARTIAL] = "一部ダウンロード済み";
    labels[OfflineBtnState.CONFIRM_DELETE] = "確認:削除";
    var label = labels[state] || labels[OfflineBtnState.IDLE];
    var prog = getPartialProgressCtx(path, ctx);
    var pct = prog ? prog.pct : 0;
    var deg = Math.round((pct||0)*360);
    var listingCls = _listingSet.has(path) ? ' is-listing' : '';
    return '<button class="offline-btn state-' + state + listingCls + '" data-folder="' + _escapeHtml(path) + '" aria-label="' + _escapeHtml(label) + '" title="' + _escapeHtml(label) + '" style="--progress:' + deg + 'deg"><span class="offline-icon"></span><span class="progress-ring"></span></button>';
  }
  function offlineControlsHtml(path){
    return offlineControlsHtmlCtx(path, null);
  }
  window.offlineControlsHtml = offlineControlsHtml;
  window.offlineControlsHtmlCtx = offlineControlsHtmlCtx;

  // ── anchored labeled popover for partially-downloaded ──
  function closeAllPartialPops(){
    document.querySelectorAll('.ux-pop').forEach(function(p){p.remove();});
  }
  function findOfflineBtn(folder){
    for(var b of document.querySelectorAll('.offline-btn')){
      if(b.dataset.folder===folder) return b;
    }
    return null;
  }
  function focusOfflineBtn(folder){
    var b=findOfflineBtn(folder);
    if(b) try{ b.focus({preventScroll:true}); } catch(e) { console.warn(e); }
  }
  function openPartialPopover(folder){
    var wasOpen = [...document.querySelectorAll('.ux-pop')].some(function(p){return p.dataset.folder===folder;});
    closeAllPartialPops();
    if(wasOpen){ focusOfflineBtn(folder); return; }
    var btn = findOfflineBtn(folder);
    if(!btn) return;
    var host = btn.closest('.tile,.card,.item');
    if(!host) return;
    var prog = getPartialProgress(folder) || {cachedCount:0, pageCount:0};
    var remain = Math.max((prog.pageCount||0)-(prog.cachedCount||0), 0);
    var pop = document.createElement('div');
    pop.className='ux-pop';
    pop.dataset.folder=folder;
    pop.setAttribute('role','group');
    pop.setAttribute('aria-label','オフライン保存の選択肢');
    pop.innerHTML='<button class="resume"><span class="ic">\u2193</span><span>続きをダウンロード<span class="sub">残り '+remain+' ページ</span></span></button>'
      +'<button class="del"><span class="ic">\uD83D\uDDD1</span><span>削除<span class="sub">'+(prog.cachedCount||0)+' ページ分の保存データ</span></span></button>';
    host.appendChild(pop);
    var h=pop.offsetHeight, br=btn.getBoundingClientRect();
    if(br.bottom+8+h>window.innerHeight){ pop.style.top=(btn.offsetTop-h-6)+'px'; }
    pop.querySelector('.resume').addEventListener('click', function(ev){
      ev.stopPropagation();
      closeAllPartialPops();
      downloadWork(folder);
      focusOfflineBtn(folder);
    });
    pop.querySelector('.del').addEventListener('click', function(ev){
      ev.stopPropagation();
      closeAllPartialPops();
      deleteWork(folder);
      focusOfflineBtn(folder);
    });
    var first=pop.querySelector('button');
    if(first) try{ first.focus({preventScroll:true}); } catch(e) { console.warn(e); }
  }
  window.closeAllPartialPops = closeAllPartialPops;
  window.openPartialPopover = openPartialPopover;

  // ── delegated listeners (one per page, survive innerHTML) ──
  // Need tileGrid — if not yet in DOM (this script loads before DOMContentLoaded),
  // wait for it.
  function _attachOfflineListeners(){
    var tileGrid = document.getElementById("tile-grid");
    if(!tileGrid) {
      document.addEventListener('DOMContentLoaded', _attachOfflineListeners, {once:true});
      return;
    }
    // Avoid double-attach if app.js also attaches
    if(tileGrid.__offlineListenerAttached) return;
    tileGrid.__offlineListenerAttached = true;
    tileGrid.addEventListener('click', function(e){
      var btn = e.target.closest && e.target.closest('.offline-btn');
      if(!btn) return;
      e.stopPropagation();
      var folder = btn.dataset.folder;
      if(!folder) return;
      if(_confirmMap.has(folder)){
        var kind = _confirmMap.get(folder);
        _confirmMap.delete(folder);
        try{ if(window.renderView) window.renderView(); } catch(e) { console.warn(e); }
        if(kind === 'delete'){ deleteWork(folder); }
        return;
      }
      var state = getOfflineBtnState(folder);
      if(state === OfflineBtnState.IDLE){ downloadWork(folder); return; }
      else if(state === OfflineBtnState.PARTIAL){ openPartialPopover(folder); return; }
      else if(state === OfflineBtnState.COMPLETE){ _confirmMap.set(folder,'delete'); try{ if(window.renderView) window.renderView(); } catch(e) { console.warn(e); } return; }
      else if(state === OfflineBtnState.DOWNLOADING){
        var ctrl = downloadControllers.get(folder);
        if(ctrl) try{ ctrl.abort(); } catch(e) { console.warn(e); }
        _downloadingSet.delete(folder);
        _stoppingSet.add(folder);
        try{ if(window.renderView) window.renderView(); } catch(e) { console.warn(e); }
        return;
      } else if(state === OfflineBtnState.STOPPING){ return; }
      else if(state === OfflineBtnState.DELETING){ return; }
      else { if(isFolderOffline(folder)) deleteWork(folder); else downloadWork(folder); }
    });
  }
  // Global dismiss for popover/confirm — single owner (app.js must not
  // duplicate these listeners; the __offlineGlobalDismissAttached flag guards
  // against double-attach regardless of which script runs first).
  if(!window.__offlineGlobalDismissAttached){
    window.__offlineGlobalDismissAttached = true;
    document.addEventListener('click', function(e){
      if(e.target.closest && (e.target.closest('.offline-btn') || e.target.closest('.ux-pop'))) return;
      var changed=false;
      if(document.querySelector('.ux-pop')){ closeAllPartialPops(); changed=true; }
      if(_confirmMap.size>0){
        for(var f of [..._confirmMap.keys()]){
          if(_deletingSet.has(f)) continue;
          _confirmMap.delete(f);
          changed=true;
        }
      }
      if(changed){ try{ if(window.renderView) window.renderView(); } catch(e) { console.warn(e); } }
    });
    document.addEventListener('keydown', function(e){
      if(e.key !== 'Escape') return;
      var changed=false;
      var popped=false;
      var popFolders=[...document.querySelectorAll('.ux-pop')].map(function(p){return p.dataset.folder;});
      if(popFolders.length){ closeAllPartialPops(); popped=true; changed=true; }
      if(_confirmMap.size>0){
        for(var f of [..._confirmMap.keys()]){
          if(_deletingSet.has(f)) continue;
          _confirmMap.delete(f);
          changed=true;
        }
      }
      if(changed){
        try{ if(window.renderView) window.renderView(); } catch(e) { console.warn(e); }
        popFolders.forEach(focusOfflineBtn);
        if(popped) e.stopImmediatePropagation();
      }
    });
  }

  window.StoreAdapter = {
    downloadWork, deleteWork, getOfflineBtnState, getPartialProgress,
    isFolderOffline, getLeafPathsFor, invalidateLeafPaths, offlineControlsHtml,
    getOfflineBtnStateCtx, getPartialProgressCtx, isFolderOfflineCtx,
    offlineControlsHtmlCtx,
    closeAllPartialPops, openPartialPopover, showQuotaConfirm, OfflineBtnState
  };
})();
