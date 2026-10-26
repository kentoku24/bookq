import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PAIR_SYNC_JS = REPO_ROOT / "app" / "static" / "browse" / "pairSync.js"

_SC = "document.getElementById('sidebar-scroll')"


@pytest.fixture
def any_browser(request):
    if os.environ.get("BROWSER") == "webkit":
        return request.getfixturevalue("webkit_browser")
    return request.getfixturevalue("browser")


@pytest.fixture
def pair_page(any_browser):
    context = any_browser.new_context(viewport={"width": 390, "height": 460})
    pg = context.new_page()
    pg.goto("about:blank")
    pg.evaluate(SETUP_JS)
    pg.add_script_tag(path=str(PAIR_SYNC_JS))
    yield pg
    context.close()


SETUP_JS = r"""
(function () {
  var now = {v: 1000000};
  var timers = []; var tid = 0;
  var rafQ = []; var rid = 0;

  window.__clock = {
    now: function () { return now.v; },
    advance: function (ms) {
      var end = now.v + ms;
      for (;;) {
        var best = null;
        for (var i = 0; i < timers.length; i++) {
          var t = timers[i];
          if (t.due <= end &&
              (best === null || t.due < best.due ||
               (t.due === best.due && t.id < best.id))) best = t;
        }
        if (best === null) break;
        now.v = best.due;
        timers.splice(timers.indexOf(best), 1);
        best.fn();
      }
      now.v = end;
    },
    frame: function (ms) {
      window.__clock.advance(ms || 0);
      var q = rafQ.slice(); rafQ = [];
      for (var i = 0; i < q.length; i++) q[i].cb(now.v);
    },
    frames: function (n, ms) {
      for (var i = 0; i < n; i++) window.__clock.frame(ms);
    },
    timersPending: function () { return timers.length; },
    rafPending: function () { return rafQ.length; },
  };
  Date.now = function () { return now.v; };
  try {
    if (window.performance) {
      performance.now = function () { return now.v; };
    }
  } catch (e) {}
  window.setTimeout = function (fn, ms) {
    var id = ++tid;
    timers.push({id: id, due: now.v + (ms || 0), fn: fn});
    return id;
  };
  window.clearTimeout = function (id) {
    for (var i = timers.length - 1; i >= 0; i--) {
      if (timers[i].id === id) timers.splice(i, 1);
    }
  };
  window.requestAnimationFrame = function (cb) {
    var id = ++rid; rafQ.push({id: id, cb: cb}); return id;
  };
  window.cancelAnimationFrame = function (id) {
    for (var i = rafQ.length - 1; i >= 0; i--) {
      if (rafQ[i].id === id) rafQ.splice(i, 1);
    }
  };

  var style = document.createElement('style');
  style.textContent =
    '#sidebar-scroll{height:400px;overflow-y:auto;display:block;}' +
    '#tile-grid>div{height:100px;}';
  document.head.appendChild(style);
  document.body.innerHTML =
    '<div id="viewer"></div>' +
    '<div id="sidebar"><div id="sidebar-scroll">' +
    '<div id="tile-grid"></div></div></div>';
  var tg = document.getElementById('tile-grid');
  for (var i = 0; i < 40; i++) {
    var d = document.createElement('div');
    d.textContent = 'row' + i;
    tg.appendChild(d);
  }

  window.__BOOKQ_TEST_MODE__ = 'true';
  window.searchMode = false;
  window.currentBrowsePath = null;
  window.viewMode = 'tile';
  window.scrollPositions = {};
  window.getScrollKey = function (p) { return 'k|' + (p || ''); };
  window.getNodeAt = function () { return {type: 'folder'}; };
  window.openSidebar = function () {
    document.getElementById('sidebar').classList.add('open');
  };
  window.closeSidebar = function () {
    document.getElementById('sidebar').classList.remove('open');
  };
  window.renderView = function () {};
  window.updateBrowseHeader = function () {};
  window.writeBrowseStateReplace = function () {};

  window.__uiSent = [];
  var ctlEvents = [];
  window.PairController = {
    state: {status: 'idle'},
    subscribe: function () { return function () {}; },
    onEvent: function (cb) { ctlEvents.push(cb); return function () {}; },
    sendUiSync: function (p) {
      window.__uiSent.push(JSON.parse(JSON.stringify(p)));
      return true;
    },
  };
  window.__fireCtlEvent = function (e) {
    for (var i = 0; i < ctlEvents.length; i++) ctlEvents[i](e);
  };

  window.__arm = function (role, over) {
    var st = Object.assign({
      status: 'active', sessionId: 'S', phase: 'selecting', role: role,
      presence: {A: {status: 'connected', role: 'leader'},
                 B: {status: 'connected', role: 'follower'}},
    }, over || {});
    window.PairController.state = st;
    window.__pairBrowseSync.onState(st);
    return window.__pairBrowseSync.armed;
  };
  window.__disarm = function () {
    window.PairController.state = {status: 'idle'};
    window.__pairBrowseSync.onState(window.PairController.state);
  };

  window.__fireInput = function (type) {
    var el = document.getElementById('sidebar-scroll');
    var ev;
    if (typeof PointerEvent === 'function' &&
        (type === 'pointerdown' || type === 'pointerup' ||
         type === 'pointercancel' || type === 'pointermove')) {
      ev = new PointerEvent(type, {bubbles: true, pointerId: 1,
                                   isPrimary: true, pointerType: 'touch'});
    } else {
      ev = new Event(type, {bubbles: true, cancelable: true});
    }
    el.dispatchEvent(ev);
    return true;
  };
  window.__fireKeyOn = function (el, key) {
    el.dispatchEvent(
      new KeyboardEvent('keydown', {key: key, bubbles: true}));
  };
  window.__fireBlur = function () {
    window.dispatchEvent(new Event('blur'));
  };
  var __hidden = false;
  window.__setHidden = function (v) {
    __hidden = !!v;
    try {
      Object.defineProperty(document, 'hidden',
        {configurable: true, get: function () { return __hidden; }});
    } catch (e) {}
    document.dispatchEvent(new Event('visibilitychange'));
  };
  window.__setReduce = function (v) {
    window.matchMedia = function (q) {
      return {matches: !!v && /reduce/.test(q), media: q,
              addListener: function () {}, removeListener: function () {},
              addEventListener: function () {},
              removeEventListener: function () {}};
    };
  };
  window.__rebuildGrid = function (n) {
    var g = document.getElementById('tile-grid');
    g.innerHTML = '';
    for (var i = 0; i < (n || 40); i++) {
      var d = document.createElement('div');
      d.textContent = 'row' + i;
      g.appendChild(d);
    }
  };
  window.__makeInput = function () {
    var inp = document.createElement('input');
    document.getElementById('sidebar').appendChild(inp);
    return inp;
  };
  window.__fireResize = function () {
    window.dispatchEvent(new Event('resize'));
  };
})();
"""


