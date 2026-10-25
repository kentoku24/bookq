/* dual-reader viewer composition — S09 (#84).
 *
 * The single integration point that glues the pairing UI, the protocol
 * client, the navigation port and the paired-view projection:
 *
 *     PairUI ⇄ PairController ⇄ PairClient ⇄ ViewerNavigation ⇄ PairedView
 *
 * Responsibilities (parent spec #75 §3.3–§3.6):
 *  - Lazily owns the PairClient — no socket exists until create()/join()/
 *    resume() actually runs, and stored credentials re-attach a member on
 *    every page boot so a reload inside the resume window rejoins the pair.
 *  - While the session is member-bound, installs the paired handler via
 *    ViewerNavigation.use(): every user intent becomes exactly one wire
 *    request through client.sendIntent(); committed STATE frames return
 *    through ViewerNavigation.applyCommittedState() → PairedView — never
 *    through request(), so a remote apply can never echo as a user intent.
 *  - Leader browse semantics: opening the sidebar pauses the shared session
 *    at 'selecting' (SET_ACTIVITY), closing without a pick resumes it
 *    ('active') and replays the newest held commit; a follower picking a
 *    work releases the pair first, then runs the pick as a plain solo op.
 *  - Session end (leave/END/reject/expiry) restores the solo handler,
 *    tears paired rendering down and drops every subscription/listener.
 */
