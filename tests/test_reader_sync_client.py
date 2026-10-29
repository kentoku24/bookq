"""S07 — dual-reader browser sync client + reconnect state machine (#82).

Deterministic browser-side tests for ``app/static/viewer/pairClient.js``:
the REAL production script is evaluated in a page (``add_script_tag`` —
never a re-implementation) and driven by injected fakes — mock sockets with
a send log, a fake clock/timer set, fake storage, a fake lifecycle-event
target, and spy apply/event/status callbacks. The module must never touch
the DOM; every collaborator is injected.

完了条件 coverage (mock socket send log + status callback列 asserts):
- only the newest (workEpoch, revision) STATE is applied — dup / out-of-order /
  old-connection-generation frames never grow the apply count
- disconnect -> RESUME -> latest manifest/state requested + applied; unsent
  and unconfirmed intents never reappear in the post-resume send log
- hidden/pagehide/pageshow/BFCache/online/offline keep open sockets <= 1
  and listener registrations flat
- resume_expired / server-restart / discarded-credential / version-incompat
  produce mutually distinguishable enum values in state notifications
- leave() during disconnect resolves without a server answer and no stale
  generation can rejoin afterwards
- READY/APPLIED notifications; sendIntent drops while
  disconnected/loading/selecting
- the transport factory swaps fake-WS/fake-DC with identical behavior
- DC up/down -> transport switch notifications; frames sent mid-switch are
  neither duplicated nor lost
- fake clock drives the 0.5->1->2->5s (+jitter, cap 5s) backoff and
  destroy() leaves zero residual timers
"""
import json
import os
import sys
from pathlib import Path

import pytest

os.environ["BOOKQ_TEST_MODE"] = "true"

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.reader_sync import catalog, model, protocol  # noqa: E402

PAIR_CLIENT_JS = REPO_ROOT / "app" / "static" / "viewer" / "pairClient.js"
PAIR_TRANSPORT_JS = REPO_ROOT / "app" / "static" / "viewer" / "pairTransport.js"


@pytest.fixture
def any_browser(request):
    if os.environ.get("BROWSER") == "webkit":
        return request.getfixturevalue("webkit_browser")
    return request.getfixturevalue("browser")


@pytest.fixture
def pc_page(any_browser):
    context = any_browser.new_context()
    pg = context.new_page()
    pg.goto("about:blank")
    pg.add_script_tag(path=str(PAIR_TRANSPORT_JS))
    pg.add_script_tag(path=str(PAIR_CLIENT_JS))
    pg.evaluate(HARNESS_JS)
    yield pg
    context.close()


# ── deterministic rig (installed into the page once) ─────────────────────

HARNESS_JS = r"""
window.__mkRig = function () {
  var rig = { sockets: [], applied: [], evlog: [], statuses: [],
              nowVal: 1000 * 1000, randomVal: 0 };
  var timers = [], tid = 0;
  rig.clock = {
    now: function () { return rig.nowVal; },
    setTimeout: function (fn, ms) {
      var id = ++tid;
      timers.push({id: id, due: rig.nowVal + ms, fn: fn});
      return id;
    },
    clearTimeout: function (id) {
      for (var i = timers.length - 1; i >= 0; i--) {
        if (timers[i].id === id) timers.splice(i, 1);
      }
    },
    pending: function () { return timers.length; },
    dues: function () {
      return timers.map(function (t) { return t.due - rig.nowVal; }).sort(
        function (a, b) { return a - b; });
    },
    advance: function (ms) {
      var end = rig.nowVal + ms;
      for (;;) {
        var best = null;
        for (var i = 0; i < timers.length; i++) {
          var t = timers[i];
          if (t.due <= end && (best === null || t.due < best.due ||
              (t.due === best.due && t.id < best.id))) best = t;
        }
        if (best === null) break;
        rig.nowVal = best.due;
        timers.splice(timers.indexOf(best), 1);
        best.fn();
      }
      rig.nowVal = end;
    },
  };
  var map = {};
  rig.storage = {
    getItem: function (k) { return Object.prototype.hasOwnProperty.call(map, k) ? map[k] : null; },
    setItem: function (k, v) { map[k] = String(v); },
    removeItem: function (k) { delete map[k]; },
    _map: map,
  };
  var lis = {};
  rig.evTarget = {
    addEventListener: function (t, f) { (lis[t] = lis[t] || []).push(f); },
    removeEventListener: function (t, f) {
      if (lis[t]) lis[t] = lis[t].filter(function (g) { return g !== f; });
    },
    fire: function (t) {
      (lis[t] || []).slice().forEach(function (f) { f({type: t}); });
    },
    count: function (t) { return (lis[t] || []).length; },
    total: function () {
      return Object.keys(lis).reduce(function (a, k) { return a + lis[k].length; }, 0);
    },
  };
  rig.openSockets = function () {
    return rig.sockets.filter(function (s) { return s.open && !s.closed; }).length;
  };
  rig.maxOpen = 0;
  // Adapter-shaped fake (send(obj) / onframe / onclose / onopen keys exist).
  rig.factory = function (url) {
    var s = {
      url: url, sent: [], open: false, closed: false,
      onframe: null, onclose: null, onopen: null,
      send: function (o) { this.sent.push(JSON.parse(JSON.stringify(o))); },
      close: function () {
        if (this.closed) return;
        this.closed = true; this.open = false;
        var f = this.onclose; if (f) f();
      },
      fireOpen: function () {
        this.open = true; rig._bump(); if (this.onopen) this.onopen();
      },
      fire: function (f) {
        if (this.closed) return;
        this.open = true; rig._bump();
        if (this.onframe) this.onframe(JSON.parse(JSON.stringify(f)));
      },
      fireClose: function () { this.close(); },
    };
    rig.sockets.push(s);
    return s;
  };
  rig._bump = function () {
    rig.maxOpen = Math.max(rig.maxOpen, rig.openSockets());
  };
  // Raw-channel-shaped fake (send(string) / onmessage / onopen / onclose /
  // readyState — RTCDataChannel / WebSocket surface the client must wrap).
  rig.rawFactory = function (url) {
    var rec = {sent: [], closed: false};
    var raw = {
      url: url, readyState: 'connecting',
      onmessage: null, onclose: null, onopen: null,
      send: function (str) { rec.sent.push(JSON.parse(str)); },
      close: function () {
        if (rec.closed) return;
        rec.closed = true; raw.readyState = 'closed';
        if (raw.onclose) raw.onclose();
      },
      fireOpen: function () {
        raw.readyState = 'open'; if (raw.onopen) raw.onopen();
      },
      fire: function (f) {
        if (rec.closed) return;
        raw.readyState = 'open';
        if (raw.onmessage) raw.onmessage({data: JSON.stringify(f)});
      },
      fireClose: function () { raw.close(); },
      _rec: rec,
    };
    rig.sockets.push({
      get open() { return raw.readyState === 'open' && !rec.closed; },
      get closed() { return rec.closed; },
      sent: rec.sent, raw: raw,
      fireOpen: raw.fireOpen, fire: raw.fire, fireClose: raw.fireClose,
      close: raw.close,
    });
    return raw;
  };
  rig.dcChannel = function () {
    var rec = {sent: [], closed: false};
    var dc = {
      label: 'sync', readyState: 'connecting',
      onmessage: null, onopen: null, onclose: null,
      send: function (str) { rec.sent.push(JSON.parse(str)); },
      close: function () {
        if (rec.closed) return;
        rec.closed = true; dc.readyState = 'closed';
        if (dc.onclose) dc.onclose();
      },
      fireOpen: function () { dc.readyState = 'open'; if (dc.onopen) dc.onopen(); },
      fire: function (f) {
        if (rec.closed) return;
        if (dc.onmessage) dc.onmessage({data: JSON.stringify(f)});
      },
      fireClose: function () { dc.close(); },
      _rec: rec,
    };
    return dc;
  };
  rig.mkClient = function (extra) {
    var o = {
      url: 'ws://test/ws/reader-sync',
      transportFactory: rig.factory,
      storage: rig.storage,
      events: rig.evTarget,
      now: rig.clock.now,
      setTimeout: rig.clock.setTimeout,
      clearTimeout: rig.clock.clearTimeout,
      random: function () { return rig.randomVal; },
      applyState: function (snap, man) {
        rig.applied.push({rev: snap.revision, epoch: snap.workEpoch,
                          mv: snap.manifestVersion, spread: snap.spreadId,
                          phase: snap.phase,
                          manifest: man ? (man.folder || true) : null});
      },
      onEvent: function (e) {
        rig.evlog.push(e.type + (e.reason ? ':' + e.reason : '') +
                       (e.transport ? '@' + e.transport : ''));
      },
    };
    if (extra) for (var k in extra) o[k] = extra[k];
    var c = window.PairClient.createClient(o);
    c.subscribe(function (s) {
      rig.statuses.push(s.status + (s.endReason ? '/' + s.endReason : ''));
    });
    rig.client = c;
    return c;
  };
  // ── frame helpers ────────────────────────────────────────────────────
  rig.st = function (sid, rev, over) {
    return Object.assign({
      v: 1, type: 'STATE', sessionId: sid, revision: rev, workEpoch: 0,
      manifestVersion: 0, phase: 'waiting', spreadId: null, offset: 0,
      leaderSide: 'right', presence: {}, dataPlane: 'server-ws',
    }, over || {});
  };
  rig.ack = function (reqFrame, data) {
    return {v: 1, type: 'ACK', requestId: reqFrame.requestId,
            request: reqFrame.type, sessionId: reqFrame.sessionId || null,
            data: data || {}};
  };
  rig.rej = function (reqFrame, reason, snap) {
    var f = {v: 1, type: 'REJECT', requestId: reqFrame.requestId,
             request: reqFrame.type, reason: reason,
             sessionId: reqFrame.sessionId || null};
    if (snap) f.snapshot = snap;
    return f;
  };
  rig.presence = function (n) {
    var p = {mL: {role: 'leader', status: 'active', name: 'l', ready: false}};
    if (n >= 2) p.mF = {role: 'follower', status: 'active', name: 'f', ready: false};
    return p;
  };
  // Drive a full follower-side pairing: JOIN -> ACK -> paired/reading STATE.
  rig.pairFollower = async function (c, sid) {
    sid = sid || 'S1';
    var pr = c.join('654321');
    var s = rig.sockets[rig.sockets.length - 1];
    s.fire({v: 1, type: 'HELLO', link: 'l2'});
    var req = s.sent[s.sent.length - 1];
    s.fire(rig.ack(req, {sessionId: sid, memberId: 'mF', role: 'follower',
                         resumeToken: 'tokF'}));
    await pr;
    s.fire(rig.st(sid, 2, {phase: 'selecting', presence: rig.presence(2)}));
    return s;
  };
  // Drive a full leader-side pairing: CREATE -> ACK -> waiting STATE ->
  // JOIN_REQUEST -> APPROVE -> paired STATE. Returns the socket.
  rig.pairLeader = async function (c, sid) {
    sid = sid || 'S1';
    var pr = c.create();
    var s = rig.sockets[rig.sockets.length - 1];
    s.fire({v: 1, type: 'HELLO', link: 'l1'});
    var req = s.sent[s.sent.length - 1];
    s.fire(rig.ack(req, {sessionId: sid, code: '654321', memberId: 'mL',
                         role: 'leader', resumeToken: 'tok', expiresInSec: 300}));
    await pr;
    s.fire(rig.st(sid, 1, {presence: rig.presence(1)}));
    s.fire({v: 1, type: 'JOIN_REQUEST', sessionId: sid, memberId: 'mF', name: 'f'});
    var ap = c.approve();
    s.fire(rig.ack(s.sent[s.sent.length - 1], {}));
    await ap;
    s.fire(rig.st(sid, 2, {phase: 'selecting', presence: rig.presence(2)}));
    return s;
  };
  return rig;
};
"""


