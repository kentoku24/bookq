/* dual-reader sync client — pairing, committed-state application, and the
 * disconnect/resume state machine (#82 / S07). Contract: parent issue #75
 * §3.3–§3.5; sibling modules pairTransport.js (wire adapters, the Switcher,
 * WebRTC signaling glue), navigation.js (committed-state sink), pairUI.js
 * (controller interface).
 *
 * DOM-free: this module never queries the DOM, never opens a socket by
 * itself beyond the injected transport factory, and never reads the clock —
 * transport factory, timers, clock, storage, lifecycle events, the
 * committed-state applier and the event sink are all injected, so tests can
 * drive every edge deterministically.
 *
 * Controller surface (the #83 pairing UI consumes a subset; #84 wires the
 * rest into the viewer):
 *   create() / join(code)          — start a session (leader / follower)
 *   approve() / reject()           — leader answers a pending join
 *   resume()                       — reattach with stored member creds
 *   leave()                        — immediate local teardown (+LEAVE if up)
 *   sendIntent(intent)             — navigation-port intents -> wire frames
 *   setActivity(state)             — direct SET_ACTIVITY (browse observer, S09)
 *   sendUiSync(payload)            — ephemeral UI_SYNC relay (browse scroll)
 *   swapSides() / changeWork()     — convenience wrappers used by the UI
 *   attachDataChannel(dc)          — plug a live RTCDataChannel
 *   subscribe(cb) -> unsubscribe   — state notifications (pairUI contract)
 *   destroy()                      — full teardown, zero residual timers
 *   state                          — {status, role, memberId, sessionId,
 *                                     code, joinRequest, side, transport,
 *                                     phase, spreadId, revision, workEpoch,
 *                                     manifestVersion, presence, error,
 *                                     endReason}
 *
 * Wire contract (S01 versioned frames):
 *   control channel — the always-on WebSocket: CREATE/JOIN/APPROVE/REJECT/
 *     RESUME/LEAVE/SET_ACTIVITY/TRANSPORT_STATE/SIGNAL/PONG.
 *   data plane — the Switcher's active transport: TURN/GOTO/SET_OFFSET/
 *     SWAP_SIDES/OPEN_WORK/READY/APPLIED.
 *
 * Reconnect (§3.5): link loss while member-bound enters 'reconnecting' —
 * retries at 0.5/1/2/5s + jitter capped at 5s, the 5min resume window is
 * mirrored locally, RESUME{memberId,resumeToken} re-binds the member and the
 * server answers with the latest STATE + MANIFEST. Nothing is replayed:
 * intents dropped while offline and sends left unconfirmed by the drop are
 * never re-sent — the committed snapshot resolves their outcome.
 */
