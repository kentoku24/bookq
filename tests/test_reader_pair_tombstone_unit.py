"""#585 — leave-tombstone unit tests (PairClient, injected fakes).

Drives the shipped web/static/viewer/pairClient.js in an about:blank page
with a fake transport factory, fake clock/timers and an in-memory
persistStorage — the module is DOM-free by design, so every edge is
deterministic: no server, no sockets, no wall-clock waits.

Covers the sessionId-keyed tombstone map: two sessions' parked LEAVEs
coexist, a flush clears only the entry it delivered, expired entries are
garbage-collected on read, and silent flush failures retry on a bounded
budget instead of waiting for the next lifecycle event.
"""
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PAIR_CLIENT_JS = REPO_ROOT / "web" / "static" / "viewer" / "pairClient.js"
TOMB_KEY = "bookq.readerSync.leaveTombstone.v1"


@contextmanager
def _pc_page(browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    try:
        pg.goto("about:blank")
        pg.add_script_tag(path=str(PAIR_CLIENT_JS))
        yield pg
    finally:
        ctx.close()


# Shared harness: fake storage/clock/timers/transport + bind/drive helpers.
# Everything runs inside one page.evaluate so state stays in-page.
_HARNESS = r"""
async (scenario) => {
  const PC = window.PairClient;
  const K = 'bookq.readerSync.leaveTombstone.v1';

  const memStore = () => {
    const m = Object.create(null);
    return {
      getItem: k => (k in m ? m[k] : null),
      setItem: (k, v) => { m[k] = String(v); },
      removeItem: k => { delete m[k]; },
    };
  };

  const persist = memStore();
  let now = 100000;
  const timers = [];
  let seq = 0;
  const setT = (fn, ms) => { const id = ++seq; timers.push({id, fn, ms}); return id; };
  const clearT = id => {
    const i = timers.findIndex(t => t.id === id);
    if (i >= 0) timers.splice(i, 1);
  };
  const fireTimers = () => {
    const due = timers.splice(0, timers.length);
    for (const t of due) t.fn();
  };

  const transports = [];
  const factory = url => {
    const t = {
      _url: url,
      onframe: null, onclose: null, onopen: null,
      sent: [], closed: false,
      send(f) { this.sent.push(f); },
      close() { this.closed = true; },
    };
    transports.push(t);
    return t;
  };
  const lastT = () => transports[transports.length - 1];
  // unanswered open transport owned by a given client url
  const pendingFor = url =>
    transports.find(t => t._url === url && t.sent.length === 0 && !t.closed);

  const opts = name => ({
    url: 'ws://fake/' + name,
    transportFactory: factory,
    persistStorage: persist,
    storage: memStore(),
    events: null,
    now: () => now,
    setTimeout: setT,
    clearTimeout: clearT,
    random: () => 0,
    attemptTimeoutMs: 500,
    resumeWindowMs: 60000,
    backoff: {base: [10], cap: 10, jitter: 0},
  });

  const readMap = () => {
    const raw = persist.getItem(K);
    return raw ? JSON.parse(raw) : {};
  };

  // CREATE entry -> HELLO -> entry frame -> ACK binds the member.
  async function bind(name, sid, mid, role) {
    const c = PC.createClient(opts(name));
    const p = c.create();
    const t = lastT();
    t.onframe({v: 1, type: 'HELLO'});
    const entry = t.sent[t.sent.length - 1];
    t.onframe({v: 1, type: 'ACK', requestId: entry.requestId,
               data: {sessionId: sid, memberId: mid, role: role || 'leader',
                      resumeToken: 'tok-' + mid}});
    await p;
    return {c: c, t: t};
  }

  // Drive a parked flush transport through RESUME->ACK->LEAVE.
  function flushOne(t) {
    t.onframe({v: 1, type: 'HELLO'});
    const resume = t.sent[t.sent.length - 1];
    const types = () => t.sent.map(f => f.type);
    t.onframe({v: 1, type: 'ACK', requestId: resume.requestId, data: {}});
    return {resume: resume, types: types()};
  }

  const out = {now: () => now, setNow: v => { now = v; },
               persist: persist, readMap: readMap,
               transports: transports, fireTimers: fireTimers,
               opts: opts, bind: bind, flushOne: flushOne,
               pendingFor: pendingFor,
               results: {}};
  const fn = eval('(' + scenario + ')');
  await fn(out);
  return out.results;
}
"""


def _run(browser, scenario_src):
    with _pc_page(browser) as pg:
        return pg.evaluate(_HARNESS, scenario_src)


def test_two_sessions_tombstones_coexist_and_clear_is_scoped(browser):
    """The #585 core: sessionId-keyed map — a second session's parked
    LEAVE must not overwrite the first, and a delivered flush clears only
    its own entry."""
    src = r"""
    async (h) => {
      // session A binds, its link dies, it leaves -> tombstone parked
      const a = await h.bind('a', 'sess-a', 'mem-a');
      a.t.onclose();                       // link gone -> member-bound
      await a.c.leave();                   // offline leave: tombstone only
      h.results.afterA = h.readMap();

      // session B (same profile storage, different session) does the same
      h.setNow(h.now() + 1000);            // B's entry is strictly newer
      const b = await h.bind('b', 'sess-b', 'mem-b');
      b.t.onclose();
      await b.c.leave();
      h.results.afterB = h.readMap();

      // oldest entry (sess-a) flushes first on A's throwaway link
      // (B's boot/leave flush attempts also target sess-a but stay
      // unanswered — only the entry each delivers is cleared)
      const driven = h.pendingFor('ws://fake/a');
      const r = h.flushOne(driven);
      h.results.flushSent = r.types;
      h.results.resumeSid = r.resume.sessionId;
      h.results.afterFlush = h.readMap();

      // drain: the successful flush scheduled the next queued tombstone
      h.fireTimers();
      const next = h.pendingFor('ws://fake/a');
      const r2 = h.flushOne(next);
      h.results.drainSent = r2.types;
      h.results.drainSid = r2.resume.sessionId;
      h.results.final = h.readMap();
      a.c.destroy(); b.c.destroy();
    }
    """
    res = _run(browser, src)
    assert set(res["afterA"]) == {"sess-a"}
    # both sessions' tombstones coexist — the single-slot bug is gone
    assert set(res["afterB"]) == {"sess-a", "sess-b"}
    # flush delivered sess-a's LEAVE and cleared ONLY that entry
    assert res["resumeSid"] == "sess-a"
    assert res["flushSent"] == ["RESUME", "LEAVE"]
    assert set(res["afterFlush"]) == {"sess-b"}
    # the queued sess-b tombstone drains next and retires too
    assert res["drainSid"] == "sess-b"
    assert res["final"] == {}


def test_expired_tombstone_is_garbage_collected(browser):
    """Expired entries are dropped on read instead of lingering forever."""
    src = r"""
    async (h) => {
      h.persist.setItem('bookq.readerSync.leaveTombstone.v1', JSON.stringify({
        'sess-old': {memberId: 'm1', resumeToken: 't1', at: 1000},
        'sess-live': {memberId: 'm2', resumeToken: 't2', at: 99000},
      }));
      // a fresh client boot runs _maybeFlushLeave -> _tombstoneRead,
      // which GCs the expired entry even before any frame is exchanged
      const c = window.PairClient.createClient(h.opts('gc'));
      h.results.mapAfterBoot = h.readMap();
      c.destroy();
    }
    """
    res = _run(browser, src)
    # sess-old (age 99000 > 60000 window) is gone; sess-live remains and
    # became the in-flight flush target
    assert set(res["mapAfterBoot"]) == {"sess-live"}


def test_legacy_single_tombstone_still_flushes(browser):
    """Pre-#585 records ({sessionId,...} flat) normalize to a one-entry
    map so an in-flight v1 tombstone is not stranded by the upgrade."""
    src = r"""
    async (h) => {
      h.persist.setItem('bookq.readerSync.leaveTombstone.v1', JSON.stringify({
        sessionId: 'sess-leg', memberId: 'mem-leg',
        resumeToken: 'tok-leg', at: 99900,
      }));
      const c = window.PairClient.createClient(h.opts('leg'));
      const t = h.transports[h.transports.length - 1];
      const r = h.flushOne(t);
      h.results.sent = r.types;
      h.results.sid = r.resume.sessionId;
      h.results.memberId = r.resume.payload.memberId;
      h.results.mapAfter = h.readMap();
      c.destroy();
    }
    """
    res = _run(browser, src)
    assert res["sent"] == ["RESUME", "LEAVE"]
    assert res["sid"] == "sess-leg"
    assert res["memberId"] == "mem-leg"
    assert res["mapAfter"] == {}


def test_transport_failures_do_not_spend_entry_budget(browser):
    """#598: a black-holed/offline flush link is an ENVIRONMENTAL failure —
    it hits every queued entry equally, so it must not spend the entry's
    retry budget. Only resume_in_use (entry-specific) is bounded (#594).
    Transport failures retry on FLUSH_RETRY_MS until the window lapses,
    and the tombstone stays parked (never cleared by silence)."""
    src = r"""
    async (h) => {
      const a = await h.bind('a', 'sess-a', 'mem-a');
      a.t.onclose();
      await a.c.leave();   // parks tombstone + opens flush attempt #1
      const flushStart = h.transports.length - 1;  // index of flush #1
      // pump the manual timer queue: each round fires the pending
      // watchdog (attemptTimeoutMs=500) and the scheduled retry (4s)
      for (let i = 0; i < 14; i++) h.fireTimers();
      const attempts = h.transports.length - flushStart;
      h.results.attempts = attempts;
      h.results.mapKept = h.readMap();
      a.c.destroy();
    }
    """
    res = _run(browser, src)
    # Uncharged: retries continue past the old 4-attempt cap — the entry
    # is only bounded by its resume window, not by a failure counter.
    assert res["attempts"] > 4
    # silent failures never clear the tombstone and never write `fails`
    assert set(res["mapKept"]) == {"sess-a"}
    assert "fails" not in res["mapKept"]["sess-a"]


def test_offline_leave_delivers_after_reconnect(browser):
    """#598 regression: leaving while offline must not burn the retry
    budget — once the transport answers again the parked LEAVE goes out
    and the tombstone clears, inside the same resume window."""
    src = r"""
    async (h) => {
      const a = await h.bind('a', 'sess-a', 'mem-a');
      a.t.onclose();
      await a.c.leave();   // parks tombstone + flush attempt #1
      const flushStart = h.transports.length - 1;
      // "Offline": every flush transport dies on close before HELLO.
      // Far more attempts than the old budget of 4.
      for (let i = 0; i < 12; i++) {
        const t = h.transports[h.transports.length - 1];
        if (t && t._url && t.sent.length === 0 && !t.closed) t.onclose();
        h.fireTimers();
      }
      const offlineAttempts = h.transports.length - flushStart;
      // "Reconnect": the next flush transport answers HELLO -> RESUME ->
      // ACK -> LEAVE like a healthy link.
      const t = h.pendingFor('ws://fake/a');
      if (t) {
        t.onframe({v: 1, type: 'HELLO'});
        const resume = t.sent[t.sent.length - 1];
        t.onframe({v: 1, type: 'ACK', requestId: resume.requestId, data: {}});
      }
      h.fireTimers();
      const lastFlush = h.transports[h.transports.length - 1];
      h.results.offlineAttempts = offlineAttempts;
      h.results.delivered = !!lastFlush &&
        lastFlush.sent.some(f => f.type === 'LEAVE');
      h.results.mapAfter = h.readMap();
      a.c.destroy();
    }
    """
    res = _run(browser, src)
    assert res["offlineAttempts"] > 4          # survived past old budget
    assert res["delivered"] is True            # LEAVE actually sent
    assert res["mapAfter"] == {}               # tombstone cleared


def test_window_expiry_stops_retries_and_gcs_entry(browser):
    """#598 review: transport-failure retries are bounded by the resume
    window, not a counter — advancing the clock past the window must
    stop new flush transports AND garbage-collect the expired entry,
    not leave it retrying forever."""
    src = r"""
    async (h) => {
      const a = await h.bind('a', 'sess-a', 'mem-a');
      a.t.onclose();
      await a.c.leave();   // parks tombstone + flush attempt #1
      for (let i = 0; i < 4; i++) h.fireTimers();  // a few silent retries
      const before = h.transports.length;
      h.setNow(h.now() + 70000);                  // past the 60s window
      for (let i = 0; i < 6; i++) h.fireTimers();
      h.results.attemptsAfterExpiry = h.transports.length - before;
      h.results.mapAfter = h.readMap();
      a.c.destroy();
    }
    """
    res = _run(browser, src)
    # _tombstoneRead GCs the expired entry and returns null — the pending
    # retry fires once, opens nothing, and schedules nothing further.
    assert res["attemptsAfterExpiry"] == 0
    assert "sess-a" not in res["mapAfter"]


def test_exhausted_oldest_tombstone_yields_to_younger(browser):
    """#594: a persistently unflushable OLDEST entry must not starve
    younger tombstones — once its per-entry budget is spent the drain
    skips it and delivers the next session's LEAVE. The spent entry
    stays parked until its window lapses."""
    src = r"""
    async (h) => {
      // Seed the map directly: sess-a is oldest, sess-b younger. One
      // client drains the queue serially through its own flush link.
      h.persist.setItem('bookq.readerSync.leaveTombstone.v1', JSON.stringify({
        'sess-a': {memberId: 'mem-a', resumeToken: 'tok-a', at: 90000},
        'sess-b': {memberId: 'mem-b', resumeToken: 'tok-b', at: 99000},
      }));
      const c = window.PairClient.createClient(h.opts('drain'));

      const sids = [];
      // sess-a's member stays contested: every RESUME draws
      // resume_in_use. Each rejection burns one of the entry's
      // FLUSH_RETRY_MAX+1 (=4) attempts; the 5th flush must be sess-b.
      for (let i = 0; i < 12; i++) {
        const t = h.pendingFor('ws://fake/drain');
        if (!t) { h.fireTimers(); continue; }
        t.onframe({v: 1, type: 'HELLO'});
        const resume = t.sent[t.sent.length - 1];
        sids.push(resume.sessionId);
        if (resume.sessionId === 'sess-b') {
          t.onframe({v: 1, type: 'ACK', requestId: resume.requestId,
                     data: {}});
        } else {
          t.onframe({v: 1, type: 'REJECT', requestId: resume.requestId,
                     reason: 'resume_in_use'});
        }
        h.fireTimers();
        if (sids[sids.length - 1] === 'sess-b' &&
            !h.pendingFor('ws://fake/drain')) break;
      }
      h.results.sids = sids;
      h.results.finalMap = h.readMap();
      c.destroy();
    }
    """
    res = _run(browser, src)
    # sess-a burns exactly its 4-attempt budget, then sess-b is delivered
    assert res["sids"] == ["sess-a"] * 4 + ["sess-b"]
    # sess-b's entry cleared on delivery; sess-a stays parked with its
    # spent budget until the window lapses
    assert set(res["finalMap"]) == {"sess-a"}
    assert res["finalMap"]["sess-a"]["fails"] == 4


def test_peek_leave_tombstone_map_oldest_expiry_and_legacy(browser):
    """peekLeaveTombstone reports the OLDEST live map entry (what the next
    flush would deliver), skips expired/malformed entries, and still
    understands the legacy single-object record."""
    with _pc_page(browser) as pg:
        res = pg.evaluate(
            """() => {
              const store = {
                _v: undefined,
                getItem: function(k) { return this._v === undefined ? null : this._v; },
                setItem: function(k, v) { this._v = v; },
                removeItem: function() { this._v = null; },
              };
              const peek = window.PairClient.peekLeaveTombstone;
              const R = {};
              store.setItem('k', JSON.stringify({
                s1: {memberId: 'm', resumeToken: 't', at: 50},
                s2: {memberId: 'm', resumeToken: 't', at: 90},
                s3: {memberId: 'm', resumeToken: 't', at: 10},
              }));
              // window 40: s1 (age 50) and s3 (age 90) expired -> s2 only
              R.onlyLive = peek(store, () => 100, 40).sessionId;
              // default 300s window: all live -> oldest s3 wins
              R.defaultWindow = peek(store, () => 100).sessionId;
              store.setItem('k', JSON.stringify({
                sessionId: 's-leg', memberId: 'm', resumeToken: 't', at: 90}));
              R.legacy = peek(store, () => 100, 40);
              store.setItem('k', '{not json');
              R.badJson = peek(store, () => 100, 40);
              store.setItem('k', JSON.stringify({s: {memberId: 'm'}}));
              R.missingFields = peek(store, () => 100, 40);
              return R;
            }""")
    assert res["onlyLive"] == "s2"
    assert res["defaultWindow"] == "s3"
    assert res["legacy"]["sessionId"] == "s-leg"
    assert res["legacy"]["memberId"] == "m"
    assert res["badJson"] is None
    assert res["missingFields"] is None
