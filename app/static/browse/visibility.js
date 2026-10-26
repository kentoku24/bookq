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
  var _hidden = new Set();     // hidden ROOT keys — top-level node _fullPath (flattened names like "wrap/work" contain "/")
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
  var _reqGen = 0;               // bumped per issued request (GET or PUT)
  var _appliedGen = 0;           // issueGen of the newest APPLIED response — an older request's response never lands over it
  var _convergenceSent = false;  // down-revision adoption: at most one auto convergence GET per adoption
  var _retrying = false;         // error 再試行の進行フラグ — status は error のまま（loading へ戻すと drain が再入する）
  var _topLevel = new Set();     // root-level node _fullPath set — app.js republishes on every (re)index
  var _refreshFlight = null;
  var _refreshQueued = false;
  var _coalesceTimer = 0;
  var _readyPromise = null;
  var _readyResolve = null;

  function _esc(s){ return window.OfflineCore.escapeHtml(s); }

  function _sig(revision, roots){
    // NUL-joined: roots may contain "/" and "," — a plain join collides
    // (["a,b","c"] vs ["a","b,c"] produce the same string).
    return revision + "|" + roots.slice().sort().join("\u0000");
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
  // allowDownrev is for server responses only: a GET/PUT snapshot that went
  // backwards (server reset or a stale pre-PUT read) is still the server's
  // truth — adopt it verbatim and let the caller converge once.
  function _applySnapshot(owner, revision, roots, opts){
    opts = opts || {};
    if (owner !== _owner){
      _owner = owner;
      _revision = 0;
      _appliedSig = "";
      _showAll = false;
    }
    if (revision < _revision && !opts.allowDownrev) return false; // monotonic per owner
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
  // Every terminal branch of a request resolves ready() — the first
  // completed judgment may come from ANY source, including a PUT landing
  // before the initial GET.
  function _resolveReady(){
    if (_readyResolve){ var r = _readyResolve; _readyResolve = null; r(); }
  }
  // A GET response whose issueGen lost to a newer applied response never
  // lands; one that lands with srvRev < _revision is still adopted (server
  // is truth) but schedules exactly ONE convergence GET — a stale pre-PUT
  // read self-corrects, a real reset stays put.
  function _convergeOnce(){
    if (_convergenceSent) return;
    _convergenceSent = true;
    _scheduleRefresh();
  }

  function _get(){
    var ctl = (typeof AbortController === "function") ? new AbortController() : null;
    var timer = ctl ? setTimeout(function(){ try { ctl.abort(); } catch(e){} }, GET_TIMEOUT_MS) : null;
    var opts = { credentials: "same-origin", cache: "no-store" };
    if (ctl) opts.signal = ctl.signal;
    return fetch(URL, opts).then(function(res){
      if (timer) clearTimeout(timer);
      if (res.status === 401 || res.status === 403) return { authFail: true };
      if (!res.ok) return { httpErr: res.status };
      return res.json().then(function(j){ return { json: j }; });
    }).catch(function(){
      if (timer) clearTimeout(timer);
      return { netErr: true };
    });
  }

  function _resolveGet(out, cached, issueGen){
    if (out === null) return;                 // defensive — nothing resolves null
    var prevStatus = _status;
    if (out.json){
      if (issueGen >= _appliedGen){
        var j = out.json;
        var srvOwner = (typeof j.owner === "string" && j.owner) ? j.owner : _owner;
        var srvRev = (typeof j.revision === "number") ? j.revision : 0;
        var roots = Array.isArray(j.hiddenRoots) ? j.hiddenRoots : [];
        var downrev = (srvOwner === _owner) && (srvRev < _revision);
        _appliedGen = issueGen;
        // Commit the status BEFORE _applySnapshot: its onChange listeners read
        // readyState — firing while still "loading" would be a wasted no-op.
        _status = "ready"; _readOnly = false; _lastError = null;
        _applySnapshot(srvOwner, srvRev, roots,
          { force: srvOwner !== _owner, allowDownrev: true });
        if (downrev) _convergeOnce(); else _convergenceSent = false;
      }
      // else: a newer request's response already applied — this older GET
      // never lands over it (a PUT issued later still wins).
    } else if (out.authFail){
      // Never continue on another account's snapshot.
      _status = "error"; _lastError = "auth";
    } else if (cached){
      // Network/5xx failure with a same-owner snapshot: read-only fallback.
      _status = "readonly"; _readOnly = true; _lastError = null;
      _applySnapshot(cached.owner, cached.revision, cached.hiddenRoots, { cache: false });
    } else {
      // No snapshot at all: error + retry — an empty hidden set must NOT be
      // assumed (that would expose everything), and error is NOT denied-by-
      // default either — guards queue, nothing is treated as hidden.
      _status = "error"; _lastError = "fetch";
    }
    _retrying = false;
    _offline = (typeof navigator !== "undefined" && navigator.onLine === false);
    _updateStatusDom();
    // Status edges (error→ready, readonly→ready …) re-render even when the
    // snapshot itself was identical, so drains and error UIs converge.
    if (_status !== prevStatus) _fireChange();
    _resolveReady();
  }

  function refresh(){
    if (_refreshFlight){ _refreshQueued = true; return _refreshFlight; }
    var cached = _owner ? _readCache(_owner) : null;
    var issueGen = ++_reqGen;
    _refreshFlight = _get().then(function(out){
      _refreshFlight = null;
      _resolveGet(out, cached, issueGen);
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
  // Hidden keys are the _fullPath of top-level nodes — a flattened scanner
  // root's name is multi-segment ("wrap/work"), so key matching is a full
  // prefix match, not a first-segment compare. Longest matching key wins.
  // "", "." and ".." segments are normalized away so "visible/../hidden"
  // cannot evade the hidden set; a path escaping the library root yields
  // null — never a valid root.
  function _normalizePath(path){
    if (typeof path !== "string" || !path) return null;
    var segs = path.split("/");
    var out = [];
    for (var i = 0; i < segs.length; i++){
      var s = segs[i];
      if (s === "" || s === ".") continue;
      if (s === ".."){ if (!out.length) return null; out.pop(); continue; }
      out.push(s);
    }
    return out.join("/");
  }
  // First segment of the normalized path — compatibility helper for
  // callers that only need the root segment (tests, legacy callers).
  function rootForPath(path){
    var norm = _normalizePath(path);
    return (norm && norm.length) ? norm.split("/")[0] : null;
  }
  // The hidden key that covers path (path === K or path under K + "/"), or
  // null. Longest key wins so a hidden flattened root beats a hidden
  // ancestor prefix.
  function hiddenRootFor(path){
    var norm = _normalizePath(path);
    if (norm === null || norm === "") return null;
    var best = null;
    _hidden.forEach(function(k){
      if (typeof k === "string" && k &&
          (norm === k || norm.indexOf(k + "/") === 0) &&
          (best === null || k.length > best.length)){
        best = k;
      }
    });
    return best;
  }
  // Positive-hidden only: with no snapshot applied the hidden set is empty,
  // so error/unknown state can never masquerade as "hidden".
  function isHidden(path){ return hiddenRootFor(path) !== null; }
  function isVisible(path){
    if (_showAll) return true;
    if (!_hasSnapshot) return false;   // fail closed — never assume "all visible"
    return !isHidden(path);
  }

  // ── top-level node set (editable roots) ──
  function setTopLevelPaths(paths){
    _topLevel = new Set();
    for (var i = 0; i < (paths || []).length; i++){
      var p = paths[i];
      if (typeof p === "string" && p) _topLevel.add(p);
    }
  }
  function isTopLevel(path){ return _topLevel.has(path); }

  // Shared deny/queue gate for every navigation entry: "pending" while the
  // judgment is unknown (loading, or error/idle without any snapshot),
  // "hidden" only on a positive hidden match in normal mode, else "allow".
  function guardPath(path){
    if (_showAll) return "allow";
    if (_status === "loading" || !_hasSnapshot) return "pending";
    return (path && hiddenRootFor(path) !== null) ? "hidden" : "allow";
  }
  // Grid-wide placeholder while NO snapshot exists at all — renderers must
  // never paint an unfiltered list in loading/error. With any snapshot
  // (cached or live) the grid renders normally off its data.
  function blockedGridHtml(){
    if (_hasSnapshot) return null;
    if (_status === "idle" || _status === "loading")
      return '<div style="color:#555;text-align:center;padding:20px;font-size:13px">読込中…</div>';
    if (_status === "error")
      return '<div style="color:#c44;text-align:center;padding:20px;font-size:13px">取得失敗 <button type="button" class="fv-grid-retry">再試行</button></div>';
    return null;
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
    return !!(folder && isHidden(folder));
  }

  // ── shared presentation for every renderer ──
  function _eyeSvg(hidden){
    var slash = hidden ? '<line x1="3" y1="3" x2="21" y2="21"/>' : "";
    return '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/>' + slash + "</svg>";
  }
  // path = node _fullPath; opts.editable = real top-level folder/archive
  // tile (omit → auto via isTopLevel). The hidden key K covering path wins
  // the badge: 「非表示」 on the root itself, 「上位フォルダが非表示」 on
  // everything under it (K !== path).
  function presentation(path, opts){
    opts = opts || {};
    var key = hiddenRootFor(path);
    var hidden = key !== null;
    var descendant = hidden && key !== path;
    var cls = hidden ? (descendant ? "is-hidden-descendant" : "is-hidden") : "";
    var badge = hidden
      ? '<span class="visibility-badge">' + (descendant ? "上位フォルダが非表示" : "非表示") + "</span>"
      : "";
    var editable = (opts.editable !== undefined) ? !!opts.editable : isTopLevel(path);
    var btn = "";
    if (editable && path){
      var label = hidden ? "再表示する" : "非表示にする";
      btn = '<button type="button" class="visibility-toggle" data-root="' + _esc(path) +
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
  // Client-side copy of the backend root rule: normalized path, no empty /
  // "." / ".." segments, no NUL or backslash, no dot-leading segment.
  // Multi-segment roots (flattened names) are valid — membership in the
  // current top-level set is the server's check, not the shape's.
  function _validRootPath(root){
    if (typeof root !== "string" || !root || root.length > 2048) return null;
    if (root.indexOf("\\") !== -1 || root.indexOf("\u0000") !== -1) return null;
    var segs = root.split("/");
    for (var i = 0; i < segs.length; i++){
      var s = segs[i];
      if (!s || s === "." || s === ".." || s.charAt(0) === ".") return null;
    }
    return root;
  }
  function setHidden(root, hidden){
    if (_validRootPath(root) === null)
      return Promise.resolve({ ok: false, invalid: true });
    if (_saving) return Promise.resolve({ ok: false, busy: true });
    if (_readOnly || _offline || (typeof navigator !== "undefined" && navigator.onLine === false)){
      _updateStatusDom();
      return Promise.resolve({ ok: false, offline: true });
    }
    _saving = true; _setGridBusy(true); _updateStatusDom();
    var issueGen = ++_reqGen;
    return fetch(URL, {
      method: "PUT", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json", [OWNER_HEADER]: _owner },
      body: JSON.stringify({ root: root, hidden: !!hidden })
    }).then(function(res){
      _saving = false; _setGridBusy(false);
      if (res.status === 409){
        // The owner the page booted under is stale — pull the server's
        // authoritative owner/snapshot instead of writing to another user.
        _status = "error"; _lastError = "owner"; _updateStatusDom();
        refresh();
        _resolveReady();
        return { ok: false, status: 409 };
      }
      if (res.status === 401 || res.status === 403){
        _status = "error"; _lastError = "auth"; _updateStatusDom();
        _resolveReady();
        return { ok: false, status: res.status };
      }
      if (!res.ok){
        _lastError = "save"; _updateStatusDom();
        _resolveReady();
        return { ok: false, status: res.status };
      }
      return res.json().then(function(j){
        var srvOwner = (typeof j.owner === "string" && j.owner) ? j.owner : _owner;
        // The PUT response is the authoritative post-write snapshot — always
        // applied, even if a newer-issued GET already landed (the GET's read
        // predates the commit, so the write can only be fresher).
        _appliedGen = Math.max(_appliedGen, issueGen);
        _status = "ready"; _readOnly = false; _lastError = null;
        _applySnapshot(srvOwner,
          (typeof j.revision === "number") ? j.revision : 0,
          Array.isArray(j.hiddenRoots) ? j.hiddenRoots : [],
          { force: srvOwner !== _owner, allowDownrev: true });
        _convergenceSent = false;
        _updateStatusDom();
        _resolveReady();
        return { ok: true };
      });
    }).catch(function(){
      _saving = false; _setGridBusy(false);
      _lastError = "save"; _updateStatusDom();
      _resolveReady();
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
  // Retry keeps _status at "error" (NOT "loading"): a pending drain only
  // fires on the ready/readonly edge, and flipping to loading would make
  // guards queue while the previous error state still stands.
  function _startRetry(){
    _retrying = true; _lastError = null; _updateStatusDom();
    refresh();
  }
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
    else if (_status === "error"){ txt = _retrying ? "再試行中…" : "取得失敗"; retry = !_retrying; }
    else if (_lastError === "save") txt = "保存失敗";
    el.innerHTML = _esc(txt) + (retry ? ' <button type="button" id="visibility-retry">再試行</button>' : "");
    var rb = document.getElementById("visibility-retry");
    if (rb) rb.addEventListener("click", function(e){
      e.preventDefault(); e.stopPropagation();
      _startRetry();
    });
  }

  // One delegated listener for every .visibility-toggle in the grid —
  // survives innerHTML replacement, never lets the tile-open handler see it.
  function _bindDelegatedToggle(){
    var grid = document.getElementById("tile-grid");
    if (!grid || grid.__fvDelegated) return;
    grid.__fvDelegated = true;
    grid.addEventListener("click", function(e){
      var rt = e.target && e.target.closest ? e.target.closest(".fv-grid-retry") : null;
      if (rt && grid.contains(rt)){
        e.preventDefault(); e.stopPropagation();
        if (e.stopImmediatePropagation) e.stopImmediatePropagation();
        _startRetry();
        return;
      }
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
      refreshInFlight: !!_refreshFlight,
      reqGen: _reqGen, appliedGen: _appliedGen, fetchGen: _reqGen,
    };
  }

  window.FolderVisibility = {
    apiVersion: API_VERSION,
    init: init,
    ready: ready,
    refresh: refresh,
    rootForPath: rootForPath,
    hiddenRootFor: hiddenRootFor,
    isHidden: isHidden,
    isVisible: isVisible,
    isTopLevel: isTopLevel,
    setTopLevelPaths: setTopLevelPaths,
    guardPath: guardPath,
    blockedGridHtml: blockedGridHtml,
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