def _scroll(pg):
    return pg.evaluate(f"() => ({_SC}).scrollTop")


def _hook(pg):
    return pg.evaluate("() => window.__pairBrowseSync.smoothing")


def _arm_follower(pg):
    assert pg.evaluate("() => window.__arm('follower')") is True


def _rearm(pg):
    pg.evaluate("() => window.__disarm()")
    pg.wait_for_timeout(60)
    pg.evaluate("() => window.__arm('follower')")


def _apply(pg, top, path=None, view_h=400, mode="tile", pred=None):
    m = {"view": "browse", "path": path, "top": top, "viewH": view_h,
         "mode": mode}
    if pred is not None:
        m["pred"] = pred
    pg.evaluate(
        "(m) => window.__fireCtlEvent({type: 'ui-sync', payload: m})", m)


def test_same_context_receive_does_not_jump_scrolltop(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    st0 = _scroll(pg)
    assert st0 == pytest.approx(500, abs=2)
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 140)
    st1 = _scroll(pg)
    assert st1 == pytest.approx(st0, abs=1), \
        f"subsequent receive must not jump scrollTop: {st0} -> {st1}"
    pg.evaluate("() => window.__clock.frames(95, 16)")
    assert _scroll(pg) == pytest.approx(540, abs=2)


def test_receiver_moves_on_raf_between_samples(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 160)
    pos0 = _scroll(pg)
    pg.evaluate("() => window.__clock.frame(16)")
    st = _scroll(pg)
    max_top = pg.evaluate(
        f"() => ({_SC}).scrollHeight - ({_SC}).clientHeight")
    assert st > pos0, \
        f"smoothed motion must draw between samples: {pos0} -> {st}"
    assert st <= pos0 + 64, \
        f"frame step must be a bounded move, not a jump: {pos0} -> {st}"
    assert 0 <= st <= max_top
    pos1 = st
    pg.evaluate("() => window.__clock.frame(16)")
    assert _scroll(pg) != pos1


def test_unexpected_reversal_keeps_display_continuous(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 160)
    pg.evaluate("() => window.__clock.frame(16)")
    pg.evaluate("() => window.__clock.advance(44)")
    _apply(pg, 220)
    pg.evaluate("() => window.__clock.frame(16)")
    prev = _scroll(pg)
    h0 = _hook(pg)
    _apply(pg, 160)
    st = _scroll(pg)
    h1 = _hook(pg)
    assert st == pytest.approx(prev, abs=1), \
        f"direction reversal must stay continuous: {prev} -> {st}"
    assert h1["displayTop"] == pytest.approx(h0["displayTop"], abs=1e-6)
    assert h1["displayVel"] == pytest.approx(h0["displayVel"], abs=1e-6), \
        "display velocity must carry over an unexpected retarget"
    assert h1["active"] is True
    assert h1["history"] <= 3