def _manifest(work="simple_manga", names=("1.png", "2.png", "3.png", "10.png", "11.png"),
              offset=0):
    """A real-shape manifest: content keys + server-verified digest."""
    pages = [{"folder": work, "name": n} for n in names]
    chapters = [{"name": work, "folder": work, "pages": pages}]
    plan = model.build_spread_plan(
        [{"folder": work, "pages": pages}], offset)
    core = {
        "v": protocol.PROTOCOL_VERSION,
        "folder": work,
        "offset": offset,
        "chapters": chapters,
        "pageRefs": list(pages),
        "spreadTable": [model.serialize_spread(s) for s in plan],
    }
    core["digest"] = catalog.manifest_digest(core)
    core["initial"] = {"page": pages[0], "spreadId": core["spreadTable"][0]["id"]}
    return core


def _manifest_frames(manifest, sid, epoch, mv):
    """MANIFEST wire frames exactly like SessionRegistry._send_manifest."""
    frames = []
    for chunk in catalog.split_manifest(manifest):
        f = dict(chunk)
        f.update({"type": "MANIFEST", "sessionId": sid,
                  "workEpoch": epoch, "manifestVersion": mv})
        frames.append(f)
    return frames


def test_pair_client_file_and_surface(pc_page):
    assert PAIR_CLIENT_JS.exists()
    api = pc_page.evaluate(
        """() => {
          const rig = window.__mkRig();
          const c = rig.mkClient();
          const keys = {};
          for (const k of ['create', 'join', 'match', 'approve', 'reject',
                           'resume', 'leave', 'sendIntent', 'swapSides',
                           'subscribe', 'destroy', 'attachDataChannel',
                           'enableWebRTC', 'restartDataChannel',
                           'changeWork', 'setActivity']) keys[k] = typeof c[k];
          c.destroy();
          return {keys, hasState: typeof c.state === 'object',
                  statusEnum: window.PairClient.STATUS,
                  reasonEnum: window.PairClient.END_REASON,
                  timers: rig.clock.pending()};
        }"""
    )
    for k, v in api["keys"].items():
        assert v == "function", f"missing client.{k}"
    assert api["hasState"]
    # every PairUI-consumed status exists in the enum
    for s in ("idle", "connecting", "connected", "reconnecting",
              "peer-away", "loading", "error", "ended"):
        assert api["statusEnum"].get(s.upper().replace("-", "_")) == s
    for r in ("resume_expired", "session_lost", "credentials_lost",
              "incompatible"):
        assert r in api["reasonEnum"].values()
    assert api["timers"] == 0


def test_only_newest_state_is_applied(pc_page):
    """重複・逆順・旧世代のonmessageで適用callbackが増えない."""
    res = pc_page.evaluate(
        """async () => {
          const rig = window.__mkRig();
          const c = rig.mkClient();
          const s = await rig.pairLeader(c);
          const n0 = rig.applied.length;          // r1 waiting + r2 selecting
          s.fire(rig.st('S1', 2, {phase: 'selecting', presence: rig.presence(2)})); // dup
          s.fire(rig.st('S1', 4, {phase: 'reading', spreadId: 'c0:s1', presence: rig.presence(2)}));
          s.fire(rig.st('S1', 3, {phase: 'selecting', presence: rig.presence(2)})); // out-of-order
          s.fire(rig.st('S1', 5, {workEpoch: 1, manifestVersion: 1, phase: 'loading',
                                spreadId: 'c0:s1', presence: rig.presence(2)}));   // newer epoch, no manifest yet -> deferred
          const nMid = rig.applied.length;
          // stale-connection-generation frame: pretend an old socket is still
          // talking by replaying an older-generation handler is impossible —
          // instead verify a lower epoch after a newer epoch drops.
          s.fire(rig.st('S1', 9, {workEpoch: 0, phase: 'reading', spreadId: 'c0:s0',
                                  presence: rig.presence(2)}));
          return {n0, nMid, applied: rig.applied,
                  deferredRev: rig.applied.length && rig.applied[rig.applied.length-1].rev};
        }"""
    )
    # r1 + r2 applied during pairing, dup r2 dropped, r4 applied, r3 dropped,
    # epoch-1/mv-1 state deferred (manifest unseen), epoch-0 r9 dropped.
    assert res["n0"] == 2
    assert res["nMid"] == res["n0"] + 1            # only r4 applied
    assert [a["rev"] for a in res["applied"]] == [1, 2, 4]


def test_old_connection_generation_frames_dropped(pc_page):
    """Reconnect後、旧socketのonmessage/oncloseは一切適用されない."""
    res = pc_page.evaluate(
        """async () => {
          const rig = window.__mkRig();
          const c = rig.mkClient();
          const s1 = await rig.pairLeader(c);
          s1.fire(rig.st('S1', 3, {phase: 'reading', spreadId: 'c0:s0',
                                 presence: rig.presence(2)}));
          const n = rig.applied.length;
          s1.fireClose();                              // -> reconnecting
          rig.clock.advance(500);                      // backoff step 0
          const s2 = rig.sockets[1];
          s2.fire({v: 1, type: 'HELLO'});
          const resumeReq = s2.sent[s2.sent.length - 1];
          // the OLD socket now emits a STATE with a huge revision — stale gen
          s1.fire(rig.st('S1', 99, {phase: 'reading', spreadId: 'c0:s2',
                                    presence: rig.presence(2)}));
          s2.fire(rig.ack(resumeReq, {sessionId: 'S1', memberId: 'mL',
                                      role: 'leader', resumeToken: 'tok'}));
          s2.fire(rig.st('S1', 6, {phase: 'reading', spreadId: 'c0:s1',
                                   presence: rig.presence(2)}));
          return {applied: rig.applied, n,
                  s2types: s2.sent.map(f => f.type)};
        }"""
    )
    assert res["applied"][res["n"] - 1]["rev"] == 3
    assert res["applied"][-1]["rev"] == 6          # rev99 on the dead gen dropped
    assert res["s2types"] == ["RESUME"]


