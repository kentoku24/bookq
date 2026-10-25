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
 *   pull()      — GET the stored record: {ok:true, position} / {ok:true,
 *                 position:null} when empty / {ok:false} on network, 5xx or
 *                 auth failure. Callers must NOT treat ok:false as "empty"
 *                 — pushing a stale local newest then would clobber a newer
 *                 remote record.
 *   remove()    — DELETE the record (harness 全消去); also clears all local
 *                 send state and aborts the in-flight PUT so nothing queued
 *                 can resurrect the record after the delete.
 *
 * Perf contract (page-turn mashing): push() never awaits, never fetches —
 * it rewrites _pending and re-arms one timer, so the synchronous turn path
 * only pays microseconds. In-flight PUTs cap at one; a save landing mid-
 * flight queues as the next (and only) pending record. keepalive lets a
 * pagehide flush outlive the document. _lastSentKey skips re-sending a
 * position the server already acked — an unchanged save (tab hide/reopen
 * of the same page) never overwrites another device's newer record; a
 * genuinely different page turn always sends.
 *
 * Failure policy: a failed PUT requeues its record and retries on an
 * exponential backoff (4s→8s→…→60s cap) — never a hot retry loop against
 * a dead server or offline radio, and push() during backoff only swaps
 * _pending (the armed retry timer stands) so reading through an outage
 * can't re-arm each failed send down to 2s. Non-retryable 4xx (everything
 * except 408/429 — poison payloads, expired sessions) drop the record
 * instead.
 * Requeued records keep their ORIGINAL ts so a stale re-save can't
 * leapfrog a newer remote record; the server also compares ts and
 * tombstones wipes, so stale PUTs land as {"status":"stale"} harmlessly.
 *
 * Non-GET methods bypass the service worker entirely (sw.js only handles
 * GET). The GET rides the api networkFirst cache, which is a feature: an
 * offline pull still returns the last known position.
 */
(function () {
  "use strict";
  var URL = "/api/reading-position";
  var DEBOUNCE_MS = 2000;
  var RETRY_MAX_MS = 60000;
  var _timer = null;          // send timer (debounce or retry backoff)
  var _pending = null;        // latest record awaiting send (only ever one)
  var _inFlight = false;      // at most one PUT on the wire
  var _inFlightAbort = null;  // AbortController for the in-flight PUT
  var _lastSentKey = null;    // "folder|page|total" of the last acked record
  var _retryDelay = 0;        // backoff delay after a failed send (0 = clean)
  var _epoch = 0;             // remove() bumps this — in-flight results die with it

  function _key(rec) {
    return rec.folder + "|" + rec.page + "|" + rec.total;
  }

  function _armTimer(ms) {
    if (_timer) clearTimeout(_timer);
    _timer = setTimeout(_sendNow, ms);
  }

  // 408/429 are transient; every other 4xx is a definitive rejection.
  function _droppable(status) {
    return status >= 400 && status < 500 && status !== 408 && status !== 429;
  }

  function _sendNow() {
    if (_timer) { clearTimeout(_timer); _timer = null; }
    if (!_pending || _inFlight) return;
    var rec = _pending;
    if (_key(rec) === _lastSentKey) { _pending = null; return; }
    _pending = null;
    _inFlight = true;
    var gen = _epoch;
    var ac = typeof AbortController !== "undefined" ? new AbortController() : null;
    _inFlightAbort = ac;
    fetch(URL, {
      method: "PUT",
      credentials: "same-origin",
      keepalive: true,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder: rec.folder, page: rec.page, total: rec.total, ts: rec.ts }),
      signal: ac ? ac.signal : undefined,
    }).then(function (res) {
      if (res && res.ok) { _lastSentKey = _key(rec); return "sent"; }
      if (res && _droppable(res.status)) return "dropped";
      return "retry";
    }).catch(function () {
      return "retry";   // network error / aborted / offline
    }).then(function (outcome) {
      _inFlight = false;
      _inFlightAbort = null;
      if (gen !== _epoch) return;      // remove() wiped send state mid-flight
      if (outcome !== "retry") {
        _retryDelay = 0;
        if (_pending) _sendNow();      // a newer save queued during the flight
        return;
      }
      // Failed send: requeue the record (unless a newer save already
      // queued) and retry on an exponential backoff — 4s→8s→…→60s cap.
      if (!_pending) _pending = rec;
      _retryDelay = Math.min((_retryDelay || DEBOUNCE_MS) * 2, RETRY_MAX_MS);
      _armTimer(_retryDelay);
    });
  }

  function push(rec) {
    if (!rec || typeof rec.folder !== "string") return;
    if (_pending && _key(_pending) === _key(rec)) {
      // Same position re-saved while a send is already queued (a hide-event
      // re-save after a failed send, typically): keep the EARLIER ts so the
      // re-queue can't leapfrog a newer record another device wrote since.
      if (typeof rec.ts === "number" && rec.ts < _pending.ts) _pending.ts = rec.ts;
    } else {
      _pending = { folder: rec.folder, page: rec.page, total: rec.total, ts: rec.ts };
    }
    // During retry backoff a timer is already armed (or the in-flight
    // send's post-handler will arm one) — re-arming to 2s here would
    // defeat the backoff when the user keeps turning pages offline.
    if (_retryDelay > 0 && (_timer || _inFlight)) return;
    _armTimer(DEBOUNCE_MS);
  }

  function flush() { _sendNow(); }

  async function pull() {
    try {
      var res = await fetch(URL, { credentials: "same-origin", cache: "no-store" });
      if (!res || !res.ok) return { ok: false };
      var data = await res.json();
      var pos = data && data.position;
      if (!pos || typeof pos.folder !== "string" || !Number.isFinite(pos.page)) {
        return { ok: true, position: null };
      }
      return { ok: true, position: pos };
    } catch (e) { return { ok: false }; }
  }

  function remove() {
    // Drop everything that could still write back AFTER the DELETE lands:
    // the queued record, the retry timer, the dedup key and the in-flight
    // PUT (abort + epoch bump so its completion requeues nothing).
    _pending = null;
    _lastSentKey = null;
    _retryDelay = 0;
    _epoch++;
    if (_timer) { clearTimeout(_timer); _timer = null; }
    try { if (_inFlightAbort) _inFlightAbort.abort(); } catch (e) {}
    try {
      return fetch(URL, { method: "DELETE", credentials: "same-origin", keepalive: true })
        .then(function (res) { return !!(res && res.ok); })
        .catch(function () { return false; });
    } catch (e) { return Promise.resolve(false); }
  }

  // Page-hide flushes the debounced record immediately — visibilitychange is
  // the reliable signal on iOS Safari (pagehide can be skipped on tab close),
  // pagehide covers the rest. `online` retries whatever the outage queued.
  // app.js's own hide handlers additionally flush() AFTER their final save —
  // this self-registered listener only catches the nothing-to-save case.
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
      return { pending: _pending, inFlight: _inFlight, lastSentKey: _lastSentKey, retryDelay: _retryDelay };
    },
  };
})();