def test_forward_stream_never_retreats_display(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 262)
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 388)
    pg.evaluate("() => window.__clock.frames(6, 16)")
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 390)
    prev = _scroll(pg)
    for _ in range(20):
        pg.evaluate("() => window.__clock.frame(16)")
        cur = _scroll(pg)
        assert cur >= prev - 0.01, \
            f"forward stream must not retreat: {prev} -> {cur}"
        assert cur <= 790 + 0.5, \
            f"display must not overshoot the last measured: {cur}"
        prev = cur
    pg.evaluate("() => window.__clock.frames(80, 16)")
    assert _scroll(pg) == pytest.approx(790, abs=2)


def test_pred_leads_target_beyond_measured(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 160, pred=300)
    for top in (180, 200, 220, 240):
        pg.evaluate("() => window.__clock.advance(60)")
        _apply(pg, top, pred=300)
        pg.evaluate("() => window.__clock.frames(5, 16)")
    assert _scroll(pg) == pytest.approx(700, abs=6), \
        "a sender-supplied endpoint must become the follow target"
    assert _scroll(pg) > 560 + 20, \
        "the display must run ahead of the measured position"


def test_pred_settles_to_measured_when_stream_ends(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 160, pred=320)
    pg.evaluate("() => window.__clock.frames(30, 16)")
    pg.evaluate("() => window.__clock.frames(60, 16)")
    assert _scroll(pg) == pytest.approx(560, abs=3), \
        "after the stream goes stale the display must settle at the last measured"


def test_pred_behind_measured_is_ignored(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 160, pred=50)
    pg.evaluate("() => window.__clock.frames(60, 16)")
    assert _scroll(pg) == pytest.approx(560, abs=3), \
        "a pred behind the measured position must not pull the display back"


def test_decay_scroll_sends_pred(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 50)
    for top in (100, 180, 220):
        pg.evaluate(f"() => ({_SC}).scrollTop = {top}")
        pg.wait_for_timeout(25)
        pg.evaluate("() => window.__clock.advance(30)")
    pg.evaluate("() => window.__clock.advance(150)")
    pg.wait_for_timeout(30)
    sent = pg.evaluate("() => window.__uiSent")
    assert sent
    with_pred = [s for s in sent if "pred" in s]
    assert with_pred, "decaying scroll must attach a fitted endpoint"
    assert with_pred[-1]["pred"] == pytest.approx(327, abs=3)
    assert with_pred[-1]["top"] == pytest.approx(220, abs=2)


def test_trace_records_receive_and_writes(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 300)
    pg.evaluate("() => window.__clock.frames(10, 16)")
    trace = pg.evaluate("() => window.__pairBrowseSync.trace")
    kinds = {e["k"] for e in trace}
    assert "r" in kinds and "w" in kinds
    writes = [e for e in trace if e["k"] == "w"]
    xs = [e["x"] for e in writes]
    assert all(b >= a - 0.01 for a, b in zip(xs, xs[1:])), \
        "written positions must be monotone during a forward stream"
    assert all("T" in e for e in writes)


def test_direct_input_cancels_receiver_animation(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 200)
    pg.evaluate("() => window.__clock.frame(16)")
    at_touch = _scroll(pg)
    pg.evaluate("() => window.__fireInput('pointerdown')")
    assert _scroll(pg) == at_touch, \
        "cancel must not write scrollTop or disturb native input"
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 300)
    pg.evaluate("() => window.__clock.frames(10, 16)")
    assert _scroll(pg) == pytest.approx(at_touch, abs=1), \
        "remote scroll must not move the list while local input is active"
    assert pg.evaluate("() => window.__uiSent.length") == 0, \
        "touchdown alone must not echo a position back"


def test_known_deceleration_estimates_blended_velocity(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 262)
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 388)
    hook = _hook(pg)
    assert hook["history"] == 3
    assert hook["modelVel"] == pytest.approx(2.25, abs=0.01)
    tops, vels = [], []
    for _ in range(7):
        pg.evaluate("() => window.__clock.frame(16)")
        h = _hook(pg)
        tops.append(h["modelTop"])
        vels.append(h["displayVel"])
    assert all(b >= a for a, b in zip(tops, tops[1:])), \
        "the follow target must never reverse during a forward stream"
    assert all(v >= 0 for v in vels), \
        "display velocity must not flip sign during a forward stream"