def test_disconnect_resume_resyncs_without_replay(pc_page):
    """切断→再接続: RESUME→最新manifest/state要求・適用。未送信/未確定intent
    が復帰後の送信logに現れない。"""
    res = pc_page.evaluate(
        """async (manifestFrames) => {
          const rig = window.__mkRig();
          const c = rig.mkClient();
          const s1 = await rig.pairLeader(c);
          // committed manifest epoch1/mv1 + reading state
          for (const f of manifestFrames) s1.fire(f);
          s1.fire(rig.st('S1', 5, {workEpoch: 1, manifestVersion: 1,
                                   phase: 'reading', spreadId: 'c0:s0',
                                   presence: rig.presence(2)}));
          const readyCount = s1.sent.filter(f => f.type === 'READY').length;
          // an intent is SENT but never committed before the link dies
          const sentOk = c.sendIntent({type: 'turn', delta: 1,
                                       crossChapter: true, fromChapter: null});
          s1.fireClose();
          // while disconnected, intents are dropped outright
          const sentDown = c.sendIntent({type: 'turn', delta: -1,
                                         crossChapter: true, fromChapter: null});
          rig.clock.advance(500);
          const s2 = rig.sockets[1];
          s2.fire({v: 1, type: 'HELLO'});
          const rtypes = s2.sent.map(f => f.type);
          s2.fire(rig.ack(s2.sent[s2.sent.length - 1],
                          {sessionId: 'S1', memberId: 'mL', role: 'leader',
                           resumeToken: 'tok'}));
          // server pushes the latest STATE then re-delivers the manifest
          s2.fire(rig.st('S1', 7, {workEpoch: 1, manifestVersion: 1,
                                   phase: 'loading', spreadId: 'c0:s0',
                                   presence: rig.presence(2)}));
          for (const f of manifestFrames) s2.fire(f);
          s2.fire(rig.st('S1', 8, {workEpoch: 1, manifestVersion: 1,
                                   phase: 'reading', spreadId: 'c0:s1',
                                   presence: rig.presence(2)}));
          return {sentOk, sentDown, rtypes, readyCount,
                  s2types: s2.sent.map(f => f.type),
                  s2ready: s2.sent.filter(f => f.type === 'READY').length,
                  applied: rig.applied,
                  statuses: rig.statuses};
        }""",
        _manifest_frames(_manifest(), "S1", 1, 1),
    )
    assert res["sentOk"] is True
    assert res["sentDown"] is False
    # post-resume send log: RESUME first, then the re-READY — never a TURN
    assert res["rtypes"] == ["RESUME"]
    assert "TURN" not in res["s2types"]
    assert res["readyCount"] == 1 and res["s2ready"] == 1
    assert res["applied"][-1]["rev"] == 8 and res["applied"][-1]["spread"] == "c0:s1"
    assert "reconnecting" in res["statuses"]
    assert res["statuses"][-1] == "connected"


def test_lifecycle_events_single_socket_stable_listeners(pc_page):
    """hidden/pagehide/pageshow/online/offline の往復で socket<=1,
    listener登録数が増えない。"""
    res = pc_page.evaluate(
        """async () => {
          const rig = window.__mkRig();
          const c = rig.mkClient();
          const s1 = await rig.pairLeader(c);
          const lis0 = rig.evTarget.total();
          const flow = [];
          const act = function (label) {
            flow.push({at: label, open: rig.openSockets(),
                       pending: rig.clock.pending(),
                       listeners: rig.evTarget.total()});
          };
          rig.evTarget.fire('hidden');  act('hidden');
          rig.evTarget.fire('pagehide'); act('pagehide');
          rig.evTarget.fire('pageshow'); act('pageshow');      // BFCache restore
          rig.evTarget.fire('offline'); act('offline');
          rig.clock.advance(400);       act('t+400');          // before first retry
          rig.evTarget.fire('online');  act('online');         // immediate attempt
          const s2 = rig.sockets[rig.sockets.length - 1];
          s2.fire({v: 1, type: 'HELLO'});
          s2.fire(rig.ack(s2.sent[s2.sent.length - 1],
                          {sessionId: 'S1', memberId: 'mL', role: 'leader',
                           resumeToken: 'tok'}));
          rig.evTarget.fire('hidden');  act('hidden2');
          rig.evTarget.fire('visible'); act('visible');
          act('end');
          const acts = s1.sent.concat(s2.sent).map(f => f.type + ':' +
            ((f.payload && f.payload.state) || ''));
          return {flow, lis0, acts, socketCount: rig.sockets.length,
                  statuses: rig.statuses, maxOpen: rig.maxOpen};
        }"""
    )
    for step in res["flow"]:
        assert step["open"] <= 1, step
        assert step["listeners"] == res["lis0"], step
    assert res["maxOpen"] <= 1
    # suspended once for hidden+pagehide, active once on pageshow, suspended
    # again on the second hidden, active on visible.
    assert res["acts"].count("SET_ACTIVITY:suspended") == 2
    assert res["acts"].count("SET_ACTIVITY:active") == 2
    # offline -> close -> one RESUME on the fresh socket after online
    assert any(t == "RESUME" for t in [a.split(":")[0] for a in res["acts"]])
    assert "reconnecting" in res["statuses"]


def test_terminal_reasons_are_distinguishable(pc_page):
    """復帰期限切れ/サーバー再起動/credential破棄/version非互換 — enum で区別."""
    res = pc_page.evaluate(
        """async () => {
          const out = {};
          const boot = async (reason) => {
            const rig = window.__mkRig();
            const c = rig.mkClient();
            const pr = c.create();
            const s = rig.sockets[0];
            s.fire({v: 1, type: 'HELLO'});
            s.fire(rig.ack(s.sent[0], {sessionId: 'S1', code: '654321',
                                       memberId: 'mL', role: 'leader',
                                       resumeToken: 'tok'}));
            await pr;
            s.fireClose();
            rig.clock.advance(500);
            const s2 = rig.sockets[1];
            s2.fire({v: 1, type: 'HELLO'});
            s2.fire(rig.rej(s2.sent[s2.sent.length - 1], reason));
            return {status: c.state.status, end: c.state.endReason,
                    ev: rig.evlog};
          };
          out.expired = await boot('resume_expired');   // 復帰期限切れ
          out.restart = await boot('bad_resume');       // server再起動でsession消失
          // credential破棄: storageからresume credentialが消えている
          {
            const rig = window.__mkRig();
            const c = rig.mkClient();
            rig.storage.setItem('bookq.readerSync.v1', JSON.stringify(
              {sessionId: 'S1', memberId: 'mL', role: 'leader',
               resumeToken: 'tok'}));
            rig.storage.removeItem('bookq.readerSync.v1');   // discarded
            const r = await c.resume();
            out.creds = {status: c.state.status, end: c.state.endReason,
                         ev: rig.evlog, r};
          }
          // version非互換
          {
            const rig = window.__mkRig();
            const c = rig.mkClient();
            const pr = c.create();
            const s = rig.sockets[0];
            s.fire({v: 2, type: 'HELLO'});              // foreign protocol
            out.incompat = {status: c.state.status, end: c.state.endReason,
                            ev: rig.evlog, sent: s.sent};
          }
          return out;
        }"""
    )
    reasons = {k: res[k]["end"] for k in ("expired", "restart", "creds", "incompat")}
    assert reasons["expired"] == "resume_expired"
    assert reasons["restart"] == "session_lost"
    assert reasons["creds"] == "credentials_lost"
    assert reasons["incompat"] == "incompatible"
    assert len(set(reasons.values())) == 4            # mutually distinguishable
    assert res["incompat"]["sent"] == []              # version gate before sends
    # expired + restart terminate as ended; creds too (nothing to rejoin with)
    assert res["expired"]["status"] == "ended"
    assert res["restart"]["status"] == "ended"
    assert res["creds"]["status"] == "ended"


def test_leave_while_disconnected_is_immediate_and_final(pc_page):
    """切断中のleave()は応答を待たず即resolve。旧世代のframe/timerで
    再参加(JOIN/RESUME送信)が起きない。"""
    res = pc_page.evaluate(
        """async () => {
          const rig = window.__mkRig();
          const c = rig.mkClient();
          const s1 = await rig.pairLeader(c);
          s1.fireClose();                                 // -> reconnecting
          assert(rig.clock.pending() === 1);
          let resolved = null;
          const p = c.leave().then(r => { resolved = r; return r; });
          // must resolve without any server answer — flush the microtask
          await Promise.resolve();
          const after = rig.sockets.length;
          rig.clock.advance(60 * 1000);                   // no retry may fire
          const late = [];
          s1.fire(rig.st('S1', 50, {phase: 'reading', presence: rig.presence(2)}));
          // even if a new socket were forged by an old timer it cannot send
          for (const s of rig.sockets)
            late.push(...s.sent.map(f => f.type));
          return {resolved, after, pending: rig.clock.pending(),
                  late, statuses: rig.statuses,
                  end: c.state.endReason, status: c.state.status};
          function assert(x) { if (!x) throw new Error('assert'); }
        }"""
    )
    assert res["resolved"] == {"left": True}
    assert res["pending"] == 0                     # every timer cancelled
    assert res["status"] == "ended" and res["end"] == "left"
    assert "RESUME" not in res["late"] and "JOIN" not in res["late"]
    assert res["statuses"][-1] == "ended/left"