(function () {
  'use strict';

  var PROTOCOL_VERSION = 1;
  var DEFAULT_PATH = '/ws/reader-sync';
  var STORAGE_KEY = 'bookq.readerSync.v1';
  var RESUME_WINDOW_MS = 300 * 1000;      // sessions.RESUME_WINDOW_SECONDS
  var ATTEMPT_TIMEOUT_MS = 10 * 1000;     // connect+HELLO+entry-ACK budget
  var BACKOFF_BASE_MS = [500, 1000, 2000, 5000];
  var BACKOFF_CAP_MS = 5000;              // retry interval cap incl. jitter
  var BACKOFF_JITTER_MS = 500;
  var MAX_MANIFEST_BYTES = 32 * 1024 * 1024;
  var MANIFEST_KIND = 'manifest-chunk';
  // app/reader_sync/catalog.py _CONTENT_KEYS — the digest covers only these.
  var MANIFEST_CONTENT_KEYS =
    ['v', 'folder', 'offset', 'chapters', 'pageRefs', 'spreadTable'];

  var STATUS = {
    IDLE: 'idle', CONNECTING: 'connecting', CONNECTED: 'connected',
    RECONNECTING: 'reconnecting', PEER_AWAY: 'peer-away', LOADING: 'loading',
    ERROR: 'error', ENDED: 'ended',
  };

  // state.endReason — every terminal cause is mutually distinguishable.
  var END_REASON = {
    LEFT: 'left', REJECTED: 'rejected', CODE_EXPIRED: 'code_expired',
    RESUME_EXPIRED: 'resume_expired', SESSION_LOST: 'session_lost',
    RESUME_IN_USE: 'resume_in_use', CREDENTIALS_LOST: 'credentials_lost',
    INCOMPATIBLE: 'incompatible', ERROR: 'error',
  };

  // RESUME REJECT reasons: value === terminal endReason, null === keep retrying
  // (resume_in_use is transient — the peer's stale link is reaped server-side).
  var RESUME_REJECT_END = {
    resume_expired: 'resume_expired',
    bad_resume: 'session_lost',
    resume_in_use: null,
  };
  // END.reason -> endReason ('shutdown' means the server went away — the
  // session it hosted is gone with it).
  var END_REASON_MAP = {
    left: 'left', rejected: 'rejected', code_expired: 'code_expired',
    resume_expired: 'resume_expired', shutdown: 'session_lost',
  };
  var LIFECYCLE_EVENTS = ['hidden', 'visible', 'pagehide', 'pageshow',
                        'online', 'offline'];

  var _ridSeq = 0;

  function _err(code, msg) {
    var e = new Error(code + ': ' + msg);
    e.code = code;
    return e;
  }

  function _rid() {
    return 'r' + (++_ridSeq).toString(36) +
      Math.random().toString(36).slice(2, 8);
  }

  /* ── canonical JSON + sha256 (manifest digest verification) ──────────
   * catalog.py canonical form: json.dumps(sort_keys=True, separators=(',',':'),
   * ensure_ascii=True). Reproduced byte-for-byte: sorted keys, compact
   * separators, every non-ASCII UTF-16 unit escaped to \uXXXX (lowercase). */

  function _jsonStr(s) {
    return JSON.stringify(s).replace(/[\u0080-\uffff]/g, function (ch) {
      var h = ch.charCodeAt(0).toString(16);
      return '\\u' + '0000'.slice(h.length) + h;
    });
  }

  function canonicalJson(v) {
    if (v === null || v === undefined) return 'null';
    if (Array.isArray(v)) {
      return '[' + v.map(canonicalJson).join(',') + ']';
    }
    var t = typeof v;
    if (t === 'number') {
      if (!isFinite(v)) throw _err('ERR_MALFORMED', 'non-finite number');
      return JSON.stringify(v);
    }
    if (t === 'boolean') return v ? 'true' : 'false';
    if (t === 'string') return _jsonStr(v);
    if (t === 'object') {
      var keys = Object.keys(v).sort();
      var out = [];
      for (var i = 0; i < keys.length; i++) {
        out.push(_jsonStr(keys[i]) + ':' + canonicalJson(v[keys[i]]));
      }
      return '{' + out.join(',') + '}';
    }
    throw _err('ERR_MALFORMED', 'unsupported canonical-json type: ' + t);
  }

  var _SHA256_K = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
    0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
    0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
    0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
    0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
    0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ];

  /* sha256 over an ASCII string (canonicalJson output is ASCII-only, so each
   * char code is the byte). Returns lowercase hex. */
  function sha256ascii(msg) {
    var bytes = [];
    for (var i = 0; i < msg.length; i++) bytes.push(msg.charCodeAt(i) & 0xff);
    var bitLen = bytes.length * 8;
    bytes.push(0x80);
    while (bytes.length % 64 !== 56) bytes.push(0);
    var hi = Math.floor(bitLen / 4294967296);
    var lo = bitLen >>> 0;
    bytes.push((hi >>> 24) & 0xff, (hi >>> 16) & 0xff, (hi >>> 8) & 0xff, hi & 0xff,
               (lo >>> 24) & 0xff, (lo >>> 16) & 0xff, (lo >>> 8) & 0xff, lo & 0xff);
    var h = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
             0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
    var w = new Array(64);
    var rr = function (x, n) { return (x >>> n) | (x << (32 - n)); };
    for (var off = 0; off < bytes.length; off += 64) {
      for (i = 0; i < 16; i++) {
        w[i] = (bytes[off + 4 * i] << 24) | (bytes[off + 4 * i + 1] << 16) |
               (bytes[off + 4 * i + 2] << 8) | bytes[off + 4 * i + 3];
      }
      for (i = 16; i < 64; i++) {
        var s0 = rr(w[i - 15], 7) ^ rr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
        var s1 = rr(w[i - 2], 17) ^ rr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
        w[i] = (w[i - 16] + s0 + w[i - 7] + s1) | 0;
      }
      var a = h[0], b = h[1], c = h[2], d = h[3],
          e = h[4], f = h[5], g = h[6], hh = h[7];
      for (i = 0; i < 64; i++) {
        var S1 = rr(e, 6) ^ rr(e, 11) ^ rr(e, 25);
        var ch = (e & f) ^ (~e & g);
        var t1 = (hh + S1 + ch + _SHA256_K[i] + w[i]) | 0;
        var S0 = rr(a, 2) ^ rr(a, 13) ^ rr(a, 22);
        var maj = (a & b) ^ (a & c) ^ (b & c);
        var t2 = (S0 + maj) | 0;
        hh = g; g = f; f = e; e = (d + t1) | 0;
        d = c; c = b; b = a; a = (t1 + t2) | 0;
      }
      h[0] = (h[0] + a) | 0; h[1] = (h[1] + b) | 0; h[2] = (h[2] + c) | 0;
      h[3] = (h[3] + d) | 0; h[4] = (h[4] + e) | 0; h[5] = (h[5] + f) | 0;
      h[6] = (h[6] + g) | 0; h[7] = (h[7] + hh) | 0;
    }
    var hex = '';
    for (i = 0; i < 8; i++) hex += ('0000000' + (h[i] >>> 0).toString(16)).slice(-8);
    return hex;
  }

  /* sha256 hex of the canonical content fields — must equal the server-side
   * manifest_digest() result for the same manifest. */
  function manifestDigestHex(manifest) {
    var content = {};
    for (var i = 0; i < MANIFEST_CONTENT_KEYS.length; i++) {
      var k = MANIFEST_CONTENT_KEYS[i];
      if (!(k in manifest)) throw _err('ERR_MALFORMED', 'manifest missing ' + k);
      content[k] = manifest[k];
    }
    return sha256ascii(canonicalJson(content));
  }

  /* ── lifecycle event plumbing ─────────────────────────────────────────
   * The client listens for semantic lifecycle names on an injected target;
   * domEventTarget adapts document/window into that vocabulary for real use. */

  function domEventTarget(win, doc) {
    var map = {
      hidden: [doc, 'visibilitychange'],
      visible: [doc, 'visibilitychange'],
      pagehide: [win, 'pagehide'],
      pageshow: [win, 'pageshow'],
      online: [win, 'online'],
      offline: [win, 'offline'],
    };
    var reg = [];   // [semanticType, fn, wrappedFn]
    return {
      addEventListener: function (t, f) {
        var m = map[t];
        if (!m || !m[0]) return;
        var w = (t === 'hidden' || t === 'visible')
          ? function () { f({type: doc.hidden ? 'hidden' : 'visible'}); }
          : f;
        reg.push([t, f, w]);
        m[0].addEventListener(m[1], w);
      },
      removeEventListener: function (t, f) {
        var m = map[t];
        if (!m || !m[0]) return;
        for (var i = reg.length - 1; i >= 0; i--) {
          var r = reg[i];
          if (r[0] === t && r[1] === f) {
            m[0].removeEventListener(m[1], r[2]);
            reg.splice(i, 1);
          }
        }
      },
    };
  }

  /* ── client ─────────────────────────────────────────────────────────── */

  function createClient(opts) {
    opts = opts || {};
    var PT = opts.PairTransport || globalThis.PairTransport || null;

    var _now = opts.now || function () { return Date.now(); };
    var _setT = opts.setTimeout ||
      function (fn, ms) { return setTimeout(fn, ms); };
    var _clearT = opts.clearTimeout ||
      function (id) { clearTimeout(id); };
    var _random = opts.random || Math.random;
    var _storage = opts.storage !== undefined ? opts.storage
      : (typeof window !== 'undefined' && window.sessionStorage
         ? window.sessionStorage : null);
    var _events = opts.events !== undefined ? opts.events
      : (typeof window !== 'undefined' && typeof document !== 'undefined'
         ? domEventTarget(window, document) : null);
    var _applyState = opts.applyState || function (snap, manifest, o) {
      if (globalThis.ViewerNavigation &&
          typeof globalThis.ViewerNavigation.applyCommittedState === 'function') {
        return globalThis.ViewerNavigation.applyCommittedState(snap, manifest, o);
      }
      return undefined;
    };
    var _onEvent = opts.onEvent || null;
    var _url = opts.url ||
      (typeof location !== 'undefined'
        ? (location.protocol === 'https:' ? 'wss://' : 'ws://') +
          location.host + DEFAULT_PATH
        : DEFAULT_PATH);
    var _factory = opts.transportFactory || function (url) {
      if (!PT) throw _err('ERR_MALFORMED', 'no transport factory / PairTransport');
      return PT.createWebSocketTransport(url);
    };
    var _resumeWindow = opts.resumeWindowMs || RESUME_WINDOW_MS;
    var _attemptTimeout = opts.attemptTimeoutMs || ATTEMPT_TIMEOUT_MS;
    var _backoff = opts.backoff ||
      {base: BACKOFF_BASE_MS, cap: BACKOFF_CAP_MS, jitter: BACKOFF_JITTER_MS};
    // Client-side liveness: the server heartbeats every ~5s and closes at
    // 15s silence, but a black-holed path (mobile NAT drop, captive-radio
    // freeze) delivers nothing — including the close — so only an inbound
    // watchdog can tell a dead-but-open socket from a quiet one. 20s is
    // four missed PINGs + slack; the check reads _lastRx at fire time.
    var _livenessMs = opts.livenessTimeoutMs || 20000;
    var _webrtc = opts.webrtc || null;   // {pcFactory?} — enables the DC upgrade

    /* ── state ── */
    var _member = null;              // bound member {sessionId,memberId,role,resumeToken}
    var _roleIntent = null;          // role shown while an entry op is in flight
    var _code = null;
    var _pendingMember = null;       // {memberId,name} awaiting leader approval
    var _approvedMemberId = null;    // memberId an APPROVE was already sent for
    var _link = null;                // {t, open, gone, gen}
    var _gen = 0;
    var _connecting = false;         // transport opened, awaiting HELLO
    var _entry = null;               // pending CREATE/JOIN/RESUME {type,payload,sessionId,res,rej,rid,sent}
    var _pending = {};               // requestId -> pending op {res,rej,type}
    var _seen = {epoch: -1, rev: -1};
    var _applied = {epoch: -1, rev: -1};
    var _lastState = null;
    var _manifest = null;            // {workEpoch, manifestVersion, obj}
    var _mchunks = null;             // in-flight chunk reassembly
    var _deferredState = null;       // newest STATE waiting on its manifest
    var _appliedPending = {};        // revision -> committed requestId (APPLIED report)
    var _readyFor = null;            // "epoch:mv" already READYed
    var _timers = {};                // id -> true (outstanding injected timers)
    var _retry = 0;
    var _reconnectT = null;
    var _attemptT = null;
    var _livenessT = null;           // inbound-silence watchdog timer
    var _lastRx = 0;                 // _now() stamp of the last inbound frame
    var _offlineSince = null;
    var _term = null;                // {status, endReason} once terminal
    var _left = false;
    var _destroyed = false;
    var _errorSt = null;             // transient error overlay (ERROR frame / apply fail)
    var _suspended = false;
    var _plane = 'server-ws';
    var _dataT = null;
    var _switcher = (PT && typeof PT.createSwitcher === 'function')
      ? PT.createSwitcher() : null;
    var _dc = {t: null, up: false};
    var _sig = null;
    var _subs = [];
    var _listeners = [];             // [type, wrappedFn] on _events

    /* ── small plumbing ── */

    function _notify() {
      if (_destroyed) return;
      var s = _public();
      _subs.slice().forEach(function (cb) {
        try { cb(s); } catch (e) { /* subscriber errors are isolated */ }
      });
    }

    function _event(type, extra) {
      if (_destroyed || !_onEvent) return;
      var e = {type: type};
      if (extra) for (var k in extra) e[k] = extra[k];
      try { _onEvent(e); } catch (x) { /* sink errors are isolated */ }
    }

    function _later(fn, ms) {
      var id = _setT(function () {
        if (_timers[id]) { delete _timers[id]; fn(); }
      }, ms);
      _timers[id] = true;
      return id;
    }

    function _unlater(slot) {
      var id = slot === 'retry' ? _reconnectT : _attemptT;
      if (id !== null) {
        delete _timers[id];
        _clearT(id);
      }
      if (slot === 'retry') _reconnectT = null; else _attemptT = null;
    }

    function _timersClear() {
      for (var id in _timers) _clearT(id);
      _timers = {};
      _reconnectT = null;
      _attemptT = null;
      _livenessT = null;
    }

    /* Inbound-silence watchdog: armed (once) by every inbound frame and
     * re-checked at fire time, so a queued backlog that updates _lastRx
     * first always wins. A link quiet for _livenessMs is treated as dead —
     * the same path as onclose, so reconnect bookkeeping is identical. */
    function _disarmLiveness() {
      if (_livenessT !== null) {
        delete _timers[_livenessT];
        _clearT(_livenessT);
        _livenessT = null;
      }
    }

    function _armLiveness() {
      if (_livenessT !== null) return;
      _livenessT = _later(function () {
        _livenessT = null;
        if (!_linkOpen()) return;
        if (_now() - _lastRx < _livenessMs) { _armLiveness(); return; }
        var l = _link;
        if (l) { l.gone = true; if (l.t) { try { l.t.close(); } catch (e) {} } }
        _handleLinkGone(_gen);
      }, _livenessMs + 1);             // +1: fire strictly past the deadline
    }

    function _public() {
      return {
        status: _deriveStatus(),
        role: _member ? _member.role : _roleIntent,
        memberId: _member ? _member.memberId : null,
        sessionId: _member ? _member.sessionId : null,
        code: _code,
        joinRequest: !!_pendingMember,
        side: _side(),
        transport: _effPlane(),
        phase: _lastState ? _lastState.phase : null,
        spreadId: _lastState ? _lastState.spreadId : null,
        revision: _seen.rev < 0 ? 0 : _seen.rev,
        workEpoch: _seen.epoch < 0 ? 0 : _seen.epoch,
        manifestVersion: _lastState ? _lastState.manifestVersion : 0,
        presence: _lastState ? _lastState.presence : {},
        error: _errorSt,
        endReason: _term ? _term.endReason : null,
      };
    }

    function _side() {
      var ls = _lastState ? _lastState.leaderSide : 'left';
      var role = _member ? _member.role : _roleIntent;
      if (role === 'leader') return ls;
      if (role === 'follower') return ls === 'right' ? 'left' : 'right';
      return null;
    }

    function _effPlane() {
      return (_plane === 'webrtc-dc' && _dc.up) ? 'webrtc-dc' : 'server-ws';
    }

    function _deriveStatus() {
      if (_destroyed) return STATUS.IDLE;
      if (_term) return _term.status;
      if (_errorSt) return STATUS.ERROR;
      if (_member && !_linkOpen()) return STATUS.RECONNECTING;
      var ph = _lastState ? _lastState.phase : null;
      if (ph === 'reading' || ph === 'selecting') return STATUS.CONNECTED;
      if (ph === 'paused') return STATUS.PEER_AWAY;
      if (ph === 'loading') return STATUS.LOADING;
      if (_member || _entry || _connecting || _roleIntent) return STATUS.CONNECTING;
      return STATUS.IDLE;
    }

    function _paired() {
      var p = _lastState ? _lastState.presence : null;
      return !!p && Object.keys(p).length >= 2;
    }

    /* ── credentials (sessionStorage, tab-scoped — §3.5) ── */

    function _store() {
      if (!_storage || !_member) return;
      try {
        _storage.setItem(STORAGE_KEY, JSON.stringify({
          sessionId: _member.sessionId, memberId: _member.memberId,
          role: _member.role, resumeToken: _member.resumeToken,
        }));
      } catch (e) { /* storage is best-effort */ }
    }

    function _readCreds() {
      if (!_storage) return null;
      try {
        var raw = _storage.getItem(STORAGE_KEY);
        if (!raw) return null;
        var d = JSON.parse(raw);
        if (d && d.sessionId && d.memberId && d.resumeToken) return d;
      } catch (e) { /* malformed/absent */ }
      return null;
    }

    function _clearCreds() {
      if (!_storage) return;
      try { _storage.removeItem(STORAGE_KEY); } catch (e) {}
    }

    /* ── transport plumbing ── */

    function _adapt(raw, name) {
      if (!raw) return null;
      if ('onframe' in raw) return raw;              // already adapter-shaped
      var t = {
        name: name || 'raw', raw: raw, ready: false,
        onframe: null, onclose: null, onopen: null,
        send: function (o) { raw.send(JSON.stringify(o)); },
        close: function () { try { raw.close(); } catch (e) {} },
      };
      raw.onmessage = function (ev) {
        if (!t.onframe) return;
        var f;
        try { f = JSON.parse(ev.data); } catch (e) { return; }
        t.onframe(f);
      };
      raw.onclose = function () { if (t.onclose) t.onclose(); };
      raw.onopen = function () { t.ready = true; if (t.onopen) t.onopen(); };
      return t;
    }

    function _linkOpen() {
      return !!(_link && _link.t && _link.open && !_link.gone);
    }

    function _ctlSend(obj) {
      if (!_linkOpen()) return false;
      try { _link.t.send(obj); return true; } catch (e) { return false; }
    }

    function _dataSend(obj) {
      var t = _dataT;
      if (!t) return false;
      // never write onto a dead transport — the DC falls back to the
      // control channel so a mid-switch frame is never lost.
      if (t === _dc.t) { if (!_dc.up) return false; }
      else if (!_linkOpen()) return false;
      try {
        if (_switcher) _switcher.send(obj); else t.send(obj);
        return true;
      } catch (e) {
        if (t === _dc.t && _linkOpen()) {
          _dc.up = false;
          _syncDataT();
          try { _link.t.send(obj); return true; } catch (e2) { return false; }
        }
        return false;
      }
    }

    function _syncDataT() {
      var want = (_plane === 'webrtc-dc' && _member &&
                  _member.role === 'follower' && _dc.up && _dc.t)
        ? _dc.t : (_link ? _link.t : null);
      _dataT = want;
      if (_switcher) {
        // the Switcher is the SEND-path swap only: setTransport() clears the
        // previous adapter's handlers and forwards the new one to sw.onframe
        // — we keep inbound ownership (gen-guarded dispatch, DC relay) by
        // re-arming each live transport's handlers right after the swap.
        _switcher.setTransport(want);
        if (_link && _link.t && _link.t.__pcInbound) _link.t.__pcInbound();
        if (_dc.t && _dc.t !== (_link && _link.t) && _dc.t.__pcInbound) {
          _dc.t.__pcInbound();
        }
      }
    }

    var _reportedPlane = 'server-ws';

    /* Effective-plane notifications: STATE.dataPlane and TRANSPORT_STATE
     * both feed _setPlane, dc up/down feed _syncDataT — subscribers see
     * exactly one 'transport' event per actual send-path change. */
    function _emitPlaneIfChanged(rev) {
      var e = _effPlane();
      if (e !== _reportedPlane) {
        _reportedPlane = e;
        _event('transport', {transport: e, revision: rev});
      }
    }

    function _setPlane(plane, rev) {
      var want = plane === 'webrtc-dc' ? 'webrtc-dc' : 'server-ws';
      if (want === _plane) { _emitPlaneIfChanged(rev); return; }
      _plane = want;
      _syncDataT();
      _emitPlaneIfChanged(rev);
    }

    function _req(type, payload, over) {
      var f = {
        v: PROTOCOL_VERSION,
        requestId: _rid(),
        sessionId: _member ? _member.sessionId : null,
        workEpoch: _seen.epoch > 0 ? _seen.epoch : 0,
        baseRevision: _seen.rev > 0 ? _seen.rev : 0,
        type: type,
        payload: payload || {},
      };
      if (over) for (var k in over) f[k] = over[k];
      return f;
    }

    function _openLink() {
      _gen++;
      var gen = _gen;
      _unlater('attempt');
      if (_link && _link.t) {
        var old = _link.t;
        old.onframe = null; old.onclose = null; old.onopen = null;
        try { old.close(); } catch (e) {}
      }
      _link = {t: null, open: false, gone: false, dead: false, gen: gen};
      _disarmLiveness();               // the guarded socket is gone
      _connecting = true;
      var raw;
      try { raw = _factory(_url); }
      catch (e) { _handleLinkGone(gen); return; }
      var t = _adapt(raw, 'server-ws');
      _link.t = t;
      var fh = function (f) {
        if (gen !== _gen) return;
        _link.open = true;
        _dispatch(f, gen);
      };
      var ch = function () { _handleLinkGone(gen); };
      var oh = ('onopen' in t)
        ? function () { if (gen === _gen && _link) _link.open = true; }
        : null;
      t.__pcInbound = function () {
        t.onframe = fh; t.onclose = ch; if (oh) t.onopen = oh;
      };
      t.__pcInbound();
      _syncDataT();
      // an entry op that never reaches its ACK within the budget is retried
      // (reconnect) or rejected (entry); wedged sockets may never stall us.
      // The timeout is also capped at the resume window so the deadline
      // check always lands even when a socket hangs silently.
      var budget = _attemptTimeout;
      if (_offlineSince !== null) {
        var left = _resumeWindow - (_now() - _offlineSince);
        if (left < budget) budget = Math.max(0, left);
      }
      _attemptT = _later(function () {
        _attemptT = null;
        var l = _link;
        if (l) { l.gone = true; if (l.t) { try { l.t.close(); } catch (e) {} } }
        _handleLinkGone(gen);
      }, budget);
    }

    function _handleLinkGone(gen) {
      if (gen !== _gen || _destroyed) return;
      if (_link && _link.dead) return;               // idempotent
      if (_link) {
        _link.dead = true; _link.gone = true;
        _link.open = false; _link.t = null;
      }
      _connecting = false;
      _unlater('attempt');
      _disarmLiveness();               // nothing live left to guard
      _syncDataT();
      // requests bound to the dead link can never resolve — reject them;
      // nothing is replayed on the next link (§3.5).
      for (var rid in _pending) {
        var p = _pending[rid];
        delete _pending[rid];
        if (_entry && p === _entry) continue;
        try { p.rej(_err('ERR_DISCONNECTED', 'link lost')); } catch (e) {}
      }
      if (_entry) {
        var e = _entry;
        if (e.type === 'RESUME') {
          // keep the pending resume: it is re-sent on the next HELLO
          e.sent = false;
        } else {
          _entry = null;
          _roleIntent = _member ? _roleIntent : null;
          try { e.rej(_err('ERR_DISCONNECTED', 'link lost before ' + e.type)); }
          catch (x) {}
        }
      }
      if (_destroyed || _term || _left) { _notify(); return; }
      if (_member) {
        if (_offlineSince === null) _offlineSince = _now();
        _event('reconnecting', {attempt: _retry});
        _scheduleRetry();
      }
      _notify();
    }

    /* ── reconnect state machine ── */

    function _scheduleRetry() {
      if (_destroyed || _term || _left || !_member) return;
      if (_linkOpen()) return;
      if (_reconnectT !== null) return;
      if (_offlineSince !== null &&
          _now() - _offlineSince >= _resumeWindow) {
        _terminate('ended', END_REASON.RESUME_EXPIRED);
        return;
      }
      var i = _retry;
      _retry += 1;
      var base = _backoff.base[Math.min(i, _backoff.base.length - 1)];
      var delay = Math.min(_backoff.cap, base + _random() * _backoff.jitter);
      // never schedule past the resume window — a timer must always land at
      // the deadline so the local expiry check runs even if no socket dies.
      if (_offlineSince !== null) {
        var left = _resumeWindow - (_now() - _offlineSince);
        if (left <= 0) {
          _terminate('ended', END_REASON.RESUME_EXPIRED);
          return;
        }
        delay = Math.min(delay, left);
      }
      _reconnectT = _later(function () {
        _reconnectT = null;
        _attemptResume();
      }, delay);
    }

    function _attemptResume() {
      if (_destroyed || _term || _left || !_member) return;
      if (_linkOpen() || _connecting) return;
      if (_offlineSince !== null &&
          _now() - _offlineSince >= _resumeWindow) {
        _terminate('ended', END_REASON.RESUME_EXPIRED);
        return;
      }
      if (_entry && _entry.type === 'RESUME') {
        // A RESUME entry kept across a dead link is re-sent on the next
        // HELLO — overwriting it here would orphan the caller's promise.
        _openLink();
        _notify();
        return;
      }
      _entry = {
        type: 'RESUME',
        payload: {memberId: _member.memberId, resumeToken: _member.resumeToken},
        sessionId: _member.sessionId,
        res: function () {}, rej: function () {},
        rid: null, sent: false,
      };
      _openLink();
      _notify();
    }

    function _wakeReconnect() {
      if (!_member || _linkOpen() || _destroyed || _term || _left) return;
      _unlater('retry');
      _attemptResume();
    }

    /* ── session entry ops ── */

    function _resetSession(keepRole) {
      _term = null;
      _left = false;
      _errorSt = null;
      _endReset();
      if (!keepRole) _roleIntent = null;
      _code = null;
      _pendingMember = null;
      _approvedMemberId = null;
    }

    function _endReset() {
      _seen = {epoch: -1, rev: -1};
      _applied = {epoch: -1, rev: -1};
      _lastState = null;
      _manifest = null;
      _mchunks = null;
      _deferredState = null;
      _appliedPending = {};
      _readyFor = null;
      _retry = 0;
      _offlineSince = null;
      _plane = 'server-ws';
      _reportedPlane = 'server-ws';
      _pendingMember = null;
      _approvedMemberId = null;
    }

    function _startEntry(type, payload, sessionId) {
      return new Promise(function (res, rej) {
        if (_entry) {
          // An outstanding entry op is superseded — its caller must get a
          // rejection, not a promise that can never settle. Its requestId
          // registration goes with it (the ACK can only ever arrive on a
          // dead socket, but a stale slot lingers until the next teardown).
          if (_entry.rid) delete _pending[_entry.rid];
          try { _entry.rej(_err('ERR_STATE', 'superseded')); } catch (e) {}
          _entry = null;
        }
        _entry = {
          type: type, payload: payload, sessionId: sessionId || null,
          res: res, rej: rej, rid: null, sent: false,
        };
        _openLink();
        _notify();
      });
    }

    function _entryFrame(e) {
      var f = _req(e.type, e.payload, {sessionId: e.sessionId});
      if (e.sessionId === null) f.sessionId = null;
      return f;
    }

    /* A bound member's link is closed politely first: LEAVE reaches the
     * peer as an END (or the flushAfter handshake replays it), instead of
     * the socket vanishing into a 5-minute peer-away window. */
    function _leaveBound() {
      if (_member) { try { leave(); } catch (e) { /* best-effort */ } }
    }

    function create() {
      if (_destroyed) return Promise.reject(_err('ERR_STATE', 'destroyed'));
      _leaveBound();
      _teardownLink();
      _resetSession();
      _roleIntent = 'leader';
      return _startEntry('CREATE', {}, null);
    }

    function join(code) {
      if (_destroyed) return Promise.reject(_err('ERR_STATE', 'destroyed'));
      if (typeof code !== 'string' || !/^\d{6}$/.test(code)) {
        return Promise.reject(_err('ERR_MALFORMED', 'code must be 6 digits'));
      }
      _leaveBound();
      _teardownLink();
      _resetSession();
      _roleIntent = 'follower';
      return _startEntry('JOIN', {code: code}, null);
    }

    /* One-tap pairing: the server binds the caller as follower when a
     * fresh waiting session exists, else as a waiting leader. Resolves
     * {ambiguous, candidates} when several sessions wait — the caller
     * then picks one via match(candidate.id). Resolves {unsupported}
     * only for a pre-MATCH server (malformed reject) so the UI falls
     * back; any other rejection stays a normal error. */
    function match(pick) {
      if (_destroyed) return Promise.reject(_err('ERR_STATE', 'destroyed'));
      _leaveBound();
      _teardownLink();
      _resetSession();
      // role decided by the server — no _roleIntent yet
      return _startEntry('MATCH', pick ? {pick: pick} : {}, null);
    }

    function resume() {
      if (_destroyed) return Promise.reject(_err('ERR_STATE', 'destroyed'));
      if (_member && _linkOpen()) {
        return Promise.resolve({resumed: true, already: true});
      }
      var creds = _member || _readCreds();
      if (!creds || !creds.memberId || !creds.resumeToken || !creds.sessionId) {
        // 復帰credential破棄 — distinguishable terminal; re-pairing required.
        _terminate('ended', END_REASON.CREDENTIALS_LOST);
        return Promise.resolve({resumed: false, reason: 'credentials_lost'});
      }
      if (!_member) {
        _resetSession();
        _member = {sessionId: creds.sessionId, memberId: creds.memberId,
                   role: creds.role || null, resumeToken: creds.resumeToken};
        _roleIntent = _member.role;
      }
      if (_offlineSince === null) _offlineSince = _now();
      return _startEntry('RESUME', {
        memberId: creds.memberId, resumeToken: creds.resumeToken,
      }, creds.sessionId);
    }

    function approve() {
      if (!_member || !_pendingMember || !_linkOpen() ||
          _approvedMemberId === _pendingMember.memberId) {
        return Promise.resolve({approved: false});
      }
      _approvedMemberId = _pendingMember.memberId;
      return _track(_ctlSendTrack('APPROVE', {memberId: _pendingMember.memberId}),
                    'APPROVE');
    }

    function reject() {
      if (!_member || !_pendingMember || !_linkOpen()) {
        return Promise.resolve({rejected: false});
      }
      return _track(_ctlSendTrack('REJECT', {memberId: _pendingMember.memberId}),
                    'REJECT');
    }

    /* _ctlSendTrack builds + sends a session-bound control request and
     * registers it for its ACK so the returned promise resolves on it. */
    function _ctlSendTrack(type, payload) {
      var f = _req(type, payload);
      _ctlSend(f);
      return f;
    }

    function _track(f, type) {
      return new Promise(function (res, rej) {
        _pending[f.requestId] = {res: res, rej: rej, type: type};
      });
    }

    /* leave(): immediate resolve — the server may be unreachable; the local
     * teardown never waits on it, and no stale generation may rejoin. */
    function leave() {
      if (_destroyed) return Promise.resolve({left: false});
      var l = _link, e = _entry, flushAfter = null;
      if (_member && e && l && l.t && !l.gone) {
        /* The server binds member→link only on the entry ACK — a LEAVE sent
         * earlier is rejected NO_SESSION and the peer sits in peer-away
         * until the resume window expires. Detach this link so _terminate
         * keeps it alive, replay the entry on HELLO when it never got out,
         * and flush LEAVE once the ACK lands so the peer still gets its END.
         * The watcher arms only after _terminate — its _syncDataT() re-arm
         * would strip handlers installed beforehand. */
        var entryFrame = e.sent ? null : _entryFrame(e);
        var wantRid = e.sent ? e.rid : entryFrame.requestId;
        var leaveFrame = _req('LEAVE', {});   // sessionId read pre-teardown
        var t = l.t;
        _link = null;
        flushAfter = function () {
          var watchdog = _setT(function () {
            try { t.close(); } catch (x) {}
          }, _attemptTimeout);
          var done = function () {
            _clearT(watchdog);
            try { t.close(); } catch (x) {}
          };
          t.onopen = null;
          t.onclose = function () { _clearT(watchdog); };
          t.onframe = function (f) {
            if (!f || typeof f !== 'object') return;
            if (f.type === 'HELLO') {
              if (entryFrame) {
                try { t.send(entryFrame); } catch (x) { done(); }
              }
              return;
            }
            if (f.type === 'ACK' && f.requestId === wantRid) {
              try { t.send(leaveFrame); } catch (x) {}
              done();
              return;
            }
            if (f.type === 'REJECT' || f.type === 'END' || f.type === 'ERROR') {
              done();
            }
          };
        };
      } else if (_member && _linkOpen()) {
        _ctlSend(_req('LEAVE', {}));
      }
      _left = true;
      _terminate('ended', END_REASON.LEFT);
      if (flushAfter) flushAfter();
      return Promise.resolve({left: true});
    }

    /* ── intents -> wire frames (data plane) ── */

    var _READING_GATED = {TURN: 1, GOTO: 1, SET_OFFSET: 1, SWAP_SIDES: 1};

    function _sendIntentInternal(intent) {
      if (_destroyed || _term || _left || !_member || !_linkOpen()) return false;
      if (!intent || typeof intent !== 'object') return false;
      var phase = _lastState ? _lastState.phase : null;
      var f = null;
      switch (intent.type) {
        case 'turn':
          if (intent.delta !== 1 && intent.delta !== -1) return false;
          f = _req('TURN', {
            delta: intent.delta, crossChapter: !!intent.crossChapter,
            fromChapter: intent.fromChapter == null ? null : intent.fromChapter,
            manifestVersion: _seenMv(),
          });
          break;
        case 'goto':
          if (!intent.page || typeof intent.page.folder !== 'string' ||
              typeof intent.page.name !== 'string') return false;
          f = _req('GOTO', {page: {folder: intent.page.folder,
                                 name: intent.page.name},
                            manifestVersion: _seenMv()});
          break;
        case 'setOffset':
          if (intent.offset !== 0 && intent.offset !== 1) return false;
          f = _req('SET_OFFSET', {offset: intent.offset,
                                  manifestVersion: _seenMv()});
          break;
        case 'swapSides':
          if (intent.leaderSide !== 'right' && intent.leaderSide !== 'left') {
            return false;
          }
          f = _req('SWAP_SIDES', {leaderSide: intent.leaderSide,
                                  manifestVersion: _seenMv()});
          break;
        case 'openWork':
          if (_member.role !== 'leader' || !_paired()) return false;
          if (typeof intent.folder !== 'string') return false;
          f = _req('OPEN_WORK', {
            folder: intent.folder,
            page: intent.page || null,
            offset: intent.offset,
            manifestVersion: _seenMv(),
          });
          if (f.payload.offset === undefined) delete f.payload.offset;
          break;
        case 'browse':
          // leader browsing the work list pauses the session at 'selecting'
          if (_member.role !== 'leader' || !_paired()) return false;
          return _ctlSend(_req('SET_ACTIVITY', {state: 'selecting'}));
        default:
          return false;
      }
      if (_READING_GATED[f.type] && phase !== 'reading') return false;
      return _dataSend(f);
    }

    function sendIntent(intent) {
      return _sendIntentInternal(intent);
    }

    /* Direct SET_ACTIVITY for the viewer-side browse observer (S09): the
     * leader's sidebar open/close pauses/resumes the shared session without
     * routing through an intent. 'browse' intents already map onto the same
     * wire frame via _sendIntentInternal. */
    function setActivity(state) {
      if (state !== 'active' && state !== 'suspended' &&
          state !== 'selecting') return false;
      if (_destroyed || _term || _left || !_member || !_linkOpen()) {
        return false;
      }
      return _ctlSend(_req('SET_ACTIVITY', {state: state}));
    }

    /* Ephemeral mirrored-UI relay (browse scroll/position). Fire-and-forget
     * on the control channel — no _track, no ACK is consumed; the payload is
     * never sequenced reader state, so a dropped frame just converges on the
     * next send. Ordering is the WS link's own — no seq field. Always
     * _ctlSend (never _dataSend): frames arriving over the DC get relayed
     * through the leader with payload.origin attached, which this payload
     * must not take. */
    function sendUiSync(payload) {
      if (_destroyed || _term || _left || !_member || !_linkOpen() ||
          !_paired()) return false;
      if (!payload || payload.view !== 'browse') return false;
      var top = payload.top, vh = payload.viewH;
      if (typeof top !== 'number' || !isFinite(top) || top < 0) return false;
      // 必須フィールドはサーバと同じ範範囲で厳格拒否 — ここで通しても
      // サーバが ERR_RANGE で落とす frame を「送信済み」と誤認させない。
      if (typeof vh !== 'number' || !isFinite(vh) || vh <= 0 ||
          vh > 100000) return false;
      var p = {
        view: 'browse',
        path: (payload.path === undefined || payload.path === null)
          ? null : String(payload.path),
        top: top, viewH: vh,
      };
      if (payload.mode === 'tile' || payload.mode === 'list' ||
          payload.mode === 'card') p.mode = payload.mode;
      // scrollH は任意フィールド — 不正値なら削って送る。frame 自体を捨てると
      // 位置情報 (top/viewH) ごと失われる（受信側は scrollH なしを legacy
      // px 変換として安全に処理する）。
      var sh = payload.scrollH;
      if (typeof sh === 'number' && isFinite(sh) && sh > 0 && sh <= 1e8)
        p.scrollH = sh;
      return _ctlSend(_req('UI_SYNC', p));
    }

    function _seenMv() {
      return _lastState && _lastState.manifestVersion
        ? _lastState.manifestVersion : 0;
    }

    function swapSides() {
      var cur = _lastState ? _lastState.leaderSide : 'left';
      return _sendIntentInternal({
        type: 'swapSides',
        leaderSide: cur === 'right' ? 'left' : 'right',
      });
    }

    function changeWork() {
      return _sendIntentInternal({type: 'browse'});
    }

    /* ── inbound dispatch ── */

    function _dispatch(f, gen) {
      if (_destroyed || gen !== _gen) return;        // stale-generation guard
      if (!f || typeof f !== 'object') return;
      _lastRx = _now();
      _armLiveness();
      if (f.v !== PROTOCOL_VERSION) {
        // version非互換 — sync stops; an update notice is the remedy (§Risks)
        _terminate('error', END_REASON.INCOMPATIBLE);
        return;
      }
      switch (f.type) {
        case 'HELLO': _onHello(); return;
        case 'PING': _ctlSend({v: PROTOCOL_VERSION, type: 'PONG', seq: f.seq});
          return;
        case 'PONG': return;
        case 'ACK': _onAck(f); return;
        case 'REJECT': _onReject(f); return;
        case 'JOIN_REQUEST':
          _pendingMember = {memberId: f.memberId, name: f.name};
          _approvedMemberId = null;
          _notify(); return;
        case 'JOIN_CANCEL':
          _pendingMember = null;
          _approvedMemberId = null;
          _notify(); return;
        case 'STATE': _onState(f); return;
        case 'MANIFEST': _onManifest(f); return;
        case 'APPLIED':
          if (typeof f.revision === 'number' && f.requestId) {
            _appliedPending[f.revision] = f.requestId;
          }
          _event('applied', {requestId: f.requestId, revision: f.revision,
                             by: f.by});
          return;
        case 'END':
          _terminate('ended', END_REASON_MAP[f.reason] || 'session_lost');
          return;
        case 'ERROR':
          _errorSt = f.code || 'error';
          _event('error', {reason: _errorSt});
          _notify(); return;
        case 'SIGNAL':
          if (_sig) _sig.applySignal({kind: f.kind, data: f.data});
          return;
        case 'UI_SYNC':
          _event('ui-sync', {payload: f.payload, from: f.from});
          return;
        case 'TRANSPORT_STATE': _onPlane(f); return;
        default: return;
      }
    }

    function _onHello() {
      _connecting = false;
      if (_entry && !_entry.sent) {
        var f = _entryFrame(_entry);
        _entry.sent = true;
        _entry.rid = f.requestId;
        _pending[f.requestId] = _entry;
        _ctlSend(f);
      }
      _notify();
    }

    function _bindFromAck(d) {
      _member = _member || {};
      _member.sessionId = d.sessionId || _member.sessionId;
      _member.memberId = d.memberId || _member.memberId;
      _member.role = d.role || _member.role;
      _member.resumeToken = d.resumeToken || _member.resumeToken;
      _roleIntent = _member.role;
      if (d.code) _code = d.code;
      _readyFor = null;                              // READY must re-arm
      _store();
      _retry = 0;
      _offlineSince = null;
    }

    function _onAck(f) {
      var p = _pending[f.requestId];
      if (!p) return;
      delete _pending[f.requestId];
      _unlater('attempt');
      if (p === _entry) {
        _entry = null;
        var d = f.data || {};
        if (p.type === 'MATCH' && d.ambiguous) {
          // several waiting sessions — unbound; caller picks via join(code)
          try { p.res(d); } catch (e) {}
          _teardownLink();
          _notify();
          return;
        }
        _bindFromAck(d);
        if (p.type === 'RESUME') {
          _event('resumed', {});
          // The server re-marks a resumed member 'active'; if we suspended
          // locally before the link died, that must be re-asserted or the
          // session un-pauses for a member nobody can see.
          if (_suspended) {
            _ctlSend(_req('SET_ACTIVITY', {state: 'suspended'}));
          }
        }
        p.res(d);
      } else {
        p.res(f.data || {});
      }
      _notify();
    }

    function _onReject(f) {
      var p = _pending[f.requestId];
      if (p) delete _pending[f.requestId];
      _unlater('attempt');
      var reason = f.reason || 'malformed';
      if (p && p === _entry) {
        _entry = null;
        if (p.type === 'RESUME') {
          var end = RESUME_REJECT_END.hasOwnProperty(reason)
            ? RESUME_REJECT_END[reason] : 'session_lost';
          if (end === null) {
            // member still claimed by a live link — retry within the window
            try { p.rej(_err('ERR_REJECTED', reason)); } catch (e) {}
            _event('resume-rejected', {reason: reason});
            _scheduleRetry();
            _notify();
            return;
          }
          p.rej(_err('ERR_REJECTED', reason));
          _terminate('ended', end);
          return;
        }
        if (p.type === 'MATCH' && reason === 'malformed') {
          // pre-MATCH server — resolve so the caller falls back to the
          // classic choice. Other MATCH rejects (bad pick, already
          // bound…) are real errors and surface normally.
          try { p.res({unsupported: reason}); } catch (e) {}
          _teardownLink();
          _notify();
          return;
        }
        _roleIntent = null;
        _errorSt = reason;
        _event('rejected', {reason: reason, request: p.type});
        try { p.rej(_err('ERR_REJECTED', reason)); } catch (e) {}
        _teardownLink();   // dead-end session — don't hold the socket
        _notify();
        return;
      }
      if (p) {
        try { p.rej(_err('ERR_REJECTED', reason)); } catch (e) {}
      }
      // reject + latest snapshot (§3.4): adopt the committed state verbatim
      if (f.snapshot && typeof f.snapshot === 'object') {
        _adoptSnapshot(f.snapshot);
      }
      _event('rejected', {reason: reason, request: f.request,
                          requestId: f.requestId});
      _notify();
    }

    function _adoptSnapshot(snap) {
      var pseudo = {
        v: PROTOCOL_VERSION, type: 'STATE',
        sessionId: snap.sessionId || (_member ? _member.sessionId : null),
        revision: snap.revision, workEpoch: snap.workEpoch,
        manifestVersion: snap.manifestVersion, phase: snap.phase,
        spreadId: snap.spreadId, offset: snap.offset,
        leaderSide: snap.leaderSide,
        presence: _lastState ? _lastState.presence : {},
        dataPlane: snap.dataPlane || 'server-ws',
      };
      _onState(pseudo);
    }

    function _onState(f) {
      if (typeof f.revision !== 'number' || typeof f.workEpoch !== 'number') {
        return;
      }
      if (_member && f.sessionId && f.sessionId !== _member.sessionId) return;
      var newer = f.workEpoch > _seen.epoch ||
        (f.workEpoch === _seen.epoch && f.revision > _seen.rev);
      if (!newer) return;                            // dup / out-of-order
      _seen = {epoch: f.workEpoch, rev: f.revision};
      _lastState = f;
      _errorSt = null;
      if (f.dataPlane === 'webrtc-dc' || f.dataPlane === 'server-ws') {
        _setPlane(f.dataPlane, f.revision);
      }
      _maybeApply(f);
      _maybeStartDc();
      _notify();
    }

    function _maybeApply(f) {
      var needMv = f.manifestVersion || 0;
      var have = _manifest && _manifest.workEpoch === f.workEpoch &&
        _manifest.manifestVersion === needMv;
      if (needMv > 0 && !have) {
        _deferredState = f;                          // wait for the manifest
        return;
      }
      if (_applied.epoch === f.workEpoch && _applied.rev >= f.revision) return;
      if (f.workEpoch < _applied.epoch) return;
      _applied = {epoch: f.workEpoch, rev: f.revision};
      var ok = true;
      try {
        _applyState(f, _manifest ? _manifest.obj : null,
                    {transport: _effPlane(), role: _member ? _member.role : null});
      } catch (e) {
        ok = false;
        _errorSt = 'apply_failed';
        _event('error', {reason: 'apply_failed'});
      }
      var rid = _appliedPending[f.revision];
      if (ok && rid != null) {
        delete _appliedPending[f.revision];
        // report against the applied commit's epoch+revision, not _seen —
        // the STATE just applied may itself be the epoch bump.
        _dataSend(_req('APPLIED', {requestId: rid},
                       {baseRevision: f.revision, workEpoch: f.workEpoch}));
      }
    }

    function _onManifest(f) {
      if (f.kind !== MANIFEST_KIND) return;
      if (!Number.isInteger(f.index) || !Number.isInteger(f.count) ||
          f.count < 1 || f.index < 0 || f.index >= f.count ||
          typeof f.data !== 'string' || typeof f.digest !== 'string') {
        _failManifest('malformed manifest chunk');
        return;
      }
      var key = (f.sessionId || '') + '|' + f.workEpoch + '|' + f.manifestVersion;
      if (_manifest && _manifest.workEpoch === f.workEpoch &&
          _manifest.manifestVersion === f.manifestVersion) {
        _sendReady();   // re-delivery (e.g. after RESUME) — READY was re-armed
        return;
      }
      if (_mchunks && _mchunks.key !== key) _mchunks = null;         // superseded
      if (_manifest && (f.workEpoch < _manifest.workEpoch ||
          (f.workEpoch === _manifest.workEpoch &&
           f.manifestVersion < _manifest.manifestVersion))) return;  // stale
      if (!_mchunks) {
        _mchunks = {key: key, digest: f.digest, count: f.count,
                    parts: new Array(f.count), got: 0,
                    workEpoch: f.workEpoch, manifestVersion: f.manifestVersion};
      }
      var b = _mchunks;
      if (b.digest !== f.digest || b.count !== f.count) {
        _mchunks = null;
        _failManifest('inconsistent manifest chunks');
        return;
      }
      if (b.parts[f.index] === undefined) {
        b.parts[f.index] = f.data;
        b.got++;
      }
      if (b.got === b.count) _finishManifest(b);
    }

    function _failManifest(msg) {
      _errorSt = 'manifest_error';
      _event('error', {reason: 'manifest_error'});
      _notify();
    }

    function _finishManifest(b) {
      _mchunks = null;
      var text = b.parts.join('');
      var obj = null;
      try {
        if (text.length > MAX_MANIFEST_BYTES) {
          throw _err('ERR_SIZE', 'manifest over cap');
        }
        obj = JSON.parse(text);
        if (!obj || typeof obj !== 'object' || obj.digest !== b.digest ||
            'sha256:' + manifestDigestHex(obj) !== b.digest) {
          throw _err('ERR_MALFORMED', 'manifest digest mismatch');
        }
      } catch (e) {
        _failManifest('manifest verify failed');
        return;
      }
      _manifest = {workEpoch: b.workEpoch, manifestVersion: b.manifestVersion,
                   obj: obj};
      _event('manifest', {workEpoch: b.workEpoch,
                          manifestVersion: b.manifestVersion});
      _sendReady();
      // a deferred STATE whose manifest just landed can now be committed.
      var d = _deferredState;
      _deferredState = null;
      if (d) _maybeApply(d);
      _notify();
    }

    /* READY signals "this member holds the metadata" — sent once per
     * (workEpoch, manifestVersion), re-armed whenever the member re-binds
     * (_bindFromAck clears _readyFor), so a post-RESUME manifest
     * re-delivery re-readies the member through the server's dedup. */
    function _sendReady() {
      if (!_member || !_manifest) return;
      var key = _manifest.workEpoch + ':' + _manifest.manifestVersion;
      if (_readyFor === key) return;
      // READY rides the manifest's own epoch — the commit STATE may not
      // have arrived yet, so _seen.epoch can still be the old epoch.
      var ok = _dataSend(_req('READY',
                              {manifestVersion: _manifest.manifestVersion},
                              {workEpoch: _manifest.workEpoch}));
      // Mark only on a real send: a dropped READY that still marked
      // _readyFor would leave the member un-READYed (phase stuck at
      // 'loading') until the next manifest re-delivery.
      if (!ok) return;
      _readyFor = key;
      _event('ready', {manifestVersion: _manifest.manifestVersion,
                       workEpoch: _manifest.workEpoch});
    }

    /* ── data-channel upgrade + transport switching ── */

    function _maybeStartDc() {
      if (!_webrtc || !_member || !_paired() || _sig) return;
      if (!PT || typeof PT.createSignalingPeer !== 'function') return;
      _sig = PT.createSignalingPeer({
        initiator: _member.role === 'leader',
        pcFactory: _webrtc.pcFactory,
        send: function (sig) {
          _ctlSend(_req('SIGNAL', {kind: sig.kind, data: sig.data}));
        },
        onChannel: function (dc) { attachDataChannel(dc); },
      });
      try { _sig.start(); } catch (e) { _sig = null; }
    }

    /* Enable the webrtc-dc upgrade after client creation. The production
     * wiring passes webrtc at construction; this exists so an opted-out
     * client can adopt the upgrade mid-session (and so the S11 runner can
     * decide the plane per scenario) without changing the default path. */
    function enableWebRTC(rtcOpts) {
      if (_destroyed || _term) return false;
      _webrtc = rtcOpts || {};
      _maybeStartDc();
      return true;
    }

    /* Re-run signaling after a DC drop mid-session: the old peer's pc is
     * closed, _sig cleared, and _maybeStartDc spins a fresh offer. The
     * pair/session state is untouched — frames keep flowing on server-ws
     * until the server confirms the plane flip again. */
    function restartDataChannel() {
      if (_destroyed || _term || !_webrtc) return false;
      if (_sig && _sig.pc) { try { _sig.pc.close(); } catch (e) {} }
      _sig = null;
      _maybeStartDc();
      return true;
    }

    function attachDataChannel(raw) {
      var t = _adapt(raw, 'webrtc-dc');
      var prev = _dc.t;
      if (prev && prev !== t) {
        prev.onframe = null; prev.onclose = null;
        try { prev.close(); } catch (e) {}
      }
      _dc.t = t;
      _dc.up = false;
      var tt = t;
      var fh = function (f) { if (_dc.t === tt) _onDcFrame(f); };
      var ch = function () { if (_dc.t === tt) _onDcDown(); };
      t.__pcInbound = function () { t.onframe = fh; t.onclose = ch; };
      t.__pcInbound();
      var up = function () {
        if (_dc.t !== tt || _dc.up) return;
        _dc.up = true;
        _ctlSend(_req('TRANSPORT_STATE', {state: 'up'}));
        _event('dc', {state: 'up'});
        _syncDataT();
        _emitPlaneIfChanged();
        _notify();
      };
      if (raw && raw.readyState === 'open') up();
      else if (raw && 'onopen' in raw) {
        var prevOpen = raw.onopen;
        raw.onopen = function () {
          if (prevOpen) { try { prevOpen(); } catch (e) {} }
          up();
        };
      } else if (t !== raw && 'onopen' in t) { t.onopen = up; }
      else up();
      _notify();
    }

    function _onDcFrame(f) {
      if (_destroyed) return;
      // leader-sequenced plane (§3.4): follower-ordered requests arriving on
      // the DC are relayed verbatim + payload.origin through the leader's
      // control channel. Everything else is a normal inbound frame.
      if (f && typeof f === 'object' && _member &&
          _member.role === 'leader' && _plane === 'webrtc-dc' &&
          typeof f.requestId === 'string' && typeof f.type === 'string' &&
          f.payload && typeof f.payload === 'object') {
        var fwd = {};
        for (var k in f) fwd[k] = f[k];
        var p = {};
        for (var k2 in f.payload) p[k2] = f.payload[k2];
        p.origin = 'follower';
        fwd.payload = p;
        _ctlSend(fwd);
        return;
      }
      _dispatch(f, _gen);
    }

    function _onDcDown() {
      var had = _dc.t || _dc.up;
      _dc.t = null;
      _dc.up = false;
      if (!had) return;
      _event('dc', {state: 'down'});
      _syncDataT();                                  // send path -> ws now
      _emitPlaneIfChanged();
      if (_member && _linkOpen()) {
        _ctlSend(_req('TRANSPORT_STATE', {state: 'down'}));
      }
      _notify();
    }

    function _onPlane(f) {
      _setPlane(f.dataPlane, f.revision);
      _notify();
    }

    /* ── lifecycle events ── */

    function _onLifecycle(type) {
      if (_destroyed) return;
      switch (type) {
        case 'hidden':
        case 'pagehide':
          // §3.5: suspend is best-effort — a lost notification is recovered
          // by the heartbeat; pagehide alone never ends the session.
          if (!_suspended) {
            _suspended = true;
            if (_member && _linkOpen()) {
              _ctlSend(_req('SET_ACTIVITY', {state: 'suspended'}));
            }
          }
          break;
        case 'visible':
        case 'pageshow':
          var was = _suspended;
          _suspended = false;
          if (_member) {
            if (_linkOpen()) {
              if (was) _ctlSend(_req('SET_ACTIVITY', {state: 'active'}));
            } else {
              _wakeReconnect();
            }
          }
          break;
        case 'offline':
          // proactive close: the transport is presumed dead — reconnect
          // bookkeeping beats waiting for a TCP timeout.
          if (_member && _link && !_link.gone) {
            var l = _link;
            l.gone = true;
            if (l.t) { try { l.t.close(); } catch (e) {} }
            _handleLinkGone(l.gen);
          }
          break;
        case 'online':
          _wakeReconnect();
          break;
        default:
          break;
      }
    }

    function _attachEvents() {
      if (!_events || typeof _events.addEventListener !== 'function') return;
      LIFECYCLE_EVENTS.forEach(function (t) {
        var fn = function () { _onLifecycle(t); };
        _listeners.push([t, fn]);
        _events.addEventListener(t, fn);
      });
    }

    function _detachEvents() {
      if (!_events || typeof _events.removeEventListener !== 'function') {
        _listeners = [];
        return;
      }
      _listeners.forEach(function (pair) {
        _events.removeEventListener(pair[0], pair[1]);
      });
      _listeners = [];
    }

    /* ── teardown ── */

    function _rejectAllPending(reason) {
      for (var rid in _pending) {
        var p = _pending[rid];
        delete _pending[rid];
        try { p.rej(_err('ERR_STATE', reason)); } catch (e) {}
      }
      if (_entry) {
        var e = _entry;
        _entry = null;
        try { e.rej(_err('ERR_STATE', reason)); } catch (x) {}
      }
    }

    function _teardownLink() {
      _gen++;
      _unlater('attempt');
      _unlater('retry');
      if (_link && _link.t) {
        var t = _link.t;
        t.onframe = null; t.onclose = null; t.onopen = null;
        try { t.close(); } catch (e) {}
      }
      _link = null;
      _connecting = false;
      _disarmLiveness();
    }

    function _terminate(status, endReason) {
      if (_term && _term.endReason === endReason) { _notify(); return; }
      _term = {status: status, endReason: endReason};
      if (status === 'error' && !_errorSt) _errorSt = endReason;
      _rejectAllPending(endReason);
      _teardownLink();
      _timersClear();
      _member = null;
      _roleIntent = null;
      _code = null;
      _pendingMember = null;
      _clearCreds();
      if (_sig) { _sig = null; }
      if (_dc.t) {
        var d = _dc.t;
        d.onframe = null; d.onclose = null;
        try { d.close(); } catch (e) {}
      }
      _dc = {t: null, up: false};
      _plane = 'server-ws';
      _reportedPlane = 'server-ws';
      _syncDataT();
      _event('ended', {reason: endReason});
      _notify();
    }

    function destroy() {
      if (_destroyed) return;
      _destroyed = true;
      _rejectAllPending('destroyed');
      _teardownLink();
      _timersClear();
      _detachEvents();
      if (_dc.t) {
        var d = _dc.t;
        d.onframe = null; d.onclose = null;
        try { d.close(); } catch (e) {}
      }
      _dc = {t: null, up: false};
      _sig = null;
      _subs = [];
    }

    /* ── subscribe / public api ── */

    function subscribe(cb) {
      if (typeof cb !== 'function') {
        throw _err('ERR_MALFORMED', 'subscribe needs a function');
      }
      _subs.push(cb);
      return function unsubscribe() {
        var i = _subs.indexOf(cb);
        if (i >= 0) _subs.splice(i, 1);
      };
    }

    _attachEvents();

    var api = {
      create: create,
      join: join,
      match: match,
      approve: approve,
      reject: reject,
      resume: resume,
      leave: leave,
      sendIntent: sendIntent,
      setActivity: setActivity,
      sendUiSync: sendUiSync,
      swapSides: swapSides,
      changeWork: changeWork,
      attachDataChannel: attachDataChannel,
      enableWebRTC: enableWebRTC,
      restartDataChannel: restartDataChannel,
      subscribe: subscribe,
      destroy: destroy,
      get state() { return _public(); },
    };
    return api;
  }

  // scrollH/pred を通す sendUiSync を持つデプロイの目印。SWR で旧
  // pairClient.js が残った混在シェルを pairSync 側が検出し、双方向とも
  // legacy px 変換に倒せるようにする。index.html の watchdog 必須 marker。
  globalThis.BOOKQ_UI_SYNC_V2 = true;

  globalThis.PairClient = {
    PROTOCOL_VERSION: PROTOCOL_VERSION,
    STATUS: STATUS,
    END_REASON: END_REASON,
    createClient: createClient,
    domEventTarget: domEventTarget,
    canonicalJson: canonicalJson,
    manifestDigestHex: manifestDigestHex,
  };
})();