def test_constant_velocity_series_does_not_reset_display(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    for top in (160, 220, 280):
        pg.evaluate("() => window.__clock.advance(60)")
        before = _scroll(pg)
        _apply(pg, top)
        after = _scroll(pg)
        assert after == pytest.approx(before, abs=1), \
            f"receive must not reset display: {before} -> {after}"
    assert _hook(pg)["modelVel"] == pytest.approx(1.0, abs=0.05)


def test_same_time_burst_gap_jitter_stay_sane(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 160)
    _apply(pg, 170)
    pg.evaluate("() => window.__clock.advance(20)")
    _apply(pg, 200)
    pg.evaluate("() => window.__clock.advance(250)")
    _apply(pg, 300)
    pg.evaluate("() => window.__clock.advance(40)")
    _apply(pg, 340)
    pg.evaluate("() => window.__clock.advance(80)")
    _apply(pg, 380)
    pg.evaluate("() => window.__clock.advance(40)")
    _apply(pg, 420)
    pg.evaluate("() => window.__clock.frames(110, 16)")
    hook = _hook(pg)
    max_top = pg.evaluate(
        f"() => ({_SC}).scrollHeight - ({_SC}).clientHeight")
    for k in ("displayTop", "displayVel", "modelTop", "modelVel"):
        v = hook[k]
        assert v == v and abs(v) != float("inf"), f"{k}={v}"
    assert hook["history"] <= 3
    st = _scroll(pg)
    assert 0 <= st <= max_top
    assert hook["active"] is False


def test_display_stays_c1_across_stale_boundary(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 200)
    pg.evaluate("() => window.__clock.frame(119)")
    h1 = _hook(pg)
    pg.evaluate("() => window.__clock.frame(1)")
    h2 = _hook(pg)
    assert h2["displayVel"] == pytest.approx(h1["displayVel"], abs=0.5), \
        "velocity must be continuous across the first boundary"
    assert h2["displayTop"] == pytest.approx(h1["displayTop"], abs=4)
    h3 = _hook(pg)
    pg.evaluate("() => window.__clock.frame(59)")
    h4 = _hook(pg)
    pg.evaluate("() => window.__clock.frame(1)")
    h5 = _hook(pg)
    assert h5["displayVel"] == pytest.approx(h4["displayVel"], abs=0.5), \
        "velocity must be continuous across the stale boundary"
    assert h5["displayTop"] == pytest.approx(h4["displayTop"], abs=4)
    assert h3["displayVel"] != 0 or h5["displayTop"] != h3["displayTop"]


def test_frame_rate_does_not_change_result(pair_page):
    pg = pair_page
    _arm_follower(pg)
    checkpoints = (119, 120, 179, 180, 181, 240, 400)
    runs = []
    for dt in (16.6, 11.1, 8.3):
        _rearm(pg)
        _apply(pg, 100)
        pg.evaluate("() => window.__clock.advance(60)")
        _apply(pg, 200)
        t0 = pg.evaluate("() => window.__clock.now()")
        row = []
        for cp in checkpoints:
            pg.evaluate(
                "([d, until]) => {"
                "  while (window.__clock.now() < until)"
                "    window.__clock.frame("
                "      Math.min(d, until - window.__clock.now()));"
                "}", [dt, t0 + cp])
            h = _hook(pg)
            row.append((h["displayTop"], h["displayVel"]))
        runs.append(row)
    for row in runs[1:]:
        for i, s in enumerate(row):
            assert s[0] == pytest.approx(runs[0][i][0], abs=1e-6), i
            assert s[1] == pytest.approx(runs[0][i][1], abs=1e-6), i


def test_stale_receive_converges_to_last_measured(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 200)
    last = _scroll(pg)
    for _ in range(95):
        pg.evaluate("() => window.__clock.frame(16)")
        cur = _scroll(pg)
        assert abs(cur - last) <= 120, \
            f"per-frame move must stay bounded: {last} -> {cur}"
        last = cur
    assert _scroll(pg) == pytest.approx(600, abs=2)
    hook = _hook(pg)
    assert hook["active"] is False
    assert pg.evaluate("() => window.__clock.rafPending()") == 0


def test_oversized_unexpected_sample_retargets_smoothly(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 160)
    pg.evaluate("() => window.__clock.frame(16)")
    prev = _scroll(pg)
    vel_before = _hook(pg)["displayVel"]
    _apply(pg, 2000)
    st = _scroll(pg)
    assert st == pytest.approx(prev, abs=1), \
        f"oversized error must not snap: {prev} -> {st}"
    hook = _hook(pg)
    assert hook["displayTop"] == pytest.approx(prev, abs=1)
    assert hook["displayVel"] == pytest.approx(vel_before, abs=0.5), \
        "display velocity must carry over across the retarget"
    pg.evaluate("() => window.__clock.frame(16)")
    first = _scroll(pg)
    assert abs(first - 2400) > 800, \
        "a large error must not be covered in a single frame"
    last = first
    # The chase gap cap bounds the approach to a ~40px/frame cruise, so a
    # ~1900px retarget needs ~1.1s; allow ample frames then assert arrival.
    for _ in range(110):
        pg.evaluate("() => window.__clock.frame(16)")
        cur = _scroll(pg)
        assert abs(cur - last) <= 60
        last = cur
    assert _scroll(pg) == pytest.approx(2400, abs=2)


def test_pointercancel_with_touch_held_keeps_local_priority(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 200)
    pg.evaluate("() => window.__clock.frame(16)")
    at_touch = _scroll(pg)
    pg.evaluate("() => window.__fireInput('pointerdown')")
    pg.evaluate("() => window.__fireInput('touchstart')")
    pg.evaluate("() => window.__fireInput('pointercancel')")
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 300)
    pg.evaluate("() => window.__clock.frames(10, 16)")
    assert _scroll(pg) == pytest.approx(at_touch, abs=1), \
        "touch still held: remote must not move the list"
    pg.evaluate("() => window.__fireInput('touchend')")
    pg.evaluate("() => window.__clock.advance(450)")
    _apply(pg, 320)
    assert _scroll(pg) == pytest.approx(at_touch, abs=1), \
        "rejoin must start from actual position, not jump"
    pg.evaluate("() => window.__clock.frames(40, 16)")
    assert _scroll(pg) == pytest.approx(720, abs=2)