def test_leave_during_inflight_resume_flushes_after_ack(pc_page):
    """RESUME ACK 前の leave() は即 resolve するが、ACK で link が member へ
    bind された直後に LEAVE を wire へ流す。ACK 前に送るとサーバは
    NO_SESSION で捨てるため、相手が resume 期間いっぱい peer-away に残る
    (reload の URL 復元が RESUME 完了前に leave する競合)。"""
    res = pc_page.evaluate(
        """async () => {
          // A: RESUME sent, ACK pending -> LEAVE deferred until the ACK.
          const rigA = window.__mkRig();
          const cA = rigA.mkClient();
          const a1 = await rigA.pairLeader(cA);
          a1.fireClose();                               // -> reconnecting
          rigA.clock.advance(500);                      // retry -> a2
          const a2 = rigA.sockets[1];
          a2.fire({v: 1, type: 'HELLO'});               // RESUME goes out
          const resumeReq = a2.sent[a2.sent.length - 1];
          let resolvedA = null;
          cA.leave().then(r => { resolvedA = r; });
          await Promise.resolve();                      // immediate resolve
          const preAck = a2.sent.map(f => f.type);
          a2.fire(rigA.ack(resumeReq, {sessionId: 'S1', memberId: 'mL',
                                       role: 'leader', resumeToken: 'tok'}));
          const postAck = a2.sent.map(f => f.type);
          rigA.clock.advance(60 * 1000);                // no retry may fire

          // B: socket still CONNECTING (pre-HELLO) -> replay RESUME on
          // HELLO, then flush LEAVE after its ACK.
          const rigB = window.__mkRig();
          const cB = rigB.mkClient();
          const b1 = await rigB.pairLeader(cB);
          b1.fireClose();
          rigB.clock.advance(500);
          const b2 = rigB.sockets[1];
          cB.leave();
          await Promise.resolve();
          const preHello = b2.sent.map(f => f.type);    // nothing yet
          b2.fire({v: 1, type: 'HELLO'});               // detached watcher replays
          const reqB = b2.sent[b2.sent.length - 1];
          b2.fire(rigB.ack(reqB, {sessionId: 'S1', memberId: 'mL',
                                  role: 'leader', resumeToken: 'tok'}));
          rigB.clock.advance(60 * 1000);
          return {resolvedA, preAck, postAck,
                  preHello, sentB: b2.sent.map(f => f.type),
                  closedA: a2.closed, closedB: b2.closed,
                  statusA: cA.state.status, endA: cA.state.endReason,
                  statusB: cB.state.status, endB: cB.state.endReason,
                  pendingA: rigA.clock.pending(), pendingB: rigB.clock.pending(),
                  socksB: rigB.sockets.length};
        }"""
    )
    # Local teardown is still immediate and final on both paths.
    assert res["resolvedA"] == {"left": True}
    assert res["statusA"] == "ended" and res["endA"] == "left"
    assert res["statusB"] == "ended" and res["endB"] == "left"
    # A: LEAVE must not precede the binding ACK — it lands right after it.
    assert "LEAVE" not in res["preAck"]
    assert res["postAck"] == res["preAck"] + ["LEAVE"]
    assert res["closedA"] is True
    # B: nothing before HELLO; RESUME replayed, then LEAVE after its ACK.
    assert res["preHello"] == []
    assert res["sentB"] == ["RESUME", "LEAVE"]
    assert res["closedB"] is True
    # Watchdog/timers drained; no stale generation may reconnect.
    assert res["pendingA"] == 0 and res["pendingB"] == 0
    assert res["socksB"] == 2


def test_ready_applied_notifications_and_intent_gating(pc_page):
    """metadata READY と APPLIED が通知され、切断/loading/selecting 中の
    sendIntent が送信logに現れない。"""
    res = pc_page.evaluate(
        """async (manifestFrames) => {
          const rig = window.__mkRig();
          const c = rig.mkClient();
          const s = await rig.pairLeader(c);
          const opens = c.sendIntent({type: 'openWork', folder: 'simple_manga',
                                      page: {folder: 'simple_manga', name: '1.png'}});
          const owSent = s.sent.filter(f => f.type === 'OPEN_WORK').length;
          // manifest lands (epoch1/mv1), then the commit STATE
          for (const f of manifestFrames) s.fire(f);
          const readyIdx = s.sent.map(f => f.type).indexOf('READY');
          s.fire(rig.st('S1', 5, {workEpoch: 1, manifestVersion: 1,
                                  phase: 'reading', spreadId: 'c0:s0',
                                  presence: rig.presence(2)}));
          // a committed op: server APPLIED broadcast + STATE at rev6
          s.fire({v: 1, type: 'APPLIED', sessionId: 'S1', requestId: 'srv-r1',
                  revision: 6, by: 'follower'});
          const pre = s.sent.length;
          s.fire(rig.st('S1', 6, {workEpoch: 1, manifestVersion: 1,
                                  phase: 'reading', spreadId: 'c0:s1',
                                  presence: rig.presence(2)}));
          const appliedReport = s.sent.slice(pre).filter(f => f.type === 'APPLIED');
          // gating: TURN while reading goes out…
          const inReading = c.sendIntent({type: 'turn', delta: 1,
                                          crossChapter: true, fromChapter: null});
          // …but not while loading / selecting / disconnected
          s.fire(rig.st('S1', 7, {workEpoch: 1, manifestVersion: 1,
                                  phase: 'loading', spreadId: 'c0:s1',
                                  presence: rig.presence(2)}));
          const inLoading = c.sendIntent({type: 'turn', delta: 1,
                                          crossChapter: true, fromChapter: null});
          s.fire(rig.st('S1', 8, {workEpoch: 1, manifestVersion: 1,
                                  phase: 'selecting', spreadId: 'c0:s1',
                                  presence: rig.presence(2)}));
          const inSelecting = c.sendIntent({type: 'goto',
                                            page: {folder: 'simple_manga', name: '2.png'}});
          s.fireClose();
          const inDown = c.sendIntent({type: 'turn', delta: 1,
                                       crossChapter: true, fromChapter: null});
          return {opens, owSent, readyIdx, appliedReport, inReading,
                  inLoading, inSelecting, inDown,
                  turns: s.sent.filter(f => f.type === 'TURN').length,
                  gotos: s.sent.filter(f => f.type === 'GOTO').length,
                  evlog: rig.evlog};
        }""",
        _manifest_frames(_manifest(), "S1", 1, 1),
    )
    assert res["opens"] is True and res["owSent"] == 1
    assert res["readyIdx"] >= 0                       # READY went on the wire
    assert "ready" in res["evlog"]                    # and was notified
    assert res["appliedReport"] and res["appliedReport"][0]["payload"]["requestId"] == "srv-r1"
    assert "applied" in res["evlog"]
    assert res["inReading"] is True
    assert res["inLoading"] is False and res["inSelecting"] is False
    assert res["inDown"] is False
    assert res["turns"] == 1 and res["gotos"] == 0


def _run_scenario(pg, factory_name):
    """The same connect->pair->commit->disconnect->resume script against a
    fresh rig; the transport factory kind is the only variable."""
    return pg.evaluate(
        """async (factoryName) => {
          const rig = window.__mkRig();
          const c = rig.mkClient({transportFactory: rig[factoryName]});
          const s = await rig.pairLeader(c);
          s.fire(rig.st('S1', 3, {phase: 'reading', spreadId: 'c0:s0',
                                  presence: rig.presence(2)}));
          c.sendIntent({type: 'turn', delta: 1, crossChapter: true,
                        fromChapter: null});
          s.fire(rig.st('S1', 4, {phase: 'reading', spreadId: 'c0:s1',
                                  presence: rig.presence(2)}));
          s.fireClose();
          rig.clock.advance(500);
          const s2 = rig.sockets[1];
          s2.fire({v: 1, type: 'HELLO'});
          s2.fire(rig.ack(s2.sent[s2.sent.length - 1],
                          {sessionId: 'S1', memberId: 'mL', role: 'leader',
                           resumeToken: 'tok'}));
          s2.fire(rig.st('S1', 6, {phase: 'reading', spreadId: 'c0:s2',
                                   presence: rig.presence(2)}));
          return {statuses: rig.statuses, applied: rig.applied,
                  s1types: s.sent.map(f => f.type),
                  s2types: s2.sent.map(f => f.type)};
        }""",
        factory_name,
    )


def test_transport_factory_substitution_is_identical(pc_page):
    """fake WS / fake DataChannel で同一の接続・適用・復帰状態機械."""
    ws = _run_scenario(pc_page, "factory")
    dc = _run_scenario(pc_page, "rawFactory")
    assert ws["statuses"] == dc["statuses"]
    assert ws["applied"] == dc["applied"]
    assert ws["s1types"] == dc["s1types"] == ["CREATE", "APPROVE", "TURN"]
    assert ws["s2types"] == dc["s2types"] == ["RESUME"]


