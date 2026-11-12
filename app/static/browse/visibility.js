/* bookq browse/visibility — per-user hidden top-level roots.
 * Plain global script: defines window.FolderVisibility.
 * The hidden set lives on the server keyed by the authenticated user; this
 * module holds the last-synced snapshot (memory + one owner-scoped
 * localStorage entry for offline boot), the transient normal/full-display
 * mode (never persisted), and the shared O(1) visibility predicates the
 * renderers and navigation guards consume.
 * Loads after offline-core.js (escapeHtml), before hash/render/facets/app.js.
 *
 * Wire (#236): GET/PUT go through the typed window.BookqApi client
 * (app/static/api/v1.js) — endpoint paths and snapshot shapes are spec-typed,
 * so a response-shape change breaks tsc instead of only E2E.
 */
// @ts-check
(function () {
  var API_VERSION = 1;
  // NOTE (#236): transport goes through the typed window.BookqApi client
  // (getVisibility/putVisibility) — no load-time endpoint dereference here.
  // The pre-#236 URL var (guarded endpoints lookup with a hardcoded fallback)
  // was removed with the direct fetch calls: an unguarded
  // `window.BookqApi.endpoints...` throws when this file is evaluated
  // standalone (unit-test stub), and a hardcoded fallback string would
  // violate the no-literals-outside-api/ guard
  // (tests/test_pwa_api_client.py).
  var GET_TIMEOUT_MS = 3000;
  var CACHE_PREFIX = "bookq.folderVisibility.v1.";
  var COALESCE_MS = 400;

  // ── state ──
  var _inited = false;
  var _owner = null;
  var _hidden = new Set();     // hidden ROOT keys — top-level node _fullPath (flattened names like "wrap/work" contain "/")
  var _hiddenNorm = new Map(); // NFC+lowercase key → 元の hidden key（大小文字/合成差の直URLも同一視）
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
  var _readyResolve = null;
  // init 前から存在 — pair _boot 等の早期 ready() は最初の判定（GET/PUT の
  // どちらか先に着く方）まで必ず待つ。init() はこれを再生成しない。
  var _readyPromise = new Promise(function(res){ _readyResolve = res; });

  function _esc(s){ return window.OfflineCore.escapeHtml(s); }

  // tsc helper: EventTarget has no .closest in lib.dom — the guarded any
  // cast keeps the runtime shape (null when not an Element) without DOM lies.
  function _closest(target, selector){
    var t = /** @type {any} */ (target);
    return (t && t.closest) ? t.closest(selector) : null;
  }

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
    _hiddenNorm = new Map();
    for (var _hi = 0; _hi < list.length; _hi++){
      var _hk = _normKey(list[_hi]);
      if (!_hiddenNorm.has(_hk)) _hiddenNorm.set(_hk, list[_hi]);
    }
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

  async function _get(){
    // offline は GET せず netErr 相当に倒す (spec §3: 自動リトライ無し) —
    // refresh() 側が読んだ owner ローカル snapshot が readonly フォール
    // バックとしてそのまま適用される。connecting 中は通常通り発行する —
    // 成功応答は到達証拠 (spec §5) として状態機械を online に引き上げる
    // (verdict は BookqApi→fetchJson 既定の Connectivity.fetch が流す)。
    if (window.Connectivity && window.Connectivity.isOffline()){
      return { netErr: true };
    }
    var ctl = (typeof AbortController === "function") ? new AbortController() : null;
    var timer = ctl ? setTimeout(function(){ try { ctl.abort(); } catch(e){} }, GET_TIMEOUT_MS) : null;
    try {
      var snap = await window.BookqApi.getVisibility({ signal: ctl ? ctl.signal : null });
      if (timer) clearTimeout(timer);
      return { json: snap };
    } catch (err) {
      if (timer) clearTimeout(timer);
      var status = (err && typeof err.status === "number") ? err.status : 0;
      if (status === 401 || status === 403) return { authFail: true };
      if (status !== 0) return { httpErr: status };
      return { netErr: true };
    }
  }

  function _resolveGet(out, cached, issueGen){
    if (out === null) return;                 // defensive — nothing resolves null
    var prevStatus = _status;
    if (out && typeof out === "object" && "json" in out && out.json){
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
    } else if (out && typeof out === "object" && "authFail" in out){
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
  // 大小文字・Unicode 合成差を同一視する比較キー。大小文字無視の fs では
  // サーバが "HIDE_ME" を "hide_me" に解決してしまうので、マッチングだけは
  // この正規化で行う（_hidden 自体は表示・PUT・ツリー照合用に元名のまま）。
  function _normKey(p){
    var s = String(p);
    try { s = s.normalize("NFC"); } catch(e){}
    return s.toLowerCase();
  }
  // 3種の一致: self（path 自身が hidden key）、descendant（hidden key の
  // 配下）、ancestor（path が hidden key の祖先 — 浅い window の未 flatten
  // 名など、実ツリーノードでない経路が出す暫定 tile 用）。返す key は元名。
  // covering（self/descendant）は longest key wins、ancestor は最短の
  // hidden key（最も近い子孫）を返す。
  function hiddenMatch(path){
    var norm = _normalizePath(path);
    if (norm === null || norm === "") return null;
    var nkey = _normKey(norm);
    var bestCover = null;
    var bestAnc = null;
    _hiddenNorm.forEach(function(orig, kn){
      if (nkey === kn || nkey.indexOf(kn + "/") === 0){
        if (bestCover === null || kn.length > bestCover.length) bestCover = kn;
      } else if (kn.indexOf(nkey + "/") === 0){
        if (bestAnc === null || kn.length < bestAnc.length) bestAnc = kn;
      }
    });
    if (bestCover !== null){
      return { key: _hiddenNorm.get(bestCover) || bestCover,
               kind: (bestCover === nkey) ? "self" : "descendant" };
    }
    if (bestAnc !== null){
      return { key: _hiddenNorm.get(bestAnc) || bestAnc, kind: "ancestor" };
    }
    return null;
  }
  // The hidden key that covers path (path === K or path under K + "/"), or
  // null. Longest key wins so a hidden flattened root beats a hidden
  // ancestor prefix. ancestor 一致は含まない — deny 系は covering のみで
  // 判定する（wrap への潜入自体は許し、中身は子孫側で絞る）。
  function hiddenRootFor(path){
    var m = hiddenMatch(path);
    return (m && m.kind !== "ancestor") ? m.key : null;
  }
  // Positive-hidden only: with no snapshot applied the hidden set is empty,
  // so error/unknown state can never masquerade as "hidden".
  function isHidden(path){ return hiddenRootFor(path) !== null; }
  function isVisible(path){
    if (_showAll) return true;
    if (!_hasSnapshot) return false;   // fail closed — never assume "all visible"
    // ancestor も落とす — hidden key の祖先に過ぎない暫定 tile は
    // 通常モードでは出さない（中身が全部 hidden なので）。
    return hiddenMatch(path) === null;
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
  // everything under it, 「配下に非表示」 on an ancestor-only match —
  // ancestor tile は実ツリーノードでないので editable は必ず false
  // （PUT すれば 404 になるトグルは絶対に出さない）。
  function presentation(path, opts){
    opts = opts || {};
    var m = hiddenMatch(path);
    var kind = m ? m.kind : null;
    var hidden = (kind === "self" || kind === "descendant");
    var cls = (kind === "self") ? "is-hidden"
            : (kind === "descendant") ? "is-hidden-descendant"
            : (kind === "ancestor") ? "is-hidden-ancestor" : "";
    var badge = !kind ? ""
      : '<span class="visibility-badge">' +
        (kind === "self" ? "非表示"
         : kind === "descendant" ? "上位フォルダが非表示"
         : "配下に非表示") + "</span>";
    var editable = (opts.editable !== undefined) ? !!opts.editable : isTopLevel(path);
    if (kind === "ancestor") editable = false;
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
  async function setHidden(root, hidden){
    if (_validRootPath(root) === null)
      return { ok: false, invalid: true };
    if (_saving) return { ok: false, busy: true };
    if (_readOnly || _offline || (typeof navigator !== "undefined" && navigator.onLine === false)
        || (window.Connectivity && window.Connectivity.isOffline())){
      _updateStatusDom();
      return { ok: false, offline: true };
    }
    _saving = true; _setGridBusy(true); _updateStatusDom();
    var issueGen = ++_reqGen;
    try {
      var snap = await window.BookqApi.putVisibility(root, !!hidden, _owner || "");
      _saving = false; _setGridBusy(false);
      var srvOwner = snap.owner;
      var srvRev = snap.revision;
      // The PUT response is the authoritative post-write snapshot — applied
      // when it is not behind the local revision. A PUT resolving BEHIND
      // the current revision means another device's write landed via a
      // GET while this write was in flight: the stale payload is not
      // adopted; exactly one convergence GET pulls the true head instead.
      // (owner 切替の force 採用はこの大小比較より優先。)
      _appliedGen = Math.max(_appliedGen, issueGen);
      _status = "ready"; _readOnly = false; _lastError = null;
      if (srvOwner !== _owner || srvRev >= _revision){
        _applySnapshot(srvOwner, srvRev, snap.hiddenRoots,
          { force: srvOwner !== _owner, allowDownrev: true });
        _convergenceSent = false;
      } else {
        _convergeOnce();
      }
      _updateStatusDom();
      _resolveReady();
      return { ok: true };
    } catch (err) {
      _saving = false; _setGridBusy(false);
      var status = (err && typeof err.status === "number") ? err.status : 0;
      if (status === 409){
        // The owner the page booted under is stale — pull the server's
        // authoritative owner/snapshot instead of writing to another user.
        _status = "error"; _lastError = "owner"; _updateStatusDom();
        refresh();
        _resolveReady();
        return { ok: false, status: 409 };
      }
      if (status === 401 || status === 403){
        _status = "error"; _lastError = "auth"; _updateStatusDom();
        _resolveReady();
        return { ok: false, status: status };
      }
      _lastError = "save"; _updateStatusDom();
      _resolveReady();
      if (status !== 0) return { ok: false, status: status };
      return { ok: false, net: true };
    }
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
    // body.fv-showall drives the status notice's visibility AND the red root
    // icon — in normal mode nothing about this feature is visible anywhere.
    if (typeof document !== "undefined" && document.body){
      document.body.classList.toggle("fv-showall", _showAll);
    }
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
    var g = /** @type {any} */ (grid);
    if (!grid || g.__fvDelegated) return;
    g.__fvDelegated = true;
    grid.addEventListener("click", function(e){
      var rt = _closest(e.target, ".fv-grid-retry");
      if (rt && grid.contains(rt)){
        e.preventDefault(); e.stopPropagation();
        if (e.stopImmediatePropagation) e.stopImmediatePropagation();
        _startRetry();
        return;
      }
      var btn = _closest(e.target, ".visibility-toggle");
      if (!btn || !grid.contains(btn)) return;
      e.preventDefault();
      e.stopPropagation();
      if (e.stopImmediatePropagation) e.stopImmediatePropagation();
      if (_saving) return;
      var root = btn.getAttribute("data-root");
      if (!root) return;
      // self 一致なら格納キー（元名）を PUT 対象にする — 大小文字違いの
      // キーでも解除が確実に同じ行を消す。hide は tile パスそのまま。
      var _m = hiddenMatch(root);
      var _selfHidden = !!(_m && _m.kind === "self");
      setHidden(_selfHidden ? _m.key : root, !_selfHidden);
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

  // ── hidden entry gesture ──
  // Swipe the root 📁 header icon all the way to the right edge of the
  // screen to toggle full-display mode — the same gesture exits. The icon
  // only exists at the browse root, and only people who know the gesture
  // can reach the mode; nothing about it is discoverable in normal mode.
  // Activation waits for pointerup AT the edge: while pointer capture is
  // still held on the icon, the release's click is dispatched to the icon —
  // activating mid-drag would re-render the header, drop the capture, and
  // the release would instead land a stray click on whatever sits past the
  // sidebar's 280px edge (closing the sidebar / hitting viewer controls).
  function _bindEntryGesture(){
    var h = document.getElementById("browse-header");
    var hh = /** @type {any} */ (h);
    if (!h || hh.__fvGesture) return;
    hh.__fvGesture = true;
    var drag = null; // {icon, x0, y0, pid, armed}
    var eatClick = false;
    // Swallow exactly the click produced by an activating release — it is
    // the gesture's own tail, not a user click on whatever is under it.
    if (typeof document !== "undefined"){
      document.addEventListener("click", function(e){
        if (!eatClick) return;
        eatClick = false;
        e.preventDefault(); e.stopPropagation();
        if (e.stopImmediatePropagation) e.stopImmediatePropagation();
      }, true);
    }
    function _reset(d){
      if (d && d.icon) d.icon.style.transform = "";
      return null;
    }
    function _edge(x){
      var w = (typeof window !== "undefined" && window.innerWidth) || 0;
      return w > 0 && x >= w * 0.9;
    }
    function _activate(){
      drag = _reset(drag);
      var next = !_showAll;
      eatClick = true;
      setTimeout(function(){ eatClick = false; }, 0);
      setShowAll(next);
      if (typeof window.showToast === "function"){
        window.showToast(next ? "全表示モード（再読み込みで解除）" : "通常モードに戻しました");
      }
    }
    h.addEventListener("pointerdown", function(e){
      var icon = _closest(e.target, "#browse-root-icon");
      if (!icon || !h.contains(icon)) return;
      // リリースがヘッダ外に出ても必ず icon（→ document まで bubble）へ届く
      // よう pointerdown 時点で capture する — arm 時点の capture だと外側
      // リリースで drag が残り、後の hover がアイコンを動かしてしまう。
      try { icon.setPointerCapture(e.pointerId); } catch(_){}
      drag = { icon: icon, x0: e.clientX, y0: e.clientY, pid: e.pointerId, armed: false };
    });
    // move/up/cancel は document 側で拾う — capture retarget でもそのまま
    // bubble するので、ヘッダ外リリースでも必ず drag を閉じられる。
    if (typeof document !== "undefined"){
      document.addEventListener("pointermove", function(e){
        if (!drag || e.pointerId !== drag.pid) return;
        var dx = e.clientX - drag.x0, dy = e.clientY - drag.y0;
        if (!drag.armed){
          // Arm only on a clearly rightward move — vertical scroll or a left
          // flick releases the gesture instead of hijacking it.
          if (dx > 12 && dx > Math.abs(dy) * 1.2){
            drag.armed = true;
          } else if (dx < -12 || Math.abs(dy) > 12){ drag = _reset(drag); return; }
          else return;
        }
        e.preventDefault();
        drag.icon.style.transform = "translateX(" + Math.max(0, dx) + "px)";
      });
      document.addEventListener("pointerup", function(e){
        if (!drag || e.pointerId !== drag.pid) return;
        if (drag.armed && _edge(e.clientX)) _activate();
        else drag = _reset(drag);
      });
      document.addEventListener("pointercancel", function(e){
        if (drag && e.pointerId === drag.pid) drag = _reset(drag);
      });
    }
    if (typeof window !== "undefined"){
      // OS 横取り等で up/cancel が欠けても残さない — 保険のリセット。
      window.addEventListener("blur", function(){ drag = _reset(drag); });
    }
  }

  function init(opts){
    if (_inited) return;
    _inited = true;
    opts = opts || {};
    _owner = (typeof opts.owner === "string" && opts.owner) ? opts.owner : null;
    _onChange = (typeof opts.onChange === "function") ? opts.onChange : null;
    _offline = (typeof navigator !== "undefined" && navigator.onLine === false);
    _status = "loading"; _updateStatusDom();
    // Same-owner cached snapshot applies immediately (marks read-only intent
    // until the GET resolves); a mismatched/corrupt cache is ignored entirely.
    var cached = _owner ? _readCache(_owner) : null;
    if (cached) _applySnapshot(cached.owner, cached.revision, cached.hiddenRoots, { cache: false });
    // index.html 埋め込み snapshot — render 時点のサーバ値。owner 一致なら
    // 初回 GET を待たず第一描画から絞り込み済みになり、GET は再同期だけに
    // なる。cache より古い rev は _applySnapshot の単調性が弾く。
    var embed = opts.snapshot;
    if (embed && typeof embed === "object" && embed.owner === _owner &&
        typeof embed.revision === "number" && Array.isArray(embed.hiddenRoots)){
      _applySnapshot(embed.owner, embed.revision, embed.hiddenRoots, { cache: false });
      _writeCache();
    }
    _bindDelegatedToggle();
    _bindRefreshTriggers();
    _bindEntryGesture();
    refresh();
  }

  function ready(){ return _readyPromise; }

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

  // render.js / facets.js 共用の薄い表示ヘルパ — showAll 中だけ
  // presentation を返し、内部例外でも必ず null で落ちる。
  function uiPresentation(path, editable){
    if (!_showAll) return null;
    var opts = (editable === undefined) ? {} : {editable: !!editable};
    try { return presentation(path, opts); } catch(e){ return null; }
  }

  window.FolderVisibility = {
    apiVersion: API_VERSION,
    init: init,
    ready: ready,
    refresh: refresh,
    rootForPath: rootForPath,
    hiddenRootFor: hiddenRootFor,
    hiddenMatch: hiddenMatch,
    isHidden: isHidden,
    isVisible: isVisible,
    isTopLevel: isTopLevel,
    setTopLevelPaths: setTopLevelPaths,
    guardPath: guardPath,
    blockedGridHtml: blockedGridHtml,
    uiPresentation: uiPresentation,
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