@pytest.mark.parametrize("start,end", [("touchstart", "touchend"),
                                      ("pointerdown", "pointerup")])
def test_single_input_channel_blocks_remote(pair_page, start, end):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 200)
    pg.evaluate("() => window.__clock.frame(16)")
    at_touch = _scroll(pg)
    pg.evaluate(f"() => window.__fireInput('{start}')")
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 300)
    pg.evaluate("() => window.__clock.frames(10, 16)")
    assert _scroll(pg) == pytest.approx(at_touch, abs=1)
    pg.evaluate(f"() => window.__fireInput('{end}')")
    pg.evaluate("() => window.__clock.advance(450)")
    _apply(pg, 320)
    assert _scroll(pg) == pytest.approx(at_touch, abs=1)
    pg.evaluate("() => window.__clock.frames(40, 16)")
    assert _scroll(pg) == pytest.approx(720, abs=2)


def test_lost_input_released_on_blur(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 200)
    pg.evaluate("() => window.__clock.frame(16)")
    at_touch = _scroll(pg)
    pg.evaluate("() => window.__fireInput('pointerdown')")
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 300)
    pg.evaluate("() => window.__clock.frames(10, 16)")
    assert _scroll(pg) == pytest.approx(at_touch, abs=1)
    pg.evaluate("() => window.__fireBlur()")
    pg.evaluate("() => window.__clock.advance(450)")
    _apply(pg, 320)
    assert _scroll(pg) == pytest.approx(at_touch, abs=1)
    pg.evaluate("() => window.__clock.frames(40, 16)")
    assert _scroll(pg) == pytest.approx(720, abs=2)


def test_wheel_and_scroll_key_cancel_follow(pair_page):
    pg = pair_page
    for fire in ("() => window.__fireInput('wheel')",
                 "() => window.__fireKeyOn("
                 "document.getElementById('sidebar-scroll'), 'ArrowDown')"):
        _rearm(pg)
        _apply(pg, 100)
        pg.evaluate("() => window.__clock.advance(60)")
        _apply(pg, 200)
        pg.evaluate("() => window.__clock.frame(16)")
        at = _scroll(pg)
        pg.evaluate(fire)
        assert _scroll(pg) == at
        assert _hook(pg)["active"] is False
        pg.evaluate("() => window.__clock.advance(60)")
        _apply(pg, 260)
        pg.evaluate("() => window.__clock.frames(10, 16)")
        assert _scroll(pg) == pytest.approx(at, abs=1), \
            "remote must stay rejected through the local-intent window"
        assert pg.evaluate("() => window.__uiSent.length") == 0, \
            "wheel/key intent alone must not echo a position"
        pg.evaluate("() => window.__clock.advance(450)")
        _apply(pg, 260)
        assert _scroll(pg) == pytest.approx(at, abs=1)
        pg.evaluate("() => window.__clock.frames(40, 16)")
        assert _scroll(pg) == pytest.approx(660, abs=2)