def test_dc_switch_notifications_and_inflight_integrity(pc_page):
    """DC確立・切断でtransport切替状態を通知。切替中のframeが重複・欠落しない。"""
    res = pc_page.evaluate(
        """async () => {
          const rig = window.__mkRig();
          const c = rig.mkClient();
          const s = await rig.pairFollower(c);        // follower sends ride the DC
          s.fire(rig.st('S1', 3, {phase: 'reading', spreadId: 'c0:s0',
                                  presence: rig.presence(2)}));
          const dc = rig.dcChannel();
          c.attachDataChannel(dc);
          dc.fireOpen();
          const up = s.sent.filter(f => f.type === 'TRANSPORT_STATE' &&
                                   f.payload.state === 'up').length;
          // server commits the plane flip at a revision boundary
          s.fire(rig.st('S1', 4, {phase: 'reading', spreadId: 'c0:s0',
                                  presence: rig.presence(2), dataPlane: 'webrtc-dc'}));
          s.fire({v: 1, type: 'TRANSPORT_STATE', sessionId: 'S1',
                  dataPlane: 'webrtc-dc', revision: 4});
          const tDc = c.state.transport;
          c.sendIntent({type: 'turn', delta: 1, crossChapter: true, fromChapter: null});   // mid-switch frame -> dc
          const dcGot = dc._rec.sent.map(f => f.type);
          dc.fireClose();
          const down = s.sent.filter(f => f.type === 'TRANSPORT_STATE' &&
                                     f.payload.state === 'down').length;
          s.fire({v: 1, type: 'TRANSPORT_STATE', sessionId: 'S1',
                  dataPlane: 'server-ws', revision: 5});
          const tWs = c.state.transport;
          c.sendIntent({type: 'turn', delta: -1, crossChapter: true, fromChapter: null});
          const wsTypes = s.sent.map(f => f.type);
          return {up, down, tDc, tWs, dcGot, wsTypes,
                  evlog: rig.evlog, statuses: rig.statuses,
                  wsTurns: wsTypes.filter(t => t === 'TURN').length};
        }"""
    )
    assert res["up"] == 1 and res["down"] == 1        # reported both ways
    assert res["tDc"] == "webrtc-dc" and res["tWs"] == "server-ws"
    # exactly one TURN reached each plane — no dup, no loss through the swap
    assert res["dcGot"] == ["TURN"]
    assert res["wsTurns"] == 1
    assert "transport@webrtc-dc" in res["evlog"]
    assert "transport@server-ws" in res["evlog"]


def test_leader_relays_dc_requests_with_origin(pc_page):
    """webrtc-dc中、leader はDC到着のfollower要求にpayload.originを付けて
    サーバーへ中継する（重複防止はrole:requestIdのdedup側）。"""
    res = pc_page.evaluate(
        """async () => {
          const rig = window.__mkRig();
          const c = rig.mkClient();
          const s = await rig.pairLeader(c);          // leader side
          s.fire(rig.st('S1', 3, {phase: 'reading', spreadId: 'c0:s0',
                                  presence: rig.presence(2)}));
          const dc = rig.dcChannel();
          c.attachDataChannel(dc);
          dc.fireOpen();
          s.fire({v: 1, type: 'TRANSPORT_STATE', sessionId: 'S1',
                  dataPlane: 'webrtc-dc', revision: 4});
          dc.fire({v: 1, requestId: 'fr1', sessionId: 'S1', workEpoch: 1,
                   baseRevision: 4, type: 'TURN',
                   payload: {delta: 1, crossChapter: true, fromChapter: null}});
          const fwd = s.sent[s.sent.length - 1];
          return {fwd, leaderSentTypes: s.sent.map(f => f.type)};
        }"""
    )
    fwd = res["fwd"]
    assert fwd["type"] == "TURN" and fwd["requestId"] == "fr1"
    assert fwd["payload"]["origin"] == "follower"
    assert fwd["payload"]["delta"] == 1


def test_backoff_schedule_jitter_cap_and_timer_cleanup(pc_page):
    """偽clockで再送間隔 0.5→1→2→5秒+jitter・上限5秒・destroy後タイマー0."""
    res = pc_page.evaluate(
        """async () => {
          const rig = window.__mkRig();
          const c = rig.mkClient();
          const s1 = await rig.pairLeader(c);
          s1.fireClose();
          const due1 = rig.clock.dues();                 // [500]
          rig.clock.advance(500);                        // attempt 1 opens
          rig.sockets[1].fireClose();
          const due2 = rig.clock.dues();                 // [1000]
          rig.clock.advance(1000);
          rig.sockets[2].fireClose();
          const due3 = rig.clock.dues();                 // [2000]
          rig.clock.advance(2000);
          rig.sockets[3].fireClose();
          const due4 = rig.clock.dues();                 // [5000]
          rig.clock.advance(5000);
          rig.sockets[4].fireClose();
          const due5 = rig.clock.dues();                 // [5000] (cap)
          // jitter: random()==0.9 -> +450ms, still capped at 5000
          rig.randomVal = 0.9;
          rig.clock.advance(5000);
          rig.sockets[5].fireClose();
          const due6 = rig.clock.dues();                 // [min(5000, 5000+450)]
          c.destroy();
          return {due1, due2, due3, due4, due5, due6,
                  sockets: rig.sockets.length,
                  pending: rig.clock.pending(),
                  listeners: rig.evTarget.total()};
        }"""
    )
    assert res["due1"] == [500]
    assert res["due2"] == [1000]
    assert res["due3"] == [2000]
    assert res["due4"] == [5000]
    assert res["due5"] == [5000]
    assert res["due6"] == [5000]                        # cap wins over jitter
    assert res["pending"] == 0                          # zero residual timers
    assert res["listeners"] == 0                        # listeners removed
    assert res["sockets"] == 6


def test_resume_window_expiry_ends_locally(pc_page):
    """5分の復帰windowを偽clockで超過 → サーバー非到達でも resume_expired で終了。"""
    res = pc_page.evaluate(
        """async () => {
          const rig = window.__mkRig();
          const c = rig.mkClient();
          const s1 = await rig.pairLeader(c);
          s1.fireClose();
          rig.clock.advance(500);                       // first attempt
          rig.sockets[1].fireClose();
          rig.clock.advance(301 * 1000);                // way past the window
          return {status: c.state.status, end: c.state.endReason,
                  pending: rig.clock.pending(), evlog: rig.evlog};
        }"""
    )
    assert res["status"] == "ended" and res["end"] == "resume_expired"
    assert res["pending"] == 0


def test_manifest_digest_verified_against_real_catalog(pc_page):
    """catalog.split_manifest の実manifestチャンクをJS側で再結合し、
    sha256(canonicalJSON) が宣言digestと一致して初めて commit/READY する。
    改ざんチャンクはERRORで落ち、READYもapplyも起きない。"""
    manifest = _manifest(work="夏 の 本")      # non-ASCII exercises ensure_ascii parity
    frames = _manifest_frames(manifest, "S1", 1, 1)
    res = pc_page.evaluate(
        """async (frames) => {
          const rig = window.__mkRig();
          const c = rig.mkClient();
          const s = await rig.pairLeader(c);
          for (const f of frames) s.fire(f);
          const okReady = s.sent.filter(x => x.type === 'READY').length;
          // commit STATE for epoch1/mv1 — applies with the verified manifest
          s.fire(rig.st('S1', 5, {workEpoch: 1, manifestVersion: 1,
                                  phase: 'reading', spreadId: 'c0:s0',
                                  presence: rig.presence(2)}));
          const okManifest = rig.applied.length &&
            rig.applied[rig.applied.length - 1].manifest;
          // tampered delivery: corrupt one chunk's data mid-flight
          const rig2 = window.__mkRig();
          const c2 = rig2.mkClient();
          const s2 = await rig2.pairLeader(c2, 'S2');
          const nBefore = rig2.applied.length;
          const bad = frames.map(f => Object.assign({}, f,
            f.index === 0 ? {data: f.data.slice(0, -1) + ' '} : {}));
          for (const f of bad) s2.fire(f);
          return {okReady, okManifest,
                  badReady: s2.sent.filter(x => x.type === 'READY').length,
                  badApplied: rig2.applied.length - nBefore,
                  badStatus: c2.state.status, badErr: c2.state.error,
                  ev1: rig.evlog, ev2: rig2.evlog};
        }""",
        frames,
    )
    assert res["okReady"] == 1
    assert res["okManifest"] == "夏 の 本"             # real manifest applied
    assert "manifest" in res["ev1"] and "ready" in res["ev1"]
    assert res["badReady"] == 0 and res["badApplied"] == 0
    assert res["badStatus"] == "error" and res["badErr"]


def test_pong_answers_server_heartbeat(pc_page):
    res = pc_page.evaluate(
        """async () => {
          const rig = window.__mkRig();
          const c = rig.mkClient();
          const s = await rig.pairLeader(c);
          s.fire({v: 1, type: 'PING', seq: 7});
          return {last: s.sent[s.sent.length - 1]};
        }"""
    )
    assert res["last"]["type"] == "PONG" and res["last"]["seq"] == 7


# ── UI_SYNC ephemeral browse-mirror relay ────────────────────────────────


