/* bookq browse/visibility — per-user hidden top-level roots.
 * Plain global script: defines window.FolderVisibility.
 * The hidden set lives on the server keyed by the authenticated user; this
 * module holds the last-synced snapshot (memory + one owner-scoped
 * localStorage entry for offline boot), the transient normal/full-display
 * mode (never persisted), and the shared O(1) visibility predicates the
 * renderers and navigation guards consume.
 * Loads after offline-core.js (escapeHtml), before hash/render/facets/app.js.
 */
(function () {
  var API_VERSION = 1;
  var URL = "/api/folder-visibility";
  var OWNER_HEADER = "X-Bookq-Visibility-Owner";
  var GET_TIMEOUT_MS = 3000;
  var CACHE_PREFIX = "bookq.folderVisibility.v1.";
  var COALESCE_MS = 400;

  // ── state ──
  var _inited = false;
  var _owner = null;
  var _hidden = new Set();
  var _hasSnapshot = false;    // true once any snapshot (cache or server) applied
  var _revision = 0;
  var _appliedSig = "";
  var _showAll = false;
  var _status = "idle";        // idle | loading | ready | readonly | error
  var _readOnly = false;
  var _offline = false;
  var _saving = false;
  var _lastError = null;       // null | "fetch" | "auth" | "save" | "owner"
  var _onChange = null;
  var _fetchGen = 0;
  var _refreshFlight = null;
  var _refreshQueued = false;
  var _coalesceTimer = 0;
  var _readyPromise = null;
  var _readyResolve = null;

  function _esc(s){ return window.OfflineCore.escapeHtml(s); }

  function _sig(revision, roots){
    return revision + "|" + roots.slice().sort().join("");
  }

  // ── owner-scoped snapshot cache (offline boot fallback only; the showAll
  // mode is NEVER persisted — not here, not in the URL, not to the server) ──
  function _readCache(owner){
    try {
      var raw = localStorage.getItem(CACHE_PREFIX + owner);
      if (!raw) return null;
      var o = JSON.parse(raw);
      if (!o || o.v !== 1 || o.owner !== owner) return null;
      if (typeof o.revision !== "number" || !Array.isArray(o.hiddenRoots)) return null;
      return { owner: o.owner, revision: o.revision, hiddenRoots: o.hiddenRoots };
    } catch(e){ return null; }
  }
  function _writeCache(){
    try {
      localStorage.setItem(CACHE_PREFIX + _owner, JSON.stringify({
        v: 1, owner: _owner, revision: _revision, hiddenRoots: Array.from(_hidden)
      }));
    } catch(e){
      // localStorage failure never rolls back server state; the in-memory
      // snapshot keeps working for this document.
    }
  }

  function _fireChange(){
    if (_onChange){ try { _onChange(); } catch(e){ console.warn(e); } }
  }

  // Adopt a snapshot; returns whether the visible state actually changed.
  // Owner change adopts the server owner as authoritative: mode resets to
  // normal and the old owner's revision/compare state is discarded so a
  // stale response or a stale cache can never leak into the new account.
  function _applySnapshot(owner, revision, roots, opts){
    opts = opts || {};
    if (owner !== _owner){
      _owner = owner;
      _revision = 0;
      _appliedSig = "";
      _showAll = false;
    }
    if (revision < _revision) return false; // monotonic per owner
    var list = (roots || []).filter(function(r){ return typeof r === "string"; });
    var sig = _sig(revision, list);
    var changed = sig !== _appliedSig;
    _revision = revision;
    _hidden = new Set(list);
    _hasSnapshot = true;
    _appliedSig = sig;
    if (opts.cache !== false) _writeCache();
    if (changed || opts.force) _fireChange();
    return changed;
  }

  // ── network ──
  function _get(){
    var gen = ++_fetchGen;
    var ctl = (typeof AbortController === "function") ? new AbortController() : null;
    var timer = ctl ? setTimeout(function(){ try { ctl.abort(); } catch(e){} }, GET_TIMEOUT_MS) : null;
    var opts = { credentials: "same-origin", cache: "no-store" };
    if (ctl) opts.signal = ctl.signal;
    return fetch(URL, opts).then(function(res){
      if (timer) clearTimeout(timer);
      if (gen !== _fetchGen) return null;      // superseded — let the newer call resolve
      if (res.status === 401 || res.status === 403) return { authFail: true };
      if (!res.ok) return { httpErr: res.status };
      return res.json().then(function(j){ return { json: j }; });
    }).catch(function(){
      if (timer) clearTimeout(timer);
      return { netErr: true };
    });
  }

  function _resolveGet(out, cached){
    if (out === null) return;                 // superseded
    if (out.json){
      var j = out.json;
      var srvOwner = (typeof j.owner === "string" && j.owner) ? j.owner : _owner;
      // Commit the status BEFORE _applySnapshot: its onChange listeners read
      // readyState — firing while still "loading" would be a wasted no-op.
      _status = "ready"; _readOnly = false; _lastError = null;
      _applySnapshot(srvOwner,
        (typeof j.revision === "number") ? j.revision : 0,
        Array.isArray(j.hiddenRoots) ? j.hiddenRoots : [],
        { force: srvOwner !== _owner });
    } else if (out.authFail){
      // Never continue on another account's snapshot.
      _status = "error"; _lastError = "auth";
    } else if (cached){
      // Network/5xx failure with a same-owner snapshot: read-only fallback.
      _status = "readonly"; _readOnly = true; _lastError = null;
      _applySnapshot(cached.owner, cached.revision, cached.hiddenRoots, { cache: false });
    } else {
      // No snapshot at all: error + retry — an empty hidden set must NOT be
      // assumed (that would expose everything).
      _status = "error"; _lastError = "fetch";
    }
    _offline = (typeof navigator !== "undefined" && navigator.onLine === false);
    _updateStatusDom();
    if (_readyResolve){ var r = _readyResolve; _readyResolve = null; r(); }
  }

  function refresh(){
    if (_refreshFlight){ _refreshQueued = true; return _refreshFlight; }
    var cached = _owner ? _readCache(_owner) : null;
    _refreshFlight = _get().then(function(out){
      _refreshFlight = null;
      _resolveGet(out, cached);
      if (_refreshQueued){ _refreshQueued = false; _scheduleRefresh(); }
    });
    return _refreshFlight;
  }

  function _scheduleRefresh(){
    if (_coalesceTimer) return;
    _coalesceTimer = setTimeout(function(){
      _coalesceTimer = 0;
      refresh();
    }, COALESCE_MS);
  }

  // ── predicates ──
  // First top-level segment after normalizing "", "." and ".." so
  // "visible/../hidden" cannot evade the hidden set. A path that escapes the
  // library root (leading "..") yields null — never a valid root.
  function rootForPath(path){
    if (typeof path !== "string" || !path) return null;
    var segs = path.split("/");
    var out = [];
    for (var i = 0; i < segs.length; i++){
      var s = segs[i];
      if (s === "" || s === ".") continue;
      if (s === ".."){ if (!out.length) return null; out.pop(); continue; }
      out.push(s);
    }
    return out.length ? out[0] : null;
  }
  function isHidden(path){
    var root = rootForPath(path);
    return !!(root && _hidden.has(root));
  }
  function isVisible(path){
    if (_showAll) return true;
    if (!_hasSnapshot) return false;   // fail closed — never assume "all visible"
    return !isHidden(path);
  }

  // ── committed-work predicate (2台同期) ──
  // Resolve the committed work's folder in O(1): manifest.folder first, else
  // the committed spread's anchor PageRef (snap.spreadId → spreadTable entry
  // → right/left .folder). true only when a folder resolved AND is not
  // currently visible — a manifest that yields no folder (legacy/test
  // snapshots) never blocks.
  function committedFolderHidden(manifest, snap){
    if (_showAll) return false;
    var folder = null;
    if (manifest && typeof manifest.folder === "string" && manifest.folder){
      folder = manifest.folder;
    } else if (manifest && snap && snap.spreadId){
      var table = manifest.spreadTable || manifest.spreads;
      if (Array.isArray(table)){
        for (var i = 0; i < table.length; i++){
          var s = table[i];
          if (s && s.id === snap.spreadId){
            var ref = s.right || s.left || null;
            if (ref && typeof ref.folder === "string" && ref.folder) folder = ref.folder;
            break;
          }
        }
      }
    }
    return !!(folder && !isVisible(folder));
  }

  // ── shared presentation for every renderer ──
  function _eyeSvg(hidden){
    var slash = hidden ? '<line x1="3" y1="3" x2="21" y2="21"/>' : "";
    return '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/>' + slash + "</svg>";
  }
  // path = node _fullPath; opts.editable = real top-level folder/archive tile.
  // Hidden roots get 「非表示」; items under a hidden root get 「上位フォルダが非表示」.
  function presentation(path, opts){
    opts = opts || {};
    var root = rootForPath(path);
    var hidden = !!(root && _hidden.has(root));
    var descendant = !!(hidden && path && root && path !== root);
    var cls = hidden ? (descendant ? "is-hidden-descendant" : "is-hidden") : "";
    var badge = hidden
      ? '<span class="visibility-badge">' + (descendant ? "上位フォルダが非表示" : "非表示") + "</span>"
      : "";
    var btn = "";
    if (opts.editable && root){
      var label = hidden ? "再表示する" : "非表示にする";
      btn = '<button type="button" class="visibility-toggle" data-root="' + _esc(root) +
        '" aria-pressed="' + (hidden ? "true" : "false") +
        '" aria-label="' + label + '" title="' + label + '">' + _eyeSvg(hidden) + "</button>";
    }
    return { className: cls, badgeHtml: badge, buttonHtml: btn };
  }

  // ── write path: one in-flight PUT, server snapshot commits verbatim ──
  function _setGridBusy(on){
    var g = document.getElementById("tile-grid");
    if (g) g.classList.toggle("vis-busy", !!on);
  }
  function setHidden(root, hidden){
    if (typeof root !== "string" || !root || root.indexOf("/") !== -1 || root === "." || root === "..")
      return Promise.resolve({ ok: false, invalid: true });
    if (_saving) return Promise.resolve({ ok: false, busy: true });
    if (_readOnly || _offline || (typeof navigator !== "undefined" && navigator.onLine === false)){
      _updateStatusDom();
      return Promise.resolve({ ok: false, offline: true });
    }
    _saving = true; _setGridBusy(true); _updateStatusDom();
    var gen = ++_fetchGen;
    return fetch(URL, {
      method: "PUT", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json", [OWNER_HEADER]: _owner },
      body: JSON.stringify({ root: root, hidden: !!hidden })
    }).then(function(res){
      _saving = false; _setGridBusy(false);
      if (gen !== _fetchGen) return { ok: false, stale: true };
      if (res.status === 409){
        // The owner the page booted under is stale — pull the server's
        // authoritative owner/snapshot instead of writing to another user.
        _status = "error"; _lastError = "owner"; _updateStatusDom();
        refresh();
        return { ok: false, status: 409 };
      }
      if (res.status === 401 || res.status === 403){
        _status = "error"; _lastError = "auth"; _updateStatusDom();
        return { ok: false, status: res.status };
      }
      if (!res.ok){
        _lastError = "save"; _updateStatusDom();
        return { ok: false, status: res.status };
      }
      return res.json().then(function(j){
        var srvOwner = (typeof j.owner === "string" && j.owner) ? j.owner : _owner;
        // Same ordering as _resolveGet: commit status before notifying.
        _status = "ready"; _readOnly = false; _lastError = null;
        _applySnapshot(srvOwner,
          (typeof j.revision === "number") ? j.revision : 0,
          Array.isArray(j.hiddenRoots) ? j.hiddenRoots : [],
          { force: srvOwner !== _owner });
        _updateStatusDom();
        return { ok: true };
      });
    }).catch(function(){
      _saving = false; _setGridBusy(false);
      _lastError = "save"; _updateStatusDom();
      return { ok: false, net: true };
    });
  }

  // ── mode: memory only, never persisted ──
  function setShowAll(v){
    v = !!v;
    if (_showAll === v) return;
    _showAll = v;
    _updateStatusDom();
    _fireChange();
    _scheduleRefresh();   // mode switch re-syncs against the server
  }

  // ── status row ──
  function _updateStatusDom(){
    var n = document.getElementById("visibility-normal");
    var a = document.getElementById("visibility-all");
    if (n){ n.setAttribute("aria-pressed", _showAll ? "false" : "true"); n.classList.toggle("active", !_showAll); }
    if (a){ a.setAttribute("aria-pressed", _showAll ? "true" : "false"); a.classList.toggle("active", _showAll); }
    var el = document.getElementById("visibility-status");
    if (!el) return;
    var txt = "";
    var retry = false;
    if (_saving) txt = "保存中…";
    else if (_status === "loading") txt = "読込中…";
    else if (_offline || _readOnly) txt = "オフライン（変更不可）";
    else if (_status === "error"){ txt = "取得失敗"; retry = true; }
    else if (_lastError === "save") txt = "保存失敗";
    el.innerHTML = _esc(txt) + (retry ? ' <button type="button" id="visibility-retry">再試行</button>' : "");
    var rb = document.getElementById("visibility-retry");
    if (rb) rb.addEventListener("click", function(e){
      e.preventDefault(); e.stopPropagation();
      _status = "loading"; _lastError = null; _updateStatusDom();
      refresh();
    });
  }

  // One delegated listener for every .visibility-toggle in the grid —
  // survives innerHTML replacement, never lets the tile-open handler see it.
  function _bindDelegatedToggle(){
    var grid = document.getElementById("tile-grid");
    if (!grid || grid.__fvDelegated) return;
    grid.__fvDelegated = true;
    grid.addEventListener("click", function(e){
      var btn = e.target && e.target.closest ? e.target.closest(".visibility-toggle") : null;
      if (!btn || !grid.contains(btn)) return;
      e.preventDefault();
      e.stopPropagation();
      if (e.stopImmediatePropagation) e.stopImmediatePropagation();
      if (_saving) return;
      var root = btn.getAttribute("data-root");
      if (!root) return;
      setHidden(root, !_hidden.has(root));
    });
  }

  function _bindRefreshTriggers(){
    if (typeof document !== "undefined"){
      document.addEventListener("visibilitychange", function(){
        if (document.visibilityState === "visible") _scheduleRefresh();
      });
    }
    if (typeof window !== "undefined"){
      window.addEventListener("focus", _scheduleRefresh);
      window.addEventListener("pageshow", function(e){ if (e && e.persisted) _scheduleRefresh(); });
      window.addEventListener("online", function(){ _offline = false; _updateStatusDom(); _scheduleRefresh(); });
      window.addEventListener("offline", function(){ _offline = true; _updateStatusDom(); });
    }
  }

  function _bindModeButtons(){
    var n = document.getElementById("visibility-normal");
    var a = document.getElementById("visibility-all");
    if (n) n.addEventListener("click", function(e){ e.preventDefault(); e.stopPropagation(); setShowAll(false); });
    if (a) a.addEventListener("click", function(e){ e.preventDefault(); e.stopPropagation(); setShowAll(true); });
  }

  function init(opts){
    if (_inited) return;
    _inited = true;
    opts = opts || {};
    _owner = (typeof opts.owner === "string" && opts.owner) ? opts.owner : null;
    _onChange = (typeof opts.onChange === "function") ? opts.onChange : null;
    _readyPromise = new Promise(function(res){ _readyResolve = res; });
    _offline = (typeof navigator !== "undefined" && navigator.onLine === false);
    _status = "loading"; _updateStatusDom();
    // Same-owner cached snapshot applies immediately (marks read-only intent
    // until the GET resolves); a mismatched/corrupt cache is ignored entirely.
    var cached = _owner ? _readCache(_owner) : null;
    if (cached) _applySnapshot(cached.owner, cached.revision, cached.hiddenRoots, { cache: false });
    _bindDelegatedToggle();
    _bindRefreshTriggers();
    _bindModeButtons();
    refresh();
  }

  function ready(){ return _readyPromise || Promise.resolve(); }

  function snapshot(){
    return {
      owner: _owner, revision: _revision,
      hiddenRoots: Array.from(_hidden),
      status: _status, readOnly: _readOnly, showAll: _showAll,
    };
  }
  function _debug(){
    return {
      status: _status, owner: _owner, revision: _revision,
      hiddenCount: _hidden.size, showAll: _showAll,
      offline: _offline, saving: _saving, readOnly: _readOnly,
      lastError: _lastError, hasSnapshot: _hasSnapshot,
      refreshInFlight: !!_refreshFlight, fetchGen: _fetchGen,
    };
  }

  window.FolderVisibility = {
    apiVersion: API_VERSION,
    init: init,
    ready: ready,
    refresh: refresh,
    rootForPath: rootForPath,
    isHidden: isHidden,
    isVisible: isVisible,
    get showAll(){ return _showAll; },
    setShowAll: setShowAll,
    get readyState(){ return _status; },
    get saving(){ return _saving; },
    // True once a snapshot (owner-scoped cache or server) has been applied —
    // predicates are meaningful while a fresh GET is still in flight.
    get hasSnapshot(){ return _hasSnapshot; },
    committedFolderHidden: committedFolderHidden,
    presentation: presentation,
    setHidden: setHidden,
    snapshot: snapshot,
    _debug: _debug,
  };
})();