def test_scroll_key_in_editable_does_not_cancel(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 200)
    pg.evaluate("() => window.__clock.frame(16)")
    pg.evaluate("() => window.__makeInput()")
    pg.evaluate(
        "() => window.__fireKeyOn(document.querySelector('input'),"
        " 'ArrowDown')")
    assert _hook(pg)["active"] is True
    pg.evaluate(
        "() => window.__fireKeyOn("
        "document.getElementById('sidebar-scroll'), 'ArrowDown')")
    assert _hook(pg)["active"] is False


def test_remote_stream_beyond_suppression_sends_no_echo(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    for i in range(8):
        pg.evaluate("() => window.__clock.advance(60)")
        _apply(pg, 160 + i * 30)
        pg.evaluate("() => window.__clock.frame(16)")
        pg.wait_for_timeout(30)
        assert pg.evaluate("() => window.__uiSent.length") == 0
    pg.evaluate("() => window.__clock.frames(60, 16)")
    pg.wait_for_timeout(80)
    assert pg.evaluate("() => window.__uiSent.length") == 0


def test_clamped_remote_never_echoes_during_follow(pair_page):
    pg = pair_page
    _arm_follower(pg)
    max_top = pg.evaluate(
        f"() => ({_SC}).scrollHeight - ({_SC}).clientHeight")
    _apply(pg, 3300)
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 3400)
    hit_max = False
    for _ in range(40):
        pg.evaluate("() => window.__clock.frame(16)")
        if _scroll(pg) >= max_top - 1:
            hit_max = True
    assert hit_max, \
        "an out-of-range follow target must drive into the physical clamp"
    hook = _hook(pg)
    assert hook["displayTop"] <= max_top + 1
    pg.evaluate("() => window.__clock.frames(60, 16)")
    pg.wait_for_timeout(80)
    assert pg.evaluate("() => window.__uiSent.length") == 0
    assert _scroll(pg) == pytest.approx(max_top, abs=2), \
        "the display settles at the physical edge, not beyond it"


def test_genuine_local_scroll_sends_at_existing_cadence(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    pg.evaluate(f"() => ({_SC}).scrollTop = 700")
    pg.wait_for_timeout(80)
    sent = pg.evaluate("() => window.__uiSent")
    assert len(sent) == 1
    assert sent[0]["top"] == pytest.approx(700, abs=2)


def test_reset_boundaries_apply_immediately(pair_page):
    pg = pair_page
    _arm_follower(pg)
    for trigger in ("() => window.__fireResize()",
                    "() => window.__setHidden(true)",
                    "() => window.__rebuildGrid()",
                    "rearm"):
        _apply(pg, 100)
        pg.evaluate("() => window.__clock.advance(60)")
        _apply(pg, 160)
        pg.evaluate("() => window.__clock.frame(16)")
        if trigger == "rearm":
            _rearm(pg)
        else:
            pg.evaluate(trigger)
        if "rebuild" in trigger:
            pg.wait_for_timeout(80)
        assert pg.evaluate("() => window.__clock.rafPending()") == 0, trigger
        pg.evaluate("() => window.__setHidden(false)")
        _apply(pg, 200)
        assert _scroll(pg) == pytest.approx(600, abs=2), trigger


def test_list_mode_applies_immediately(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 160, mode="list")
    assert _scroll(pg) == pytest.approx(560, abs=2)
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 180, mode="list")
    assert _scroll(pg) == pytest.approx(580, abs=2)
    assert _hook(pg)["active"] is False
    assert pg.evaluate("() => window.__clock.rafPending()") == 0


def test_reduced_motion_applies_immediately(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    pg.evaluate("() => window.__setReduce(true)")
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 200)
    assert _scroll(pg) == pytest.approx(600, abs=2)
    assert _hook(pg)["active"] is False
    assert pg.evaluate("() => window.__clock.rafPending()") == 0
    pg.evaluate("() => window.__setReduce(false)")


def test_solo_has_no_resident_raf(pair_page):
    pg = pair_page
    assert pg.evaluate("() => window.__clock.rafPending()") == 0
    _apply(pg, 100)
    pg.evaluate("() => window.__clock.frames(20, 16)")
    assert pg.evaluate("() => window.__clock.rafPending()") == 0
    assert _hook(pg)["active"] is False