def test_send_ui_sync_writes_ctl_frame(pc_page):
    """Paired member -> one UI_SYNC frame on the control channel, verbatim
    browse payload; no _pending tracking (fire-and-forget)."""
    res = pc_page.evaluate(
        """async () => {
          const rig = window.__mkRig();
          const c = rig.mkClient();
          const s = await rig.pairLeader(c);
          const pre = s.sent.length;
          const ok = c.sendUiSync({view: 'browse', path: 'group/ep',
                                   top: 120.5, viewH: 600, scrollH: 2400,
                                   mode: 'list'});
          const f = s.sent[s.sent.length - 1];
          return {ok, sentCount: s.sent.length - pre, f};
        }"""
    )
    assert res["ok"] is True and res["sentCount"] == 1
    f = res["f"]
    assert f["type"] == "UI_SYNC" and f["sessionId"] == "S1"
    assert f["payload"] == {"view": "browse", "path": "group/ep",
                            "top": 120.5, "viewH": 600, "scrollH": 2400,
                            "mode": "list"}


def test_send_ui_sync_gating_and_shape(pc_page):
    """Unpaired, bad payload shapes, and a dead link all refuse to send."""
    res = pc_page.evaluate(
        """async () => {
          const rig = window.__mkRig();
          const c = rig.mkClient();
          // not even member-bound yet
          const idle = c.sendUiSync({view: 'browse', path: null, top: 0,
                                     viewH: 100});
          const s = await rig.pairLeader(c);
          const base = s.sent.length;
          const badView = c.sendUiSync({view: 'overview', top: 0, viewH: 1});
          const badTop = c.sendUiSync({view: 'browse', top: -1, viewH: 1});
          const badH = c.sendUiSync({view: 'browse', top: 0, viewH: 0});
          const badHBig = c.sendUiSync({view: 'browse', top: 0,
                                        viewH: 100001});
          const noView = c.sendUiSync({path: 'x', top: 0, viewH: 1});
          const nan = c.sendUiSync({view: 'browse', top: NaN, viewH: 1});
          const sentAfterBad = s.sent.length - base;
          // optional fields: invalid values are dropped, the frame still
          // goes out (position data must not be lost with the bad extra)
          const badSh = c.sendUiSync({view: 'browse', top: 0, viewH: 1,
                                      scrollH: 0});
          const shDropF = s.sent[s.sent.length - 1];
          const badShT = c.sendUiSync({view: 'browse', top: 0, viewH: 1,
                                       scrollH: 'x'});
          const shDropF2 = s.sent[s.sent.length - 1];
          const badShCap = c.sendUiSync({view: 'browse', top: 0, viewH: 1,
                                         scrollH: 2e8});
          const shDropF3 = s.sent[s.sent.length - 1];
          const predDrop = c.sendUiSync({view: 'browse', top: 0, viewH: 1,
                                         pred: -3});
          const predDropF = s.sent[s.sent.length - 1];
          // minimal payload: path null, no mode
          const minOk = c.sendUiSync({view: 'browse', path: null, top: 0,
                                      viewH: 480});
          const minF = s.sent[s.sent.length - 1];
          // unpaired again: presence drops to 1 member
          s.fire(rig.st('S1', 3, {phase: 'selecting',
                                  presence: rig.presence(1)}));
          const unpaired = c.sendUiSync({view: 'browse', path: null, top: 0,
                                         viewH: 1});
          s.fire(rig.st('S1', 4, {phase: 'selecting',
                                  presence: rig.presence(2)}));
          s.fireClose();                       // link down
          const down = c.sendUiSync({view: 'browse', path: null, top: 0,
                                     viewH: 1});
          const uiTypes = s.sent.filter(f => f.type === 'UI_SYNC').length;
          return {idle, badView, badTop, badH, badHBig, noView, nan,
                  sentAfterBad,
                  badSh, shDropF, badShT, shDropF2, badShCap, shDropF3,
                  predDrop, predDropF,
                  minOk, minF, unpaired, down, uiTypes};
        }"""
    )
    assert res["idle"] is False
    for k in ("badView", "badTop", "badH", "badHBig", "noView", "nan"):
        assert res[k] is False, k
    assert res["sentAfterBad"] == 0
    # invalid optional fields are dropped, not frame-fatal
    for k in ("badSh", "badShT", "badShCap", "predDrop"):
        assert res[k] is True, k
    for k in ("shDropF", "shDropF2", "shDropF3"):
        assert "scrollH" not in res[k]["payload"], k
    assert "pred" not in res["predDropF"]["payload"]
    assert res["minOk"] is True
    assert res["minF"]["payload"] == {"view": "browse", "path": None,
                                      "top": 0, "viewH": 480}
    assert res["unpaired"] is False and res["down"] is False
    assert res["uiTypes"] == 5


def test_inbound_ui_sync_fires_ui_sync_event(pc_page):
    """A peer UI_SYNC frame surfaces as a verbatim 'ui-sync' event with the
    sender role attached; it never touches committed-state application."""
    res = pc_page.evaluate(
        """async () => {
          const rig = window.__mkRig();
          const events = [];
          const c = rig.mkClient({
            onEvent: function (e) { events.push(JSON.parse(JSON.stringify(e))); },
          });
          const s = await rig.pairLeader(c);
          const applied0 = rig.applied.length;
          s.fire({v: 1, type: 'UI_SYNC', sessionId: 'S1', from: 'follower',
                  payload: {view: 'browse', path: 'g', top: 77, viewH: 600}});
          return {events, applied0, appliedNow: rig.applied.length};
        }"""
    )
    ui = [e for e in res["events"] if e["type"] == "ui-sync"]
    assert len(ui) == 1
    assert ui[0]["from"] == "follower"
    assert ui[0]["payload"] == {"view": "browse", "path": "g", "top": 77,
                                "viewH": 600}
    assert res["appliedNow"] == res["applied0"]   # never an applyState call


def test_ui_sync_survives_dc_plane_switch(pc_page):
    """UI_SYNC stays on the control channel even after the data plane flips
    to webrtc-dc — a follower send never takes the leader-relay path."""
    res = pc_page.evaluate(
        """async () => {
          const rig = window.__mkRig();
          const c = rig.mkClient();
          const s = await rig.pairFollower(c);
          // DC comes up -> TRANSPORT_STATE up on both -> plane flips
          const dc = rig.dcChannel();
          c.attachDataChannel(dc);
          dc.fireOpen();
          s.fire(rig.st('S1', 3, {phase: 'selecting',
                                  presence: rig.presence(2),
                                  dataPlane: 'webrtc-dc'}));
          s.fire({v: 1, type: 'TRANSPORT_STATE', sessionId: 'S1',
                  dataPlane: 'webrtc-dc', revision: 3});
          const dcSent0 = dc._rec.sent.length;
          const wsSent0 = s.sent.length;
          const ok = c.sendUiSync({view: 'browse', path: 'g', top: 5,
                                   viewH: 400});
          return {ok,
                  dcUi: dc._rec.sent.filter(f => f.type === 'UI_SYNC').length,
                  wsUi: s.sent.filter(f => f.type === 'UI_SYNC').length,
                  plane: c.state.transport};
        }"""
    )
    assert res["plane"] == "webrtc-dc"
    assert res["ok"] is True
    assert res["dcUi"] == 0 and res["wsUi"] == 1


# ── real-server integration: actual WebSocket against the live app ────────