(function () {
  'use strict';

  var CREDS_KEY = 'bookq.readerSync.v1';
  var SENT_CAP = 200;

  var _client = null;
  var _subs = [];            // PairUI subscribers (unsub via returned fn)
  var _eventSubs = [];       // client event subscribers (ui-sync relay etc.)
  var _navUnuse = null;      // installed ViewerNavigation handler's unuse()
  var _sideObs = null;       // MutationObserver on #sidebar class
  var _sideOpen = null;      // last observed .open state
  var _openWorkSent = false; // leader bootstrap OPEN_WORK already dispatched
  var _selIssued = false;    // a SET_ACTIVITY selecting is outstanding
  var _sawJoinReq = false;   // joinRequest edge — auto-approve (#114)
  var _heldCommit = null;    // newest suppressed commit while leader browses
  var _lastApply = null;     // newest committed args, for display re-arm
  var _appliedKey = null;    // display key of the newest APPLIED commit
  var _resolveGen = 0;       // generation guard for async page resolution
  var _sent = [];            // intent audit log (tests: 1 op = 1 request)

  function _VN() { return globalThis.ViewerNavigation; }
  function _PV() { return globalThis.PairedView; }

  function _paired(st) {
    var p = st && st.presence;
    return !!p && Object.keys(p).length >= 2;
  }

  /* Member-bound && alive — the window where user movement is a wire op.
   * 'error'/'ended' are terminal surfaces, 'idle' means no session. */
  function _memberBound(st) {
    return !!st && !!st.sessionId && st.status !== 'idle' &&
           st.status !== 'ended' && st.status !== 'error';
  }

  function _sidebarOpen() {
    var el = typeof document !== 'undefined' &&
             document.getElementById('sidebar');
    return !!(el && el.classList && el.classList.contains('open'));
  }

  /* ── committed-state sink ── */

  /* What the display actually shows for a commit — phase/presence flaps
   * (selecting/active/paused activity echoes) don't move it, but a work
   * pick or a remote turn does. */
  function _displayKey(snap) {
    if (!snap) return '';
    return [snap.manifestVersion, snap.spreadId, snap.offset,
            snap.leaderSide].join('|');
  }

  function _applyCommitted(snap, manifest, o) {
    _lastApply = {snap: snap, manifest: manifest, o: o};
    var leader = !!(o && o.role === 'leader');
    if (leader && _sidebarOpen() && _displayKey(snap) === _appliedKey) {
      // Leader is browsing the shared-work list: an echo that changes
      // nothing on screen must not rip the grid away — hold it; the
      // newest held commit replays on close. This has to key on display
      // state, not phase: a dead member pins the derived phase at
      // 'paused' even while the leader selects, so a peer-away STATE
      // would otherwise apply, re-enter the torn-down pair strip and
      // slam the sidebar shut. Real display moves (work pick, remote
      // turn) still apply immediately and close the list.
      _heldCommit = _lastApply;
      return;
    }
    var ret = _VN().applyCommittedState(snap, manifest, o);
    _appliedKey = _displayKey(snap);
    return ret;
  }

  /* ── sidebar observer: leader browse pauses/resumes the session ── */

  function _ensureSidebarObs() {
    if (_sideObs || typeof document === 'undefined' ||
        typeof MutationObserver !== 'function') return;
    var el = document.getElementById('sidebar');
    if (!el) return;
    _sideOpen = el.classList.contains('open');
    _sideObs = new MutationObserver(function () {
      var open = el.classList.contains('open');
      if (open === _sideOpen) return;
      _sideOpen = open;
      var st = _client ? _client.state : null;
      if (!_memberBound(st) || !_paired(st)) return;
      if (open) {
        if (st.role === 'leader' && !_selIssued && st.phase !== 'selecting') {
          _selIssued = true;
          _client.setActivity('selecting');
        }
        return;
      }
      // Close: re-arm the shared display first, then resume the session —
      // covers cancel-after-browse on the leader and any member whose local
      // browse tore the paired strip down without a commit landing.
      var pv = _PV();
      if (_heldCommit) {
        var c = _heldCommit;
        _heldCommit = null;
        try {
          _VN().applyCommittedState(c.snap, c.manifest, c.o);
          _appliedKey = _displayKey(c.snap);
        } catch (e) { /* display replays are best-effort */ }
      } else if (_lastApply && pv && typeof pv.isActive === 'function' &&
                 !pv.isActive()) {
        var d = _lastApply;
        try {
          _VN().applyCommittedState(d.snap, d.manifest, d.o);
          _appliedKey = _displayKey(d.snap);
        } catch (e2) { /* idem */ }
      }
      if (st.role === 'leader' && (_selIssued || st.phase === 'selecting')) {
        _selIssued = false;
        _client.setActivity('active');
      }
    });
    _sideObs.observe(el, {attributes: true, attributeFilter: ['class']});
  }

  /* ── paired navigation handler ── */

  /* Leave the pair, then run the intent as the solo operation it became.
   * Used for follower picks (spec: follower work change = LEAVE + solo) and
   * as the safety valve when the session is already gone. */
  function _bailToSolo(intent) {
    _uninstallNav();
    if (_client) { try { _client.leave(); } catch (e) { /* idempotent */ } }
    return _VN().request(intent);   // re-enters the default (solo) handler
  }

  /* The one non-data intent for the leader: browsing the work list. */
  function _leaderBrowse(via) {
    var ok = _client ? _client.sendIntent({type: 'browse'}) : false;
    _record({type: 'browse', via: via}, ok);
    _selIssued = _selIssued || ok;
    if (typeof window.returnToBrowse === 'function') {
      try { window.returnToBrowse(); } catch (e) { /* browse is local */ }
    }
    return true;
  }

  /* The current displayed page as a PageRef — skips spacer cells. */
  function _currentPageRef() {
    var imgs = window.images || [];
    var i = typeof window.currentIdx === 'number' ? window.currentIdx : 0;
    for (var d = 0; d < imgs.length; d++) {
      var lo = i - d, hi = i + d;
      if (lo >= 0) {
        var a = imgs[lo];
        if (a && !a.isSpacer && typeof a.name === 'string') {
          return {folder: a.folder, name: a.name};
        }
      }
      if (d > 0 && hi < imgs.length) {
        var b = imgs[hi];
        if (b && !b.isSpacer && typeof b.name === 'string') {
          return {folder: b.folder, name: b.name};
        }
      }
      if (lo < 0 && hi >= imgs.length) break;
    }
    return null;
  }

  /* Chapter-page names for a leaf folder — the same precedence catalog.py's
   * _chapter_pages applies server-side: leaf folders list all children,
   * mixed folders list image children only. */
  function _treeNames(folder) {
    var node = (window.getNodeAt && folder) ? window.getNodeAt(folder) : null;
    var OC = window.OfflineCore || {};
    if (!node || !Array.isArray(node.children)) return null;
    if (OC.folderHasOnlyImages && OC.folderHasOnlyImages(node)) {
      return node.children.map(function (c) { return c.name; });
    }
    if (OC.folderHasImages && OC.folderHasImages(node)) {
      return node.children
        .filter(function (c) { return c.type === 'image'; })
        .map(function (c) { return c.name; });
    }
    return null;
  }

  function _pickRef(folder, names) {
    if (!names || !names.length) return null;
    var idx = 0;
    try {
      var saved = window.ReadingProgress && window.ReadingProgress.get
        ? window.ReadingProgress.get(folder) : null;
      if (saved && typeof saved.page === 'number' && saved.page > 0) {
        idx = Math.min(saved.page, names.length - 1);
      }
    } catch (e) { /* resume is best-effort */ }
    return {folder: folder, name: names[idx]};
  }

  /* Resolve the initial PageRef for a leader OPEN_WORK: the saved resume
   * point inside that chapter when one exists, else its first page. The
   * tree answers synchronously; a fetch covers paths the tree lacks. */
  function _resolveInitialPageRef(folder, done) {
    var names = _treeNames(folder);
    if (names) { done(_pickRef(folder, names)); return; }
    fetch('/api/images?folder=' + encodeURIComponent(folder))
      .then(function (r) { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
      .then(function (list) {
        done(Array.isArray(list) ? _pickRef(folder, list) : null);
      })
      .catch(function () { done(null); });
  }

  function _leaderOpenWork(intent) {
    var gen = ++_resolveGen;
    function emit(page) {
      if (!page || typeof page.folder !== 'string' ||
          typeof page.name !== 'string') {
        _record({type: 'openWork', via: intent.via, folder: intent.folder,
                 page: page || null}, false);
        return false;
      }
      var ok = _client && _client.sendIntent({
        type: 'openWork', folder: intent.folder, page: page,
      });
      _record({type: 'openWork', via: intent.via, folder: intent.folder,
               page: page}, ok);
      return ok;
    }
    if (intent.page) return emit(intent.page);
    _resolveInitialPageRef(intent.folder, function (ref) {
      if (gen === _resolveGen) emit(ref);
    });
    return true;   // consumed — the async resolve decides the wire op
  }

  function _navHandler(intent) {
    var st = _client ? _client.state : null;
    if (!_memberBound(st) || !st.role) return _bailToSolo(intent);
    switch (intent && intent.type) {
      case 'turn': case 'goto': case 'setOffset': {
        var ok = _client.sendIntent(intent);
        _record(intent, ok);
        if (!ok && intent.type === 'turn' && intent.via === 'swipe' &&
            typeof window.viewerSnapTo === 'function') {
          // Dropped (e.g. mid-reconnect): the drag must not strand the
          // strip away from the committed page — bounce back to current.
          try { window.viewerSnapTo(window.currentIdx, false); }
          catch (e) { /* visual only */ }
        }
        return ok;
      }
      case 'openWork':
        return st.role === 'leader' ? _leaderOpenWork(intent)
                                    : _bailToSolo(intent);
      case 'browse':
        if (st.role === 'leader') return _leaderBrowse(intent.via);
        // Follower browsing is a local look at the list — the pair stays
        // (a work PICK is what releases it); the next commit pulls the
        // shared display back in front. Mirrors the solo browse case.
        _record({type: 'browse', via: intent.via, local: true}, true);
        if (typeof window.returnToBrowse === 'function') {
          try { window.returnToBrowse(); } catch (e) { /* local only */ }
        }
        return true;
      default:
        return false;
    }
  }

  function _installNav() {
    if (_navUnuse || !_VN() || typeof _VN().use !== 'function') return;
    _navUnuse = _VN().use(_navHandler);
  }

  function _uninstallNav() {
    if (!_navUnuse) return;
    try { _navUnuse(); } catch (e) { /* idempotent */ }
    _navUnuse = null;
  }

  /* ── session lifecycle ── */

  /* Pair established with no shared work: the leader opens its current
   * book as the session's first manifest — the shared spread then contains
   * the leader's current PageRef (parent spec §3.3). */
  function _maybeBootstrapWork(st) {
    if (_openWorkSent || !st || st.role !== 'leader' ||
        st.phase !== 'selecting' || st.manifestVersion) return;
    var folder = window.currentFolder;
    var page = _currentPageRef();
    if (typeof folder !== 'string' || !folder || !page) return;
    var ok = _client.sendIntent({type: 'openWork', folder: folder, page: page});
    _record({type: 'openWork', via: 'pair-bootstrap', folder: folder,
             page: page}, ok);
    // A dropped send (transient link loss) retries on the next state notify.
    _openWorkSent = !!ok;
  }

  function _teardown() {
    _uninstallNav();
    _openWorkSent = false;
    _selIssued = false;
    _heldCommit = null;
    _lastApply = null;
    _appliedKey = null;
    _resolveGen++;
    if (_sideObs) { try { _sideObs.disconnect(); } catch (e) {} _sideObs = null; }
    _sideOpen = null;
    try {
      var pv = _PV();
      if (pv && typeof pv.leave === 'function') pv.leave();
    } catch (e) { /* teardown must never throw into the client */ }
  }

  function _onClientState(st) {
    if (st && st.phase !== 'selecting') _selIssued = false;
    // Auto-approve (#114): a JOIN reaching the leader is answered at once —
    // there is no manual approve step in the UI. Edge-triggered so a pending
    // request yields exactly one APPROVE; client.approve() is idempotent for
    // explicit/programmatic callers.
    var hasReq = !!(st && st.joinRequest && st.role === 'leader');
    if (hasReq && !_sawJoinReq && _client) {
      _client.approve().catch(function () { /* wire outcome lands via REJECT/STATE */ });
    }
    _sawJoinReq = hasReq;
    var bound = _memberBound(st) && _paired(st);
    if (bound && !_navUnuse) {
      _installNav();
      _ensureSidebarObs();
    } else if (!bound && _navUnuse) {
      _uninstallNav();
    }
    if (!bound && _sideObs) {
      try { _sideObs.disconnect(); } catch (e) { /* idempotent */ }
      _sideObs = null;
      _sideOpen = null;
      _heldCommit = null;
      _appliedKey = null;
    }
    if (bound) _maybeBootstrapWork(st);
    _notify();
  }

  function _onClientEvent(e) {
    if (e && e.type === 'ended') {
      _teardown();
      if (e.reason === 'incompatible' &&
          typeof window.showErrorToast === 'function') {
        // Version skew: sync must stop; updating the shell is the remedy.
        try { window.showErrorToast('更新あり: 再読み込みで最新版が適用されます'); }
        catch (x) { /* notice is best-effort */ }
      }
    }
    _eventSubs.slice().forEach(function (cb) {
      try { cb(e); } catch (x) { /* subscriber isolation */ }
    });
    // 'ui-sync' frames carry no state change — notifying every state
    // subscriber per scroll frame is pure waste.
    if (e && e.type === 'ui-sync') return;
    _notify();
  }

  function _ensureClient() {
    if (_client) return _client;
    if (!globalThis.PairClient ||
        typeof globalThis.PairClient.createClient !== 'function') {
      throw new Error('pairClient unavailable');
    }
    _client = globalThis.PairClient.createClient({
      webrtc: {},                 // enable the webrtc-dc upgrade path
      applyState: _applyCommitted,
      onEvent: _onClientEvent,
    });
    _client.subscribe(_onClientState);
    return _client;
  }

  function _notify() {
    var s = api.state;
    _subs.slice().forEach(function (cb) {
      try { cb(s); } catch (e) { /* subscriber isolation */ }
    });
  }

  function _record(intent, ok) {
    _sent.push({type: intent.type, via: intent.via || null,
                folder: intent.folder, page: intent.page, ok: !!ok});
    if (_sent.length > SENT_CAP) _sent.shift();
  }

  /* Boot: a still-valid credential rejoins the pair without touching the
   * UI — the reload lands straight back on the shared spread (S07 resume).
   * No credentials ⇒ zero sockets, zero subscriptions (solo untouched). */
  function _readCreds() {
    try {
      var raw = window.sessionStorage &&
                window.sessionStorage.getItem(CREDS_KEY);
      if (!raw) return null;
      var d = JSON.parse(raw);
      return (d && d.sessionId && d.memberId && d.resumeToken) ? d : null;
    } catch (e) { return null; }
  }

  function _boot() {
    if (!_readCreds()) return;
    try { _ensureClient().resume(); } catch (e) { /* rejoin is best-effort */ }
  }

  /* ── public controller surface (PairUI contract + S09 additions) ── */

  var api = {
    create: function () { return _ensureClient().create(); },
    join: function (code) { return _ensureClient().join(code); },
    /* One-tap connect: server-side MATCH — follower if a fresh session is
     * waiting, waiting leader otherwise. connect(pickId) binds a chosen
     * ambiguous candidate. Resolves {ambiguous|candidates} or
     * {unsupported} for UI fallback handling. */
    connect: function (pick) { return _ensureClient().match(pick); },
    approve: function () {
      return _client ? _client.approve() : Promise.resolve({approved: false});
    },
    reject: function () {
      return _client ? _client.reject() : Promise.resolve({rejected: false});
    },
    resume: function () { return _ensureClient().resume(); },
    leave: function () {
      return _client ? _client.leave() : Promise.resolve({left: false});
    },
    swapSides: function () {
      return _client ? _client.swapSides() : false;
    },
    /* Leader "作品変更": pause the session at 'selecting' and open the
     * shared-work list locally; the next tile pick goes out as OPEN_WORK. */
    changeWork: function () {
      var st = _client ? _client.state : null;
      if (!_memberBound(st) || st.role !== 'leader') return false;
      return _leaderBrowse('change-work');
    },
    sendIntent: function (intent) {
      return _client ? _client.sendIntent(intent) : false;
    },
    /* Ephemeral mirrored-UI relay (browse scroll mirror). Fire-and-forget
     * boolean — the companion onEvent('ui-sync') subscription receives
     * peer frames verbatim. */
    sendUiSync: function (payload) {
      return _client && typeof _client.sendUiSync === 'function'
        ? _client.sendUiSync(payload) : false;
    },
    /* Raw client-event subscription (same shape as PairClient's onEvent
     * sink). Unlike subscribe() — which notifies with the public state —
     * this delivers events verbatim; PairBrowseSync consumes 'ui-sync'
     * frames through it. */
    onEvent: function (cb) {
      if (typeof cb !== 'function') throw new Error('subscriber must be a function');
      _eventSubs.push(cb);
      return function () {
        var i = _eventSubs.indexOf(cb);
        if (i >= 0) _eventSubs.splice(i, 1);
      };
    },
    attachDataChannel: function (dc) {
      if (_client && typeof _client.attachDataChannel === 'function') {
        _client.attachDataChannel(dc);
      }
    },
    subscribe: function (cb) {
      if (typeof cb !== 'function') throw new Error('subscriber must be a function');
      _subs.push(cb);
      return function () {
        var i = _subs.indexOf(cb);
        if (i >= 0) _subs.splice(i, 1);
      };
    },
    destroy: function () {
      _teardown();
      _subs = [];
      _eventSubs = [];
      if (_client) {
        try { _client.destroy(); } catch (e) { /* idempotent */ }
        _client = null;
      }
    },
    get client() { return _client; },
    get state() {
      return _client ? _client.state : {status: 'idle'};
    },
    _sentLog: _sent,
  };

  /* Test seam: an injected controller (test_reader_pairing_ui.py installs
   * window.PairController before defer scripts run) always wins. */
  if (!globalThis.PairController) {
    globalThis.PairController = api;
    if (typeof document !== 'undefined' &&
        document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', _boot);
    } else if (typeof document !== 'undefined') {
      _boot();
    }
  }
})();