def test_history_capped_and_single_raf(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    for i in range(6):
        pg.evaluate("() => window.__clock.advance(60)")
        _apply(pg, 160 + i * 40)
        assert _hook(pg)["history"] <= 3
        assert pg.evaluate("() => window.__clock.rafPending()") <= 1


def test_no_resident_raf_or_timers_after_converge(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 160)
    pg.evaluate("() => window.__clock.frames(110, 16)")
    assert pg.evaluate("() => window.__clock.rafPending()") == 0
    pg.evaluate("() => window.__clock.advance(3000)")
    assert pg.evaluate("() => window.__clock.timersPending()") == 0


def test_forward_jump_converges_after_retarget(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 160)
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 400)
    pg.evaluate("() => window.__clock.frames(140, 16)")
    assert _scroll(pg) == pytest.approx(800, abs=2)
    hook = _hook(pg)
    assert hook["active"] is False
    assert abs(hook["displayVel"]) <= 0.01


def test_genuine_local_move_uses_existing_cadence(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)

    def local_move(top, adv):
        pg.evaluate(f"() => window.__clock.advance({adv})")
        pg.evaluate(f"() => ({_SC}).scrollTop = {top}")
        pg.wait_for_timeout(80)

    t0 = pg.evaluate("() => window.__clock.now()")
    local_move(700, 1)
    sent = pg.evaluate("() => window.__uiSent")
    assert [s["top"] for s in sent] == [700]
    local_move(720, 20)
    assert pg.evaluate("() => window.__uiSent.length") == 1
    local_move(760, 40)
    sent = pg.evaluate("() => window.__uiSent")
    assert [s["top"] for s in sent] == [700, 760]
    local_move(780, 20)
    assert pg.evaluate("() => window.__uiSent.length") == 2
    pg.evaluate("() => window.__clock.advance(120)")
    pg.wait_for_timeout(80)
    assert pg.evaluate("() => window.__clock.now()") - t0 == 201
    sent = pg.evaluate("() => window.__uiSent")
    assert [s["top"] for s in sent] == [700, 760, 780]
    for s in sent:
        assert s["view"] == "browse" and s["mode"] == "tile"
        assert s["viewH"] == 400 and s["path"] is None


def test_local_move_back_to_remote_position_still_sends(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    pg.wait_for_timeout(80)
    assert pg.evaluate("() => window.__uiSent.length") == 0
    pg.evaluate(f"() => ({_SC}).scrollTop = 700")
    pg.wait_for_timeout(80)
    pg.evaluate("() => window.__clock.advance(61)")
    pg.evaluate(f"() => ({_SC}).scrollTop = 500")
    pg.wait_for_timeout(80)
    sent = pg.evaluate("() => window.__uiSent")
    assert [s["top"] for s in sent] == [700, 500], \
        "a genuine move back to the last written position must still send"


def test_solo_input_is_not_recorded(pair_page):
    pg = pair_page
    pg.evaluate("() => window.__fireInput('pointerdown')")
    pg.evaluate("() => window.__fireInput('pointerup')")
    pg.evaluate("() => window.__fireInput('touchstart')")
    pg.evaluate("() => window.__fireInput('touchend')")
    pg.evaluate("() => window.__fireInput('touchcancel')")
    pg.evaluate("() => window.__fireInput('pointercancel')")
    pg.evaluate("() => window.__fireInput('wheel')")
    pg.evaluate(
        "() => window.__fireKeyOn("
        "document.getElementById('sidebar-scroll'), 'ArrowDown')")
    _arm_follower(pg)
    assert _hook(pg)["local"] is False
    _apply(pg, 100)
    assert _scroll(pg) == pytest.approx(500, abs=2)


def test_release_outside_scroller_ends_local_hold(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 200)
    pg.evaluate("() => window.__clock.frame(16)")
    at = _scroll(pg)
    pg.evaluate("() => window.__fireInput('pointerdown')")
    pg.evaluate(
        "() => document.body.dispatchEvent(new PointerEvent('pointerup',"
        " {bubbles: true, pointerId: 1, isPrimary: true,"
        " pointerType: 'mouse'}))")
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 300)
    assert _scroll(pg) == pytest.approx(at, abs=1)
    pg.evaluate("() => window.__clock.advance(450)")
    _apply(pg, 320)
    assert _scroll(pg) == pytest.approx(at, abs=1)
    pg.evaluate("() => window.__clock.frames(40, 16)")
    assert _scroll(pg) == pytest.approx(720, abs=2)


def test_tick_gate_stops_writes_without_new_frames(pair_page):
    pg = pair_page
    triggers = ("() => { window.searchMode = true; }",
                "() => window.__arm('follower', {sessionId: 'S2'})",
                "() => window.__arm('follower', {status: 'reconnecting'})",
                "() => window.__arm('leader')")
    for trigger in triggers:
        _rearm(pg)
        pg.evaluate("() => { window.searchMode = false; }")
        _apply(pg, 100)
        pg.evaluate("() => window.__clock.advance(60)")
        _apply(pg, 160)
        pg.evaluate("() => window.__clock.frame(16)")
        before = _scroll(pg)
        assert _hook(pg)["active"] is True
        pg.evaluate(trigger)
        pg.evaluate("() => window.__clock.frames(10, 16)")
        assert _scroll(pg) == before, trigger
        assert pg.evaluate("() => window.__clock.rafPending()") == 0, trigger
        assert _hook(pg)["active"] is False, trigger