def test_pair_client_over_real_ws(server, any_browser):
    """Two REAL clients (browser WebSocket -> uvicorn server): pair, OPEN_WORK
    manifest + READY gate, committed TURN on both, then offline/online
    disconnect -> RESUME -> resync. The whole public-surface flow."""
    context = any_browser.new_context()
    pg = context.new_page()
    try:
        # opaque origins (about:blank) can't open a loopback WebSocket in
        # Chromium — the page must share the test server's origin.
        pg.goto(server)
        pg.add_script_tag(path=str(PAIR_TRANSPORT_JS))
        pg.add_script_tag(path=str(PAIR_CLIENT_JS))
        ws_url = server.replace("http://", "ws://").replace("https://", "wss://") \
            + "/ws/reader-sync"
        res = pg.evaluate(
            """async (wsUrl) => {
              const wait = (fn, ms) => new Promise((res, rej) => {
                const t0 = Date.now();
                const tick = () => {
                  let v; try { v = fn(); } catch (e) { v = false; }
                  if (v) return res(v);
                  if (Date.now() - t0 > ms) return rej(new Error('timeout'));
                  setTimeout(tick, 25);
                };
                tick();
              });
              const mk = (name) => {
                const lis = {};
                const evTarget = {
                  addEventListener: (t, f) => { (lis[t] = lis[t] || []).push(f); },
                  removeEventListener: (t, f) => {
                    if (lis[t]) lis[t] = lis[t].filter(g => g !== f);
                  },
                  fire: (t) => { (lis[t] || []).slice().forEach(f => f({type: t})); },
                };
                const store = {};
                const log = {applied: [], ev: []};
                const c = PairClient.createClient({
                  url: wsUrl,
                  events: evTarget,
                  storage: {
                    getItem: k => (k in store ? store[k] : null),
                    setItem: (k, v) => { store[k] = String(v); },
                    removeItem: k => { delete store[k]; },
                  },
                  applyState(snap, man) {
                    log.applied.push({rev: snap.revision, epoch: snap.workEpoch,
                                      phase: snap.phase, spread: snap.spreadId,
                                      hasManifest: !!man});
                  },
                  onEvent(e) { log.ev.push(e.type + (e.reason ? ':' + e.reason : '')); },
                });
                return {c, log, evTarget, name};
              };
              const L = mk('L'), F = mk('F');
              const created = await L.c.create();
              const joined = await F.c.join(created.code);
              await wait(() => L.c.state.joinRequest, 5000);
              await L.c.approve();
              await wait(() => L.c.state.phase === 'selecting' &&
                               F.c.state.phase === 'selecting', 5000);
              // leader opens the fixture work; both must manifest+READY into reading
              const ok = L.c.sendIntent({type: 'openWork', folder: 'simple_manga',
                page: {folder: 'simple_manga', name: '1.png'}});
              await wait(() => L.c.state.phase === 'reading' &&
                               F.c.state.phase === 'reading', 15000);
              const spread0 = L.c.state.spreadId;
              // follower turns — commit lands on both
              const sent = F.c.sendIntent({type: 'turn', delta: 1,
                                           crossChapter: true, fromChapter: null});
              await wait(() => L.c.state.spreadId !== spread0 &&
                               L.c.state.spreadId === F.c.state.spreadId, 5000);
              const spread1 = L.c.state.spreadId;
              // follower drops: 'offline' -> close -> reconnect -> RESUME -> resync
              F.evTarget.fire('offline');
              await wait(() => F.c.state.status === 'reconnecting', 5000);
              F.evTarget.fire('online');
              await wait(() => F.log.ev.indexOf('resumed') >= 0, 10000);
              await wait(() => F.c.state.status === 'connected', 5000);
              const revAfter = F.c.state.revision;
              const readyAgain = F.log.ev.filter(e => e === 'ready').length;
              const lStatus = L.c.state.status, fStatus = F.c.state.status;
              L.c.destroy(); F.c.destroy();
              return {created: !!created.code, joined: joined.role === 'follower',
                      ok, sent, spread0, spread1, revAfter, readyAgain,
                      lApplied: L.log.applied, fApplied: F.log.applied,
                      lEv: L.log.ev, fEv: F.log.ev,
                      lStatus, fStatus};
            }""",
            ws_url,
        )
        assert res["created"] and res["joined"]
        assert res["ok"] and res["sent"]
        assert res["spread0"] == "c0:s0" and res["spread1"] == "c0:s1"
        assert res["lApplied"] and res["fApplied"]
        assert res["lApplied"][-1]["hasManifest"] is True
        assert "resumed" in res["fEv"]                    # real RESUME landed
        assert res["readyAgain"] >= 2                     # re-READY after resume
        assert res["lStatus"] == res["fStatus"] == "connected"
    finally:
        context.close()


def test_match_sends_match_and_binds_acked_role(pc_page):
    """MATCH binds whichever role the server assigns — follower here."""
    res = pc_page.evaluate(
        """async () => {
          const rig = window.__mkRig();
          const c = rig.mkClient();
          const pr = c.match();
          const s = rig.sockets[rig.sockets.length - 1];
          s.fire({v: 1, type: 'HELLO', link: 'l1'});
          const req = s.sent[s.sent.length - 1];
          const wire = {type: req.type, sessionId: req.sessionId,
                        payload: req.payload};
          s.fire(rig.ack(req, {sessionId: 'S1', memberId: 'mF',
                               role: 'follower', resumeToken: 'tok',
                               leaderName: 'A'}));
          const data = await pr;
          s.fire(rig.st('S1', 1, {phase: 'selecting',
                                  presence: rig.presence(2)}));
          return {wire, data, status: c.state.status, role: c.state.role};
        }"""
    )
    assert res["wire"] == {"type": "MATCH", "sessionId": None, "payload": {}}
    assert res["data"]["role"] == "follower"
    assert res["role"] == "follower"
    assert res["status"] == "connected"


def test_match_ambiguous_resolves_candidates_and_stays_unbound(pc_page):
    """Several waiting sessions -> {ambiguous, candidates}, no member bind;
    a pick goes back out as MATCH{pick:token} — the human code never
    leaves the waiting leader's own screen."""
    res = pc_page.evaluate(
        """async () => {
          const rig = window.__mkRig();
          const c = rig.mkClient();
          const pr = c.match();
          const s = rig.sockets[rig.sockets.length - 1];
          s.fire({v: 1, type: 'HELLO', link: 'l1'});
          const req = s.sent[s.sent.length - 1];
          s.fire(rig.ack(req, {ambiguous: true, candidates: [
            {id: 'tok-a', leaderName: 'A', ageSec: 3},
            {id: 'tok-b', leaderName: 'B', ageSec: 7}]}));
          const data = await pr;
          const midStatus = c.state.status;
          const mpr = c.match('tok-b');
          const s2 = rig.sockets[rig.sockets.length - 1];
          s2.fire({v: 1, type: 'HELLO', link: 'l2'});
          const mreq = s2.sent[s2.sent.length - 1];
          s2.fire(rig.ack(mreq, {sessionId: 'S2', memberId: 'mF',
                                 role: 'follower', resumeToken: 't'}));
          await mpr;
          return {data, midStatus, mtype: mreq.type, pick: mreq.payload.pick,
                  role: c.state.role, socketCount: rig.sockets.length};
        }"""
    )
    assert res["data"]["ambiguous"] is True
    assert [c["id"] for c in res["data"]["candidates"]] == ["tok-a", "tok-b"]
    assert all("code" not in c for c in res["data"]["candidates"])
    assert res["midStatus"] == "idle"
    assert res["mtype"] == "MATCH" and res["pick"] == "tok-b"
    assert res["role"] == "follower"
    assert res["socketCount"] == 2          # ambiguous ack closed the first link


def test_match_reject_resolves_unsupported_for_fallback(pc_page):
    """A pre-MATCH server REJECTs the frame — the client resolves
    {unsupported} instead of surfacing a dead-end error."""
    res = pc_page.evaluate(
        """async () => {
          const rig = window.__mkRig();
          const c = rig.mkClient();
          const pr = c.match();
          const s = rig.sockets[rig.sockets.length - 1];
          s.fire({v: 1, type: 'HELLO', link: 'l1'});
          const req = s.sent[s.sent.length - 1];
          s.fire(rig.rej(req, 'malformed'));
          const data = await pr;
          return {data, status: c.state.status,
                  evlog: rig.evlog.filter(e => e.indexOf('rejected') === 0)};
        }"""
    )
    assert res["data"]["unsupported"] == "malformed"
    assert res["status"] == "idle"
    assert res["evlog"] == []


def test_match_non_malformed_reject_is_a_real_error(pc_page):
    """Only 'malformed' means a pre-MATCH server; any other MATCH reject
    (bad pick, already bound…) is a real failure — it must reject the
    promise and surface 'error', never silently fall back to choice."""
    res = pc_page.evaluate(
        """async () => {
          const rig = window.__mkRig();
          const c = rig.mkClient();
          const pr = c.match();
          const s = rig.sockets[rig.sockets.length - 1];
          s.fire({v: 1, type: 'HELLO', link: 'l1'});
          const req = s.sent[s.sent.length - 1];
          s.fire(rig.rej(req, 'bad_code'));
          let err = null;
          try { await pr; } catch (e) { err = e.code || String(e); }
          return {err, status: c.state.status, error: c.state.error,
                  evlog: rig.evlog.filter(e => e.indexOf('rejected') === 0)};
        }"""
    )
    assert res["err"] == "ERR_REJECTED"
    assert res["status"] == "error" and res["error"] == "bad_code"
    assert res["evlog"] == ["rejected:bad_code"]


# ── audit-hardening regressions (client side of #144) ────────────────


def test_inbound_silence_watchdog_recovers_blackholed_link(pc_page):
    """C3: closeすら届かないブラックホール経路は受信watchdogで検出し、
    onclose相当の再接続経路へ。inboundが続く限り誤発火しない — 発火時刻の
    _lastRx再判定でPING到着分の猶予が延びる。"""
    res = pc_page.evaluate(
        """async () => {
          const rig = window.__mkRig();
          const c = rig.mkClient({livenessTimeoutMs: 20000});
          const s1 = await rig.pairLeader(c);
          // a mid-window inbound frame pushes the fire-time recheck out —
          // silence is measured from _lastRx, not from arm time
          rig.clock.advance(15000);
          s1.fire({v: 1, type: 'PING', seq: 9});
          rig.clock.advance(15000);             // watchdog fires+re-arms here
          const alive = {open: s1.open, closed: s1.closed,
                         sockets: rig.sockets.length,
                         status: c.state.status};
          // now the path black-holes entirely: nothing inbound for 20s+
          rig.clock.advance(20001);             // watchdog kills + retry(500ms)
          const s2 = rig.sockets[rig.sockets.length - 1];
          const sameSocket = s2 === s1;
          s2.fire({v: 1, type: 'HELLO'});
          const types = s2.sent.map(f => f.type);
          s2.fire(rig.ack(s2.sent[s2.sent.length - 1],
                          {sessionId: 'S1', memberId: 'mL', role: 'leader',
                           resumeToken: 'tok'}));
          return {alive, sameSocket, s1closed: s1.closed, types,
                  sockets: rig.sockets.length,
                  statuses: rig.statuses, status: c.state.status,
                  evlog: rig.evlog};
        }"""
    )
    # while inbound frames kept arriving the watchdog never fired
    assert res["alive"] == {"open": True, "closed": False, "sockets": 1,
                            "status": "connected"}
    # after the silence deadline the old socket was closed and recovery began
    assert res["s1closed"] is True and res["sameSocket"] is False
    assert res["sockets"] == 2
    assert res["types"] == ["RESUME"]           # same path as a real onclose
    assert "reconnecting" in res["statuses"]
    assert res["status"] == "connected" and "resumed" in res["evlog"]


