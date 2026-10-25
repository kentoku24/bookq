/* ProgressSync — server transport for the single latest reading position.
 *
 * The "続きから" pill's newest ReadingProgress entry is mirrored server-side
 * (one record per user) so a fresh browser or a different device resumes
 * where the last read left off. This module owns ONLY the wire:
 *
 *   push(rec)   — {folder,page,total,ts}: queue a trailing-edge PUT, 2s
 *                 debounce so a page-turn burst sends once, at the end.
 *   flush()     — send the queued record NOW (visibilitychange/pagehide —
 *                 the 2s timer would otherwise die with the page).
 *   pull()      — GET the stored record (null on absent/error/offline).
 *   remove()    — DELETE the record (harness 全消去).
 *
 * Perf contract (page-turn mashing): push() never awaits, never fetches —
 * it rewrites _pending and re-arms one timer, so the synchronous turn path
 * only pays microseconds. In-flight PUTs cap at one; a save landing mid-
 * flight queues as the next (and only) pending record. keepalive lets a
 * pagehide flush outlive the document. _lastSentKey skips a consecutive
 * identical record (flip to p.10, back to p.9, reopen p.10 → 2 sends, not
 * 3), and is only updated on res.ok so a failed send stays resendable —
 * failures re-queue _pending and the `online` listener retries them.
 *
 * Non-GET methods bypass the service worker entirely (sw.js only handles
 * GET). The GET rides the api networkFirst cache, which is a feature: an
 * offline pull still returns the last known position.
 */
(function () {
  "use strict";
  var URL = "/api/reading-position";
  var DEBOUNCE_MS = 2000;
  var _timer = null;
  var _pending = null;        // latest record awaiting send (only ever one)
  var _inFlight = false;      // at most one PUT on the wire
  var _lastSentKey = null;    // "folder|page|total" of the last acked record
  var _seq = 0;               // send generation — a superseded send never rewrites _pending

  function _key(rec) {
    return rec.folder + "|" + rec.page + "|" + rec.total;
  }

  function _sendNow() {
    if (_timer) { clearTimeout(_timer); _timer = null; }
    if (!_pending || _inFlight) return;
    var rec = _pending;
    if (_key(rec) === _lastSentKey) { _pending = null; return; }
    _pending = null;
    _inFlight = true;
    var gen = ++_seq;
    fetch(URL, {
      method: "PUT",
      credentials: "same-origin",
      keepalive: true,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder: rec.folder, page: rec.page, total: rec.total, ts: rec.ts }),
    }).then(function (res) {
      if (res && res.ok) _lastSentKey = _key(rec);
      else if (!_pending) _pending = rec;   // 4xx/5xx: retryable via online/next save
    }).catch(function () {
      if (!_pending) _pending = rec;        // offline: keep for the online flush
    }).finally(function () {
      _inFlight = false;
      if (_pending && gen === _seq) _sendNow();  // a newer save waited — send it
      else if (_pending) _arm();                 // (defensive; unreachable today)
    });
  }

  function _arm() {
    if (_timer) return;
    _timer = setTimeout(_sendNow, DEBOUNCE_MS);
  }

  function push(rec) {
    if (!rec || typeof rec.folder !== "string") return;
    _pending = { folder: rec.folder, page: rec.page, total: rec.total, ts: rec.ts };
    if (_timer) clearTimeout(_timer);
    _timer = setTimeout(_sendNow, DEBOUNCE_MS);
  }

  function flush() { _sendNow(); }

  async function pull() {
    try {
      var res = await fetch(URL, { credentials: "same-origin", cache: "no-store" });
      if (!res.ok) return null;
      var data = await res.json();
      var pos = data && data.position;
      if (!pos || typeof pos.folder !== "string" || !Number.isFinite(pos.page)) return null;
      return pos;
    } catch (e) { return null; }
  }

  function remove() {
    try {
      return fetch(URL, { method: "DELETE", credentials: "same-origin", keepalive: true })
        .then(function (res) { return !!(res && res.ok); })
        .catch(function () { return false; });
    } catch (e) { return Promise.resolve(false); }
  }

  // Page-hide flushes the debounced record immediately — visibilitychange is
  // the reliable signal on iOS Safari (pagehide can be skipped on tab close),
  // pagehide covers the rest. `online` retries whatever the outage queued.
  if (typeof document !== "undefined") {
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden") flush();
    });
  }
  if (typeof window !== "undefined") {
    window.addEventListener("pagehide", flush);
    window.addEventListener("online", flush);
  }

  globalThis.ProgressSync = {
    push: push,
    flush: flush,
    pull: pull,
    remove: remove,
    _debug: function () {
      return { pending: _pending, inFlight: _inFlight, lastSentKey: _lastSentKey };
    },
  };
})();