def test_receive_between_frames_keeps_integration_time(pair_page):
    pg = pair_page
    _arm_follower(pg)

    def run(extra):
        _rearm(pg)
        _apply(pg, 100)
        pg.evaluate("() => window.__clock.advance(60)")
        _apply(pg, 200)
        pg.evaluate("() => window.__clock.frame(16)")
        pg.evaluate("() => window.__clock.advance(8)")
        if extra:
            _apply(pg, 200)
        pg.evaluate("() => window.__clock.frame(8)")
        return _hook(pg)["displayTop"]

    assert run(True) == pytest.approx(run(False), abs=1e-6), \
        "a receive between frames must not drop elapsed time from the filter"


def test_burst_arrival_keeps_direction_and_pred(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 160, pred=300)
    assert _hook(pg)["modelTop"] == pytest.approx(700, abs=1)
    pg.evaluate("() => window.__clock.advance(12)")
    _apply(pg, 170, pred=300)
    assert _hook(pg)["modelTop"] == pytest.approx(700, abs=1), \
        "a jitter burst must not drop the target back to the measured position"
    prev = _scroll(pg)
    for _ in range(8):
        pg.evaluate("() => window.__clock.frame(16)")
        cur = _scroll(pg)
        assert cur >= prev - 0.01, f"display must not retreat: {prev} -> {cur}"
        prev = cur


def test_drag_sends_no_pred(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 50)
    pg.evaluate("() => window.__fireInput('touchstart')")
    for top in (100, 180, 220):
        pg.evaluate(f"() => ({_SC}).scrollTop = {top}")
        pg.wait_for_timeout(25)
        pg.evaluate("() => window.__clock.advance(30)")
    pg.evaluate("() => window.__clock.advance(150)")
    pg.wait_for_timeout(30)
    sent = pg.evaluate("() => window.__uiSent")
    assert sent
    assert not [s for s in sent if "pred" in s], \
        "a finger drag is not a fling — no endpoint may be extrapolated"


def test_trailing_send_after_stop_sends_no_pred(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 50)
    for top, adv in ((100, 30), (180, 20), (220, 0)):
        pg.evaluate(f"() => ({_SC}).scrollTop = {top}")
        pg.wait_for_timeout(25)
        pg.evaluate(f"() => window.__clock.advance({adv})")
    pg.evaluate("() => window.__clock.advance(200)")
    pg.wait_for_timeout(30)
    sent = pg.evaluate("() => window.__uiSent")
    assert [s["top"] for s in sent] == [100, 220]
    assert "pred" not in sent[-1], \
        "the trailing send fires after motion stopped — no stale endpoint"


def test_first_frame_applies_during_local_input(pair_page):
    pg = pair_page
    _arm_follower(pg)
    pg.evaluate("() => window.__fireInput('touchstart')")
    _apply(pg, 100)
    assert _scroll(pg) == pytest.approx(500, abs=2), \
        "the first frame after arm must land so the follower unlocks"
    assert pg.evaluate("() => window.__pairBrowseSync.unlocked") is True


def test_space_on_button_does_not_cancel_follow(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    pg.evaluate("() => window.__clock.advance(60)")
    _apply(pg, 200)
    pg.evaluate("() => window.__clock.frame(16)")
    pg.evaluate(
        "() => { var b = document.createElement('button');"
        " document.getElementById('sidebar').appendChild(b);"
        " window.__fireKeyOn(b, ' '); }")
    assert _hook(pg)["active"] is True


def test_reduced_motion_still_rejects_while_input_held(pair_page):
    pg = pair_page
    _arm_follower(pg)
    _apply(pg, 100)
    pg.evaluate("() => window.__setReduce(true)")
    pg.evaluate("() => window.__clock.advance(60)")
    pg.evaluate("() => window.__fireInput('pointerdown')")
    _apply(pg, 200)
    assert _scroll(pg) == pytest.approx(500, abs=2), \
        "held local input beats even the reduced-motion immediate path"
    pg.evaluate("() => window.__fireInput('pointerup')")
    pg.evaluate("() => window.__clock.advance(450)")
    _apply(pg, 200)
    assert _scroll(pg) == pytest.approx(600, abs=2)
    pg.evaluate("() => window.__setReduce(false)")