def test_resume_entry_survives_link_death_before_ack(pc_page):
    """H3: resume() の呼び出し promise が、RESUME 送信後〜ACK前のリンク
    死亡を跨いでも orphan されない — 保持 entry が次の HELLO で再送され、
    その ACK が元の promise を解決する。"""
    res = pc_page.evaluate(
        """async () => {
          const rig = window.__mkRig();
          rig.storage.setItem('bookq.readerSync.v1', JSON.stringify(
            {sessionId: 'S1', memberId: 'mL', role: 'leader',
             resumeToken: 'tok'}));
          const c = rig.mkClient();
          let settled = null;
          const pr = c.resume().then(r => { settled = r; return r; });
          const s1 = rig.sockets[0];
          s1.fire({v: 1, type: 'HELLO'});
          const first = s1.sent.map(f => f.type);
          s1.fireClose();                        // dies before the RESUME ACK
          rig.clock.advance(500);                // retry -> second link
          const s2 = rig.sockets[1];
          s2.fire({v: 1, type: 'HELLO'});
          const second = s2.sent.map(f => f.type);
          s2.fire(rig.ack(s2.sent[s2.sent.length - 1],
                          {sessionId: 'S1', memberId: 'mL', role: 'leader',
                           resumeToken: 'tok'}));
          await Promise.resolve(); await Promise.resolve();
          // the server then pushes the committed STATE — the real flow
          s2.fire(rig.st('S1', 3, {phase: 'reading', spreadId: 'c0:s0',
                                   presence: rig.presence(2)}));
          return {first, second, settled, sockets: rig.sockets.length,
                  evlog: rig.evlog, status: c.state.status,
                  prType: typeof pr.then};
        }"""
    )
    assert res["first"] == ["RESUME"] and res["second"] == ["RESUME"]
    # the caller's promise resolved with the ACK data — previously the
    # second _attemptResume overwrote the kept entry and orphaned it
    assert res["settled"]["sessionId"] == "S1"
    assert res["status"] == "connected" and "resumed" in res["evlog"]


def test_new_entry_op_supersedes_inflight_entry(pc_page):
    """H4: 未ACKの entry op を新しい create/join が黙殺しない — 先の
    promise は 'superseded' で明示 reject され settle する。"""
    res = pc_page.evaluate(
        """async () => {
          const rig = window.__mkRig();
          const c = rig.mkClient();
          let jErr = null;
          const jp = c.join('654321');
          jp.catch(e => { jErr = e; });
          const s1 = rig.sockets[0];
          s1.fire({v: 1, type: 'HELLO'});
          const jtypes = s1.sent.map(f => f.type);   // JOIN out, never ACKed
          const cp = c.create();                     // supersedes the JOIN
          await Promise.resolve(); await Promise.resolve();
          const s2 = rig.sockets[1];
          s2.fire({v: 1, type: 'HELLO'});
          const ctypes = s2.sent.map(f => f.type);
          s2.fire(rig.ack(s2.sent[0], {sessionId: 'S2', code: '111111',
                                     memberId: 'mL', role: 'leader',
                                     resumeToken: 'tok'}));
          await cp;
          return {jtypes, ctypes, jErrCode: jErr && jErr.code,
                  sessionId: c.state.sessionId, role: c.state.role,
                  status: c.state.status};
        }"""
    )
    assert res["jtypes"] == ["JOIN"] and res["ctypes"] == ["CREATE"]
    assert res["jErrCode"] == "ERR_STATE"       # superseded — never hangs
    # bound to the NEW session — 'connecting' until the first STATE lands
    assert res["sessionId"] == "S2" and res["role"] == "leader"
    assert res["status"] == "connecting"


def test_create_and_join_on_bound_member_leave_politely(pc_page):
    """H5: バインド済みで create/join/match を呼ぶと先に LEAVE を出す —
    ソケットの黙殺で相手を5分の paused に残さない。"""
    res = pc_page.evaluate(
        """async () => {
          const rig = window.__mkRig();
          const c = rig.mkClient();
          const s1 = await rig.pairLeader(c);
          const cp = c.create();                  // bound -> LEAVE first
          const s1last = s1.sent[s1.sent.length - 1];
          const s2 = rig.sockets[rig.sockets.length - 1];
          s2.fire({v: 1, type: 'HELLO'});
          s2.fire(rig.ack(s2.sent[0], {sessionId: 'S2', code: '111111',
                                     memberId: 'mL2', role: 'leader',
                                     resumeToken: 't2'}));
          await cp;
          const jp = c.join('999999');            // bound again -> LEAVE first
          const s2last = s2.sent[s2.sent.length - 1];
          const s3 = rig.sockets[rig.sockets.length - 1];
          s3.fire({v: 1, type: 'HELLO'});
          s3.fire(rig.ack(s3.sent[0], {sessionId: 'S3', memberId: 'mF',
                                     role: 'follower', resumeToken: 't3',
                                     leaderName: 'l'}));
          await jp;
          return {s1last, s2last, s1closed: s1.closed, s2closed: s2.closed,
                  s3type: s3.sent[0].type, s3code: s3.sent[0].payload.code,
                  sessionId: c.state.sessionId, role: c.state.role};
        }"""
    )
    assert res["s1last"]["type"] == "LEAVE"
    assert res["s1last"]["sessionId"] == "S1"
    assert res["s2last"]["type"] == "LEAVE"
    assert res["s1closed"] is True and res["s2closed"] is True
    assert res["s3type"] == "JOIN" and res["s3code"] == "999999"
    assert res["sessionId"] == "S3" and res["role"] == "follower"


def test_ready_marks_only_after_send_succeeds(pc_page):
    """H6: READY の送信が失敗した時に _readyFor をマークしない — 次の
    manifest 再配送で本当に送られるまで READY を再試行する (旧: 送信前
    マークで drop 分が永久に未 READY → 'loading' 停滞)。"""
    res = pc_page.evaluate(
        """async (manifestFrames) => {
          const rig = window.__mkRig();
          const c = rig.mkClient();
          const s = await rig.pairLeader(c);
          // one-shot send failure: the READY that follows lands nowhere
          const origSend = s.send;
          s.send = function (o) { s.send = origSend; throw new Error('boom'); };
          for (const f of manifestFrames) s.fire(f);
          const afterFail = {
            readySent: s.sent.filter(f => f.type === 'READY').length,
            readyEv: rig.evlog.filter(e => e === 'ready').length,
          };
          // manifest re-delivery (same epoch:mv — the RESUME path) re-arms it
          for (const f of manifestFrames) s.fire(f);
          return {afterFail,
                  readySent: s.sent.filter(f => f.type === 'READY').length,
                  readyEv: rig.evlog.filter(e => e === 'ready').length};
        }""",
        _manifest_frames(_manifest(), "S1", 1, 1),
    )
    assert res["afterFail"] == {"readySent": 0, "readyEv": 0}
    assert res["readySent"] == 1 and res["readyEv"] == 1


def test_suspended_reasserted_after_resume(pc_page):
    """H7: RESUME でサーバが member を 'active' に戻すため、ローカル
    suspended 中の再バインド直後に SET_ACTIVITY suspended を再 assert —
    '見えないメンバが active' によるセッション un-pause を防ぐ。"""
    res = pc_page.evaluate(
        """async () => {
          const rig = window.__mkRig();
          const c = rig.mkClient();
          const s1 = await rig.pairLeader(c);
          rig.evTarget.fire('hidden');               // locally suspended
          const s1acts = s1.sent
            .filter(f => f.type === 'SET_ACTIVITY')
            .map(f => f.payload.state);
          s1.fireClose();                            // dies while hidden
          rig.clock.advance(500);
          const s2 = rig.sockets[1];
          s2.fire({v: 1, type: 'HELLO'});
          s2.fire(rig.ack(s2.sent[s2.sent.length - 1],
                          {sessionId: 'S1', memberId: 'mL', role: 'leader',
                           resumeToken: 'tok'}));
          const s2types = s2.sent.map(f => f.type + ':' +
            ((f.payload && f.payload.state) || ''));
          rig.evTarget.fire('visible');              // now un-suspend
          const all = s2.sent.map(f => f.type + ':' +
            ((f.payload && f.payload.state) || ''));
          return {s1acts, s2types, all, evlog: rig.evlog};
        }"""
    )
    assert res["s1acts"] == ["suspended"]
    # right after the RESUME-ACK the client re-asserts its suspension —
    # previously the server's 'active' won and the pair un-paused blindly
    assert res["s2types"] == ["RESUME:", "SET_ACTIVITY:suspended"]
    assert res["all"][-1] == "SET_ACTIVITY:active"
    assert "resumed" in res["evlog"]
