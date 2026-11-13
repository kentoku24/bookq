/* bookq offline/connectivity — application-level connectivity state machine.
 *
 * docs/offline-spec.md is the contract:
 *   - three states: offline → connecting → online (initial: offline)
 *   - connecting runs a finite budget: 3 attempts, 4s probe timeout,
 *     1s→2s backoff (≈15s max), then offline; no auto-retry while offline
 *   - connect() triggers: app start, foreground return, OS `online` event,
 *     explicit retry (chip tap), and reportFail (an online request that got
 *     no response demotes online → connecting immediately)
 *   - reportSuccess (a real, non-cache-served response) cancels the rest of
 *     a connecting cycle and lands online
 *   - fetch wrapper classifies each request (spec §5): TypeError or
 *     502/503/504 → fail; ok+non-marked → success; SW-cache-marked ok →
 *     fail (the mark means the network attempt itself failed);
 *     AbortError and other statuses → neutral
 *
 * Plain global script: defines window.Connectivity. Loads right after
 * api/v1.js (BookqApi.endpoints.health) and before every fetch consumer —
 * visibility.js, facets.js, storeAdapter.js and app.js all route through
 * Connectivity.fetch so every request feeds the verdicts.
 */
(function () {
  var API_VERSION = 1;
  var MAX_ATTEMPTS = 3;
  var PROBE_TIMEOUT_MS = 4000;
  // Inter-attempt gaps — worst case 4+1+4+2+4 ≈ 15s total budget.
  var BACKOFF_MS = [1000, 2000];
  var SWCACHE_HEADER = "X-Bookq-SWCache";
  var UNREACHABLE_STATUS = { 502: 1, 503: 1, 504: 1 };

  var _state = "offline";        // "offline" | "connecting" | "online"
  var _connecting = null;        // active cycle promise — connect() coalesces
  var _attempt = 0;              // current attempt index (debug surface)
  var _gen = 0;                  // cycle generation — stale .then must not touch state

  function _el() { return document.getElementById("offline-status"); }

  function _setState(next) {
    var changed = _state !== next;
    _state = next;
    _render();
    if (changed) {
      try {
        window.dispatchEvent(new CustomEvent("bookq:connectivity-change", { detail: { state: next } }));
      } catch (e) {
        try { window.dispatchEvent(new Event("bookq:connectivity-change")); } catch (e2) {}
      }
    }
  }

  function _render() {
    var el = _el();
    if (!el) return;
    el.textContent = _state === "online" ? "●オンライン"
      : _state === "connecting" ? "◌接続中…" : "○オフライン";
    el.classList.toggle("offline", _state === "offline");
    el.classList.toggle("connecting", _state === "connecting");
    try {
      el.title = _state === "offline" ? "タップで再試行"
        : _state === "connecting" ? "サーバに接続しています" : "";
    } catch (e) {}
  }

  // health probe — network-only by wire markers (SW bypasses cache for
  // online_check / X-Online-Check); resolves to a boolean, never throws.
  function _probe(deps) {
    var f = (deps && deps.fetch) || fetch;
    var ctl = (typeof AbortController === "function") ? new AbortController() : null;
    var timer = ctl ? setTimeout(function () { try { ctl.abort(); } catch (e) {} }, PROBE_TIMEOUT_MS) : null;
    var url;
    try {
      url = globalThis.BookqApi.endpoints.health + "?online_check=1&_t=" + Date.now();
    } catch (e) {
      if (timer) clearTimeout(timer);
      return Promise.resolve(false);
    }
    return f(url, {
      method: "GET", cache: "no-store", credentials: "same-origin",
      headers: { "X-Online-Check": "1" },
      signal: ctl ? ctl.signal : undefined,
    }).then(function (r) {
      if (timer) clearTimeout(timer);
      return !!(r && r.ok);
    }).catch(function () {
      if (timer) clearTimeout(timer);
      return false;
    });
  }

  function _sleep(ms) { return new Promise(function (res) { setTimeout(res, ms); }); }

  async function _runAttempts(deps) {
    for (_attempt = 0; _attempt < MAX_ATTEMPTS; _attempt++) {
      // reportSuccess already landed online mid-cycle — stop probing.
      if (_state !== "connecting") return true;
      if (await _probe(deps)) return true;
      if (_attempt < MAX_ATTEMPTS - 1) await _sleep(BACKOFF_MS[_attempt]);
    }
    return false;
  }

  // Start one connection cycle. Re-entry while connecting returns the same
  // cycle (coalesced); offline→connecting is the only real transition in.
  function connect(deps) {
    if (_state === "connecting" && _connecting) return _connecting;
    _setState("connecting");
    // Generation guard: a late-resolving older cycle must not clear a newer
    // cycle's _connecting handle or demote its live 'connecting' state
    // (reportSuccess→online→reportFail→connect() interleaves otherwise).
    var g = ++_gen;
    _connecting = _runAttempts(deps).then(function (ok) {
      if (g !== _gen) return _state === "online";
      _connecting = null;
      // A reportSuccess may have landed 'online' already — don't demote it.
      if (ok) _setState("online");
      else if (_state === "connecting") _setState("offline");
      return _state === "online";
    });
    return _connecting;
  }

  // Explicit user retry — identical to connect() (spec §3).
  function retry(deps) { return connect(deps); }

  // An online request got no response: demote to connecting immediately;
  // the cycle decides online vs offline (spec §2). Offline stays offline —
  // no auto-retry; connecting ignores extra failures (the cycle owns them).
  function reportFail() {
    if (_state !== "online") return;
    connect();
  }

  // A real (non-SW-cache) response proves reachability — even mid-cycle.
  function reportSuccess() {
    if (_state !== "online") _setState("online");
  }

  // Fetch wrapper every app-level request should ride. Reports verdicts per
  // spec §5; resolves/rejects exactly like fetch.
  function cfetch(input, init) {
    return fetch(input, init).then(function (res) {
      try {
        if (res && res.ok) {
          // The SW mark means the network leg itself failed and a cache
          // answered — evidence of unreachability, not of being online.
          if (res.headers && res.headers.get(SWCACHE_HEADER)) reportFail();
          else reportSuccess();
        } else if (res && UNREACHABLE_STATUS[res.status]) {
          reportFail();
        }
      } catch (e) {}
      return res;
    }, function (err) {
      try {
        // AbortError is an intentional abort (download stop, superseded
        // load, our own timeouts handled by callers) — never connectivity.
        if (!(err && err.name === "AbortError")) reportFail();
      } catch (e) {}
      throw err;
    });
  }

  // Compat/test entry point (was updateOnlineStatus): one silent probe when
  // online, a full cycle otherwise. Resolves to "are we online now".
  function revalidate(deps) {
    if (_state !== "online") return connect(deps).then(function () { return _state === "online"; });
    return _probe(deps).then(function (ok) {
      if (!ok) reportFail();
      return ok;
    });
  }

  function _bindChip() {
    var el = _el();
    if (!el || el.__connBound) return;
    el.__connBound = true;
    el.addEventListener("click", function (e) {
      // online keeps the anchor default (harness link); offline retries,
      // connecting is inert while the cycle runs.
      if (_state === "online") return;
      e.preventDefault();
      if (_state === "offline") retry();
    });
    _render();
  }

  if (typeof window !== "undefined") {
    // OS-level hints: `online` while not online starts a cycle; `offline`
    // demotes only an online state (a failed probe decides offline for sure).
    window.addEventListener("online", function () { revalidate(); });
    window.addEventListener("offline", function () { reportFail(); });
    // Foreground return — spec's 復帰 trigger. While online a single silent
    // probe revalidates without flashing 接続中; failure demotes normally.
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", function () {
        if (document.visibilityState === "visible") revalidate();
      });
    }
    window.addEventListener("pageshow", function (e) { if (e && e.persisted) revalidate(); });
    if (typeof document !== "undefined") {
      if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", _bindChip, { once: true });
      } else {
        _bindChip();
      }
    }
  }

  window.Connectivity = {
    apiVersion: API_VERSION,
    get state() { return _state; },
    isOnline: function () { return _state === "online"; },
    isOffline: function () { return _state === "offline"; },
    connect: connect,
    retry: retry,
    revalidate: revalidate,
    reportFail: reportFail,
    reportSuccess: reportSuccess,
    fetch: cfetch,
    _debug: function () {
      return { state: _state, attempt: _attempt, connecting: !!_connecting };
    },
  };
})();
