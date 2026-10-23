"""Experimental three-spread ring reader (?reader=ring) — functional checks.

?reader=ring loads viewer/ringCache.js + viewer/ring.js and replaces the
variable-width strip with three fixed-stride .ring-slot roots (one
.spread-container with two page cells each, ≤6 mounted images). Everything
else — gestures, URL hash, progress, tap zones, zoom, overview, offline URL
resolution — is shared, and the default reader must be identical when the
flag is absent or unknown. All assertions are functional; no perf budgets.
"""
import pytest
from playwright.sync_api import expect

from viewer_touch import (
    open_book,
    touch_session,
    tap,
    drag,
    pinch,
    settle,
    track_tx,
    touch_end,
)

FOLDER = "long"          # 30 pages, single chapter
SERIES = "zoom_series/series/ep-1"  # multi-chapter book (overview fixture)
MIXED_AR = "mixed_ar"    # 6 pages alternating 40x60 / 90x60
UNICODE = "夏 の 本"      # 1 page — unicode + spaces in the path
MIB = 64 * 1024 * 1024

LX, CX, RX, Y = 60, 195, 330, 500  # 390-wide portrait zones: next / center / prev


def _ring_book(page, server, folder=FOLDER, p=0, total=30):
    page.goto(f"{server}/?reader=ring#/view/{folder}?p={p}")
    expect(page.locator("#counter")).to_have_text(f"{p + 1} / {total}")


def _dbg(page):
    return page.evaluate("window.__ringReaderDebug ? window.__ringReaderDebug() : null")


def _slots(page):
    return page.evaluate("document.querySelectorAll('.ring-slot').length")


def _mounted_imgs(page):
    return page.evaluate(
        "document.querySelectorAll('.ring-slot .page-cell img').length"
    )


def _wait_turn(page, idx):
    """A turn has fully landed: index moved, slide settled, anchor re-based.
    currentIdx alone is racy — it updates before the next rAF sets
    sliding/transition, so require the settle artifacts too."""
    page.wait_for_function(
        f"window.currentIdx === {idx} && window.sliding === false && "
        f"window.anchorSpread === window.SpreadView.spreadOf({idx}) && "
        f"document.querySelector('#img-track').style.transition === 'none'"
    )


def _assert_window(page, idx):
    """Mounted spreads are exactly the clamped {s-1, s, s+1} window."""
    out = page.evaluate(
        """(idx) => {
          const s = window.SpreadView.spreadOf(idx);
          const want = [s - 1, s, s + 1].filter(t => t >= 0 && t < window.spreads.length);
          return { want, got: window.__ringReaderDebug().spreads };
        }""",
        idx,
    )
    assert out["got"] == sorted(out["want"]), out


# ── 1. flag gating ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("q", ["", "?reader=bogus", "?reader=Ring"], ids=["absent", "unknown", "case"])
def test_flag_absent_or_unknown_keeps_strip(page, server, q):
    """Without the exact flag the strip renderer runs and no ring nodes exist."""
    page.goto(f"{server}/{q}#/view/{FOLDER}?p=0")
    expect(page.locator("#counter")).to_have_text("1 / 30")
    assert page.evaluate("window.__bookqReader") == "strip"
    assert _slots(page) == 0
    assert _dbg(page) is None
    assert page.evaluate(
        "document.querySelector('#img-track').classList.contains('ring-reader')"
    ) is False


def test_ring_flag_mounts_three_slot_roots(page, server):
    _ring_book(page, server)
    assert page.evaluate("window.__bookqReader") == "ring"
    assert page.evaluate(
        "document.querySelector('#img-track').classList.contains('ring-reader')"
    ) is True
    assert _slots(page) == 3
    assert _mounted_imgs(page) <= 6
    d = _dbg(page)
    assert d["slotCount"] == 3
    # At spread 0 the previous slot is out of range: only {0,1} mount.
    assert d["spreads"] == [0, 1]


def test_ring_scripts_only_load_with_flag(page, server):
    """Default pages must not even request the ring modules — the opt-in
    loads them on demand from the client, so a strip session stays lean."""
    seen = []
    page.on(
        "request",
        lambda r: seen.append(r.url) if r.resource_type == "script" else None,
    )
    page.goto(f"{server}/#/view/{FOLDER}?p=0")
    expect(page.locator("#counter")).to_have_text("1 / 30")
    assert not any("ring" in u for u in seen), seen
    # The opt-in DOES fetch them — client-initiated, on first viewer open.
    page.goto(f"{server}/?reader=ring#/view/{FOLDER}?p=0")
    expect(page.locator("#counter")).to_have_text("1 / 30")
    assert any("viewer/ring.js" in u for u in seen), seen
    assert any("viewer/ringCache.js" in u for u in seen), seen


def test_ring_flag_with_missing_module_falls_back(page, server):
    """reader=ring with ringCache.js unreachable must fall back to the strip —
    the flag alone must not pretend a ring exists."""
    page.route("**/viewer/ringCache.js", lambda r: r.abort())
    page.goto(f"{server}/?reader=ring#/view/{FOLDER}?p=0")
    expect(page.locator("#counter")).to_have_text("1 / 30")
    assert page.evaluate("window.__bookqReader") == "strip"
    assert _slots(page) == 0
    assert page.evaluate(
        "document.querySelector('#img-track').classList.contains('ring-reader')"
    ) is False
    # The legacy strip still mounts its window — only the marker is ring-free.
    assert page.evaluate("Object.keys(window.stripImgs).length") > 0
    page.unroute("**/viewer/ringCache.js")


# ── 2. long navigation ──────────────────────────────────────────────────────

def test_long_navigation_slot_identity_index_and_cache_bounds(ios_page, server):
    """15 turns each way: slot roots keep identity, window/counter/cache stay sane."""
    _ring_book(ios_page, server, p=0)
    ios_page.evaluate(
        "window.__roots = Array.from(document.querySelectorAll('.ring-slot'))"
    )
    for i in range(15):
        ios_page.keyboard.press("ArrowLeft")   # RTL forward
        _wait_turn(ios_page, i + 1)
        settle(ios_page)
        assert ios_page.evaluate(
            f"document.querySelector('#counter').textContent.trim()"
        ) == f"{i + 2} / 30"
        _assert_window(ios_page, i + 1)
    for i in range(15):
        ios_page.keyboard.press("ArrowRight")
        _wait_turn(ios_page, 14 - i)
        settle(ios_page)
    expect(ios_page.locator("#counter")).to_have_text("1 / 30")
    assert ios_page.evaluate(
        "window.__roots.every((r, i) => "
        "r === document.querySelectorAll('.ring-slot')[i])"
    ), "slot roots must keep DOM identity across turns"
    d = _dbg(ios_page)
    assert d["cache"]["entries"] <= 12
    assert d["cache"]["maxEntries"] == 12
    # Byte estimate stays under budget unless the pinned window alone exceeds it.
    assert (
        d["cache"]["bytes"] <= MIB
        or d["cache"]["pinnedBytes"] > d["cache"]["maxBytes"]
    )
    assert _mounted_imgs(ios_page) <= 6


def test_adjacent_turn_ack_does_not_rebind_window(page, server):
    """A prepared adjacent turn must acknowledge without a synchronous
    ensureWindow rebind — the deferred window job owns the rebind
    (acknowledge-first). The factory is wrapped before the book opens so the
    real public method is counted, no production instrumentation."""
    page.goto(f"{server}/?reader=ring")
    # Modules load on demand at the first viewer open — inject them here so
    # the factory is wrapped before the book's first create() runs.
    page.add_script_tag(url=f"{server}/static/viewer/ringCache.js")
    page.add_script_tag(url=f"{server}/static/viewer/ring.js")
    page.wait_for_function("!!window.RingReader")
    page.evaluate(
        """(() => {
          window.__calls = { ensureWindow: 0 };
          const orig = window.RingReader.create;
          window.RingReader.create = function (cfg) {
            const inst = orig(cfg);
            const ew = inst.ensureWindow.bind(inst);
            inst.ensureWindow = function (i) {
              window.__calls.ensureWindow++;
              return ew(i);
            };
            return inst;
          };
        })()"""
    )
    page.evaluate(f"location.hash = '#/view/{FOLDER}?p=0'")
    expect(page.locator("#counter")).to_have_text("1 / 30")
    settle(page, 4)                       # let the deferred window job run
    n0 = page.evaluate("window.__calls.ensureWindow")
    assert n0 > 0                         # the instrumented instance is live
    page.evaluate(
        "window.__roots = Array.from(document.querySelectorAll('.ring-slot'))"
    )
    # Synchronous adjacent snap: the target is already mounted, so the ack
    # path must not re-pin/rebind the window. The count is read inside the
    # SAME evaluate — a separate round-trip could let the deferred rAF job
    # slip in between under load and flake the assertion.
    during_ack = page.evaluate(
        "(() => { window.viewerSnapTo(1, true); "
        "return window.__calls.ensureWindow; })()"
    )
    assert during_ack == n0, (during_ack, n0)
    _wait_turn(page, 1)
    # The deferred window job then advances the binding to {0,1,2}.
    page.wait_for_function(f"window.__calls.ensureWindow > {n0}")
    _assert_window(page, 1)
    assert page.evaluate(
        "window.__roots.every((r, i) => "
        "r === document.querySelectorAll('.ring-slot')[i])"
    ), "slot roots must keep identity through the deferred rebind"


def test_fetch_priority_promotes_current_spread(page, server):
    """Mounted current-spread images fetch high, neighbours low. A cached
    page that becomes current is promoted on the SAME <img> — no new fetch,
    no URL change."""
    _ring_book(page, server, p=0)
    page.wait_for_function("!!window.stripImgs[0] && !!window.stripImgs[1]")
    assert page.evaluate("window.stripImgs[0].fetchPriority") == "high"
    assert page.evaluate("window.stripImgs[1].fetchPriority") == "low"
    page.evaluate(
        "window.__p1 = window.stripImgs[1]; "
        "window.__p1src = window.__p1.getAttribute('src')"
    )
    page.keyboard.press("ArrowLeft")
    _wait_turn(page, 1)
    # Promotion must reuse the element — same node, same src, no re-request.
    assert page.evaluate("window.stripImgs[1] === window.__p1") is True
    assert page.evaluate(
        "window.stripImgs[1].getAttribute('src') === window.__p1src"
    ) is True
    assert page.evaluate("window.stripImgs[1].fetchPriority") == "high"
    # The old current is now a neighbour → low; the new next → low.
    assert page.evaluate("window.stripImgs[0].fetchPriority") == "low"
    assert page.evaluate("window.stripImgs[2].fetchPriority") == "low"


# ── 3. portrait RTL touch ───────────────────────────────────────────────────

def test_touch_taps_swipes_and_boundary(ios_page, server):
    _ring_book(ios_page, server, p=1)
    s = touch_session(ios_page)
    tap(s, LX, Y)                     # left tap → next (RTL forward)
    s.detach()
    _wait_turn(ios_page, 2)
    s = touch_session(ios_page)
    tap(s, RX, Y)                     # right tap → prev
    s.detach()
    _wait_turn(ios_page, 1)
    s = touch_session(ios_page)
    drag(s, 200, 450, 330, 450)       # pull right → forward swipe
    s.detach()
    ios_page.wait_for_function("window.currentIdx === 2")
    settle(ios_page)
    # Boundaries are silent no-ops.
    page = ios_page
    page.goto(f"{server}/?reader=ring#/view/{FOLDER}?p=0")
    expect(page.locator("#counter")).to_have_text("1 / 30")
    s = touch_session(ios_page)
    tap(s, RX, Y)                     # prev at first page
    s.detach()
    settle(ios_page)
    assert ios_page.evaluate("window.currentIdx") == 0


def test_synthetic_click_fires_once(ios_page, server):
    """A real touchscreen tap fires the synthesized click too — the pair must
    turn exactly once (the click is suppressed, not double-counted)."""
    _ring_book(ios_page, server, p=1)
    ios_page.touchscreen.tap(LX, Y)   # touch events + the synthesized click
    _wait_turn(ios_page, 2)
    ios_page.wait_for_timeout(500)    # past the click-suppression window
    assert ios_page.evaluate("window.currentIdx") == 2


def test_early_tap_during_pending_swipe_advances_again(ios_page, server):
    """A tap landing mid-commit lands the swipe first, then turns once more;
    the swipe's delayed fallback timer must not undo the newer index."""
    _ring_book(ios_page, server, p=1)
    s = touch_session(ios_page)
    drag(s, 200, 450, 330, 450)       # forward swipe → commit in flight
    s.detach()
    # The tap must arrive while the commit is still pending — otherwise the
    # landing below exercises a plain tap turn, not the pending-commit path.
    ios_page.evaluate(
        """() => {
          // Pending-assert and the tap dispatch share one task — see the
          // regroup test for why a separate input round-trip could pass
          // vacuously. Left-zone tap = forward, so this both lands the
          // pending swipe (armDrag) and turns once more (touchend classify).
          if (!window.__ringCommitPending || !window.__ringCommitPending())
            throw new Error('ring commit already finalized before tap');
          const el = document.getElementById('img-container');
          const r = el.getBoundingClientRect();
          const mk = () => new Touch({identifier: 7, target: el,
            clientX: r.left + 60, clientY: r.top + r.height / 2});
          el.dispatchEvent(new TouchEvent('touchstart',
            {touches: [mk()], targetTouches: [mk()], changedTouches: [mk()],
             bubbles: true, cancelable: true}));
          el.dispatchEvent(new TouchEvent('touchend',
            {touches: [], changedTouches: [mk()],
             bubbles: true, cancelable: true}));
        }"""
    )
    ios_page.wait_for_function("window.currentIdx === 3")
    settle(ios_page)
    ios_page.wait_for_timeout(450)    # outlast the 260ms fallback timer
    assert ios_page.evaluate("window.currentIdx") == 3
    expect(ios_page.locator("#counter")).to_have_text("4 / 30")


def test_tapmode_switch_remaps_ring_turns(ios_page, server):
    _ring_book(ios_page, server, p=5)
    # Controls start visible; the click path is what test_viewer_tapmode uses.
    ios_page.locator("#tapmode-right").click()
    assert ios_page.evaluate("window.__tapForward()") == "right"
    ios_page.wait_for_timeout(500)
    s = touch_session(ios_page)
    tap(s, RX, Y)                     # right tap now means forward
    s.detach()
    _wait_turn(ios_page, 6)


def test_late_aspect_during_drag_letterboxes_not_stretches(ios_page, server):
    """A wide image landing while geometry is frozen mid-drag must letterbox
    (object-fit:contain) inside the stale cell — real geometry applies only
    after the turn settles."""
    held = []
    page = ios_page
    page.route(
        f"**/image/{MIXED_AR}/p2.png", lambda r: held.append(r)
    )
    _ring_book(page, server, folder=MIXED_AR, p=0, total=6)
    page.wait_for_function("!!window.stripCells[1]")
    stale_h = page.evaluate("window.stripCells[1].style.height")
    assert stale_h
    s = touch_session(page)
    drag(s, 200, 450, 330, 450, hold=True)      # active drag, touch still down
    for r in held:
        r.continue_()
    page.wait_for_function(
        "!!window.stripImgs[1] && window.stripImgs[1].classList.contains('loaded')"
    )
    # AR recorded but geometry frozen: stale cell unchanged, layout deferred.
    assert page.evaluate("window.stripCells[1].style.height") == stale_h
    assert _dbg(page)["layoutDirty"] is True
    assert page.evaluate(
        "getComputedStyle(window.stripImgs[1]).objectFit"
    ) == "contain"
    touch_end(s)                                # commit the swipe
    s.detach()
    _wait_turn(page, 1)
    settle(page)
    # After settle the real wide AR (90/60=1.5) re-shapes the cell — in
    # portrait a wide page clamps to viewW, so height is what changes.
    new_h = page.evaluate("window.stripCells[1].style.height")
    assert new_h and new_h != stale_h, (stale_h, new_h)
    assert _dbg(page)["layoutDirty"] is False
    page.unroute(f"**/image/{MIXED_AR}/p2.png")


# ── 4. landscape / spread layout ────────────────────────────────────────────

def _active_center(page):
    """Offset of the anchor spread's container from exact horizontal centre."""
    return page.evaluate(
        """() => {
          const el = (window.spreadEls || {})[window.anchorSpread];
          if (!el) return null;
          const r = el.getBoundingClientRect();
          return { off: (r.left + r.width / 2) - window.innerWidth / 2,
                   w: r.width, bottom: r.bottom, vh: window.innerHeight };
        }"""
    )


def test_landscape_spread_grouping_and_centering(ipad_landscape_page, server):
    page = ipad_landscape_page
    _ring_book(page, server, p=0)
    assert page.evaluate("window.pageMode") == "spread"
    assert page.evaluate("window.spreads[0].pages.length") == 2
    c = _active_center(page)
    assert abs(c["off"]) <= 1, c
    # Landscape anchors at the bottom edge of the viewport.
    assert abs(c["bottom"] - c["vh"]) <= 1, c
    # Solo cover: spread shift makes spread 0 a one-page spread, still centred.
    page.locator("#spread-shift").click()
    page.wait_for_function("window.spreads[0].pages.length === 1")
    c = _active_center(page)
    assert abs(c["off"]) <= 1, c
    _assert_window(page, 0)
    # Last odd page solo spread: jump to the last page, one cell visible.
    page.evaluate("window.viewerSnapTo(window.images.length - 1, false)")
    _wait_turn(page, 29)
    assert page.evaluate(
        "window.spreads[window.spreads.length - 1].pages.length"
    ) == 1
    d = _dbg(page)
    assert len(d["spreads"]) <= 3
    assert _mounted_imgs(page) <= 6


def test_landscape_toggle_1p_2p_rebinds(ipad_landscape_page, server):
    page = ipad_landscape_page
    _ring_book(page, server, p=4)
    assert page.evaluate("window.pageMode") == "spread"
    page.locator("#spread-toggle").click()          # 2P → 1P
    page.wait_for_function("window.pageMode === 'single'")
    _wait_turn(page, 4)
    _assert_window(page, 4)
    assert _slots(page) == 3
    page.locator("#spread-toggle").click()          # 1P → 2P
    page.wait_for_function("window.pageMode === 'spread'")
    _assert_window(page, page.evaluate("window.currentIdx"))


def test_resize_while_idle_and_sliding(ipad_landscape_page, server):
    page = ipad_landscape_page
    _ring_book(page, server, p=4)
    # Idle resize: landscape → portrait.
    page.set_viewport_size({"width": 820, "height": 1180})
    page.wait_for_function("window.pageMode === 'single'")
    _wait_turn(page, 4)
    _assert_window(page, 4)
    page.set_viewport_size({"width": 1180, "height": 820})
    page.wait_for_function("window.pageMode === 'spread'")
    _assert_window(page, 4)
    # Resize while a slide is in flight: the turn must still land, then the
    # window rebinds against the new viewport.
    page.keyboard.press("ArrowLeft")
    page.wait_for_function("window.currentIdx === 6")
    page.set_viewport_size({"width": 820, "height": 1180})
    page.wait_for_function("window.pageMode === 'single'")
    settle(page, 4)
    assert page.evaluate("window.sliding") is False
    d = _dbg(page)
    assert d["spreads"], "window must rebind after mid-slide resize"
    _assert_window(page, page.evaluate("window.currentIdx"))


def test_landscape_mixed_ar_loaded_geometry_no_peek(ipad_landscape_page, server):
    """Mixed aspect ratios with real loaded images: the active spread is
    centred on its true geometry, both page cells carry distinct widths, and
    no mounted neighbour spread peeks into the viewport — in 2P and 1P."""
    page = ipad_landscape_page
    _ring_book(page, server, folder=MIXED_AR, p=0, total=6)
    assert page.evaluate("window.pageMode") == "spread"
    # Wait until every mounted image actually loaded — geometry assertions
    # must run on real ARs, not the fallback.
    page.wait_for_function(
        """[...document.querySelectorAll('.ring-slot img')].every(
          i => i.classList.contains('loaded') && i.naturalWidth > 0)"""
    )
    c = _active_center(page)
    assert abs(c["off"]) <= 1, c
    # 2P spread {40x60, 90x60}: the wide page's cell is visibly wider.
    w0, w1 = page.evaluate(
        """(() => {
          const s = window.SpreadView.spreadOf(0);
          const cells = (window.spreadEls || {})[s].querySelectorAll('.page-cell');
          return [cells[0].offsetWidth, cells[1].offsetWidth];
        })()"""
    )
    assert w1 > w0 * 1.5, (w0, w1)
    _assert_no_peek(page)
    # 1P mode: single-page spreads, still centred, still no peek.
    page.locator("#spread-toggle").click()
    page.wait_for_function("window.pageMode === 'single'")
    page.wait_for_function(
        """[...document.querySelectorAll('.ring-slot img')].every(
          i => i.classList.contains('loaded') && i.naturalWidth > 0)"""
    )
    c = _active_center(page)
    assert abs(c["off"]) <= 1, c
    _assert_no_peek(page)


def _assert_no_peek(page):
    """Every mounted non-anchor spread box is entirely outside the viewport."""
    assert page.evaluate(
        """(() => {
          const anchor = window.anchorSpread;
          return Object.entries(window.spreadEls || {}).every(([s, el]) => {
            if (Number(s) === anchor) return true;
            const r = el.getBoundingClientRect();
            return r.right <= 0 || r.left >= window.innerWidth;
          });
        })()"""
    ) is True


# ── 5. overview / zoom ──────────────────────────────────────────────────────

def test_overview_jump_allocates_only_three_spreads(ios_page, server):
    """Tapping a far overview cell exits to it (viewerSnapTo), closes the
    overlay, and the ring mounts only the bounded window — with the target
    actually in the viewport, not just reflected in the counter."""
    _ring_book(ios_page, server, folder=SERIES, p=0, total=5)
    ios_page.keyboard.press("g")
    ios_page.wait_for_function("window.Overview.isOpen()")
    cell = ios_page.locator("#overview .ov-cell[data-gidx]").nth(10)
    gidx = int(cell.get_attribute("data-gidx"))
    cell.scroll_into_view_if_needed()
    settle(ios_page)
    # A tap within INERTIA_MS of a scroll is "stopping momentum", not a
    # choice — let the just-issued scroll's async event age out first
    # (same convention as test_viewer_overview).
    ios_page.wait_for_function(
        "performance.now() - window.Overview._debug.lastScrollTs() > 150"
    )
    b = cell.bounding_box()
    s = touch_session(ios_page)
    tap(s, b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)
    s.detach()
    ios_page.wait_for_function("window.Overview.isOpen() === false")
    _wait_turn(ios_page, gidx)
    settle(ios_page)
    d = _dbg(ios_page)
    assert len(d["spreads"]) <= 3
    _assert_window(ios_page, gidx)
    assert _mounted_imgs(ios_page) <= 6
    # The target spread is actually in the viewport, not just the counter.
    assert ios_page.evaluate(
        """(() => {
          const s = window.SpreadView.spreadOf(window.currentIdx);
          const el = (window.spreadEls || {})[s];
          const r = el.getBoundingClientRect();
          return r.left < window.innerWidth && r.right > 0;
        })()"""
    ) is True


def test_direct_far_jump_cuts_to_bounded_window(ios_page, server):
    """viewerSnapTo far from the anchor must not animate across missing
    slots: the window stays ≤3 spreads and the target is in view at settle."""
    _ring_book(ios_page, server, p=1)
    ios_page.evaluate("window.viewerSnapTo(20, true)")   # animate requested
    _wait_turn(ios_page, 20)                             # ...but must cut
    d = _dbg(ios_page)
    assert len(d["spreads"]) <= 3
    _assert_window(ios_page, 20)
    assert ios_page.evaluate(
        """(() => {
          const s = window.SpreadView.spreadOf(20);
          const el = (window.spreadEls || {})[s];
          const r = el.getBoundingClientRect();
          return r.left < window.innerWidth && r.right > 0;
        })()"""
    ) is True
    # And a far jump back keeps the same bound.
    ios_page.evaluate("window.viewerSnapTo(2, true)")
    _wait_turn(ios_page, 2)
    assert len(_dbg(ios_page)["spreads"]) <= 3
    _assert_window(ios_page, 2)


def test_turn_resets_zoom_to_one(ios_page, server):
    _ring_book(ios_page, server, folder=SERIES, p=0, total=5)
    s = touch_session(ios_page)
    pinch(s, CX, 400, [40, 60, 80])
    s.detach()
    ios_page.wait_for_function("window.Zoom.state().zoom > 1.5")
    ios_page.keyboard.press("ArrowLeft")
    _wait_turn(ios_page, 1)
    assert ios_page.evaluate("window.Zoom.state().zoom") == 1


# ── 6. reset / book switch ──────────────────────────────────────────────────

def test_browse_return_resets_ring(ios_page, server):
    _ring_book(ios_page, server, p=0)
    assert _slots(ios_page) == 3
    ios_page.evaluate("document.querySelector('#menu-btn').click()")
    ios_page.wait_for_function("document.querySelectorAll('.ring-slot').length === 0")
    d = _dbg(ios_page)
    assert d["slotCount"] == 0
    assert d["cache"]["entries"] == 0


def test_stale_book_image_cannot_hide_new_spinner(page, server):
    """A detached old-book img at the SAME index whose queued `load` fires
    after the new book opened must not dismiss the new book's spinner —
    the snapTo completion is identity-guarded, not just index-guarded."""
    held_a, held_b = [], []
    page.route("**/image/long/**", lambda r: held_a.append(r))
    _ring_book(page, server, p=0)
    page.wait_for_function("document.querySelectorAll('.ring-slot').length === 3")
    for _ in range(50):
        if held_a:
            break
        page.wait_for_timeout(20)
    assert held_a
    # Save the OLD book's still-loading current img; hold book B's too so the
    # new spinner genuinely can't have resolved yet.
    page.evaluate("window.__oldCur = window.stripImgs[0]")
    assert page.evaluate("!!window.__oldCur")
    page.route(f"**/image/{MIXED_AR}/**", lambda r: held_b.append(r))
    page.evaluate(f"location.hash = '#/view/{MIXED_AR}?p=0'")
    page.wait_for_function(
        "window.currentFolder === 'mixed_ar' && "
        "!!window.stripImgs[0] && window.stripImgs[0] !== window.__oldCur"
    )
    assert page.evaluate(
        "document.querySelector('#img-container').classList.contains('loading')"
    ) is True
    # The stale queued completion arrives — must be a no-op for book B.
    page.evaluate("window.__oldCur.dispatchEvent(new Event('load'))")
    assert page.evaluate(
        "document.querySelector('#img-container').classList.contains('loading')"
    ) is True
    assert page.evaluate("JSON.stringify(window.pageAR)") == "{}"
    assert page.evaluate(
        "document.querySelectorAll('.ring-slot img[src*=\"/long/\"]').length"
    ) == 0
    for r in held_a + held_b:
        r.abort()
    page.unroute("**/image/long/**")
    page.unroute(f"**/image/{MIXED_AR}/**")


def test_stale_book_image_cannot_mutate_new_book(page, server):
    """An image delayed past a book switch must not touch the new book's
    AR data or DOM."""
    held = []

    def hold(route):
        held.append(route)

    page.route("**/image/long/**", hold)
    _ring_book(page, server, p=0)
    page.wait_for_function(
        "document.querySelectorAll('.ring-slot').length === 3"
    )
    for _ in range(50):
        if held:
            break
        page.wait_for_timeout(20)
    assert held, "book A's image requests must be in flight"
    # Switch books while A's images are still held.
    page.evaluate(f"location.hash = '#/view/{MIXED_AR}?p=0'")
    expect(page.locator("#counter")).to_have_text("1 / 6")
    page.wait_for_function("window.currentFolder === 'mixed_ar'")
    # Let the NEW book's own fetches/prefetches finish first — otherwise a
    # still-in-flight mixed_ar load legitimately adds pageAR entries and the
    # assertion can't tell it apart from a stale book-A completion.
    page.wait_for_function(
        "(() => { const c = window.__ringReaderDebug().cache; "
        "return c.entries > 0 && c.entries === c.ready.length; })()"
    )
    settle(page)
    ar_before = page.evaluate("JSON.stringify(window.pageAR)")
    for r in held:
        r.continue_()
    settle(page, 5)
    assert page.evaluate("JSON.stringify(window.pageAR)") == ar_before
    # No book-A image may be mounted anywhere in the ring.
    assert page.evaluate(
        "document.querySelectorAll('.ring-slot img[src*=\"/long/\"]').length"
    ) == 0
    page.unroute("**/image/long/**")


# ── 7. error / offline URL ─────────────────────────────────────────────────

def test_current_image_error_stops_spinner_with_warning(page, server):
    # Abort BEFORE open: the ring window pins s+1 up front, so p002's fetch is
    # already in flight (and fails) by the time the turn lands on it.
    page.route("**/image/long/p002.png", lambda r: r.abort())
    _ring_book(page, server, p=0)
    page.keyboard.press("ArrowLeft")
    page.wait_for_function(
        "window.currentIdx === 1 && "
        "document.querySelector('#counter').textContent.includes('⚠')"
    )
    assert page.evaluate(
        "document.querySelector('#spinner').style.display"
    ) == "none"
    assert page.evaluate(
        "document.querySelector('#img-container').classList.contains('loading')"
    ) is False
    page.unroute("**/image/long/p002.png")


def test_failed_image_index_reported_not_ready(page, server):
    """debug() must not lie about readiness: an aborted fetch is `failed`,
    never `ready` or `decoded`."""
    page.route("**/image/long/p003.png", lambda r: r.abort())
    _ring_book(page, server, p=0)
    page.wait_for_function(
        "window.__ringReaderDebug().cache.failed.indexOf(2) >= 0"
    )
    c = _dbg(page)["cache"]
    assert 2 in c["failed"]
    assert 2 not in c["ready"]
    assert 2 not in c["decoded"]
    page.unroute("**/image/long/p003.png")


def test_ring_urls_use_offlinecore_verbatim(page, server):
    """Unicode/space paths must resolve to the exact OfflineCore URL — the
    cache must not carry its own encoder."""
    _ring_book(page, server, folder=UNICODE, p=0, total=1)
    got = page.evaluate(
        """(() => {
          const img = document.querySelector('.ring-slot img');
          return img ? { src: img.getAttribute('src'), want: window.getUrl(0) } : null;
        })()"""
    )
    assert got is not None
    assert got["src"] == got["want"]
    assert got["src"].startswith("/image/")


# ── 8. cache-focused unit behaviour ─────────────────────────────────────────

def test_reader_image_cache_pin_lru_and_clear(page, server):
    """Bounded cache contract: pinned survive, unpinned evict LRU-first,
    clear() invalidates pending completions."""
    page.goto(f"{server}/?reader=ring")
    # Module loads on demand at viewer-open; inject it directly —
    # these tests exercise the cache API, not the load path.
    page.add_script_tag(url=f"{server}/static/viewer/ringCache.js")
    page.wait_for_function("!!window.ReaderImageCache")
    out = page.evaluate(
        """async () => {
          const mk = (opts) => window.ReaderImageCache.create(Object.assign({
            urlFor: i => "/static/icons/icon-192.png?i=" + i,
          }, opts));
          const loads = (img) => new Promise(res => {
            img.addEventListener("load", () => res(true));
            img.addEventListener("error", () => res(false));
          });
          const res = {};

          // LRU eviction by entry cap, with touch() refreshing recency.
          const c1 = mk({ maxEntries: 3, maxBytes: 1 << 30 });
          const i1 = c1.ensure(1);
          c1.ensure(0); c1.ensure(2);          // order: 1,0,2
          c1.ensure(0);                        // touch → order 1,2,0
          c1.ensure(3);                        // cap 3 → evict oldest = idx1
          res.lruAfter = c1.debug().indices.join(",") === "2,0,3";
          const i1b = c1.ensure(1);            // re-create after eviction
          res.evictedIsNew = i1b !== i1;

          // Byte budget + pinned protection. icon-192 = 192*192*4 = 147456 B.
          const B = 192 * 192 * 4;
          const c2 = mk({ maxEntries: 10, maxBytes: 2 * B });
          const a = c2.ensure(0), b = c2.ensure(1);
          c2.pin([0, 1]);
          await Promise.all([loads(a), loads(b)]);
          const x = c2.ensure(2);              // unpinned, over byte budget
          await loads(x);
          const d2 = c2.debug();
          res.pinnedSurvive = c2.ensure(0) === a && c2.ensure(1) === b;
          res.unpinnedEvicted = d2.entries === 2 && c2.ensure(2) !== x;
          res.bytesBounded = d2.bytes === 2 * B && d2.pinnedBytes === 2 * B;

          // Pinned entries are never evicted, even over budget alone.
          const c3 = mk({ maxEntries: 10, maxBytes: B / 2 });
          const p0 = c3.ensure(0);
          c3.pin([0]);
          await loads(p0);
          const d3 = c3.debug();
          res.pinnedOverBudgetKept = c3.ensure(0) === p0 && d3.entries === 1;

          // clear() invalidates outstanding completions — a stale load can
          // neither resurrect an entry nor fire onAspect.
          let aspectCalls = 0;
          const c4 = mk({ onAspect: () => { aspectCalls++; } });
          const stale = c4.ensure(0);
          c4.clear();
          stale.dispatchEvent(new Event("load"));   // stale completion
          await new Promise(r => setTimeout(r, 50)); // real fetch may also land
          res.clearBlocksStale = c4.debug().entries === 0 && aspectCalls === 0;

          // A network failure is terminal for the page but neither ready
          // nor decoded — `failed` carries the outcome.
          const c6 = mk({ urlFor: () => "/definitely-not-here.png" });
          const bad = c6.ensure(7);
          await loads(bad);
          const d6 = c6.debug();
          res.failedNotReady = d6.failed.indexOf(7) >= 0
            && d6.ready.indexOf(7) < 0 && d6.decoded.indexOf(7) < 0;

          // No decode() API → decoded stays unknown (false), never claimed.
          const origDecode = HTMLImageElement.prototype.decode;
          HTMLImageElement.prototype.decode = undefined;
          try {
            const c7 = mk({});
            const im7 = c7.ensure(0);
            await loads(im7);
            const d7 = c7.debug();
            res.noDecodeStaysUnknown = d7.ready.indexOf(0) >= 0
              && d7.decoded.indexOf(0) < 0;
          } finally {
            HTMLImageElement.prototype.decode = origDecode;
          }

          // canPrefetch admission: a NEW far entry is refused while pinned
          // pages are cold, when the pin set fills the cap, or when pinned
          // bytes + the largest known entry would exceed the budget.
          const c8 = mk({ maxEntries: 10, maxBytes: 4 * B });
          const w0 = c8.ensure(0), w1 = c8.ensure(1);
          c8.pin([0, 1]);
          res.admitColdPins = c8.canPrefetch(9) === false;
          await Promise.all([loads(w0), loads(w1)]);
          res.admitWarmRoom = c8.canPrefetch(9) === true;        // 2B+B <= 4B
          res.admitExisting = c8.canPrefetch(0) === true;

          const c9 = mk({ maxEntries: 10, maxBytes: 2 * B });
          const q0 = c9.ensure(0), q1 = c9.ensure(1);
          c9.pin([0, 1]);
          await Promise.all([loads(q0), loads(q1)]);
          res.admitBudgetFull = c9.canPrefetch(9) === false;     // 2B+B > 2B
          // A refused admission allocates nothing — the pins stay put.
          res.admitPinsKept = c9.debug().entries === 2;

          const c10 = mk({ maxEntries: 2, maxBytes: 1 << 30 });
          const r0 = c10.ensure(0), r1 = c10.ensure(1);
          c10.pin([0, 1]);
          await Promise.all([loads(r0), loads(r1)]);
          res.admitPinCap = c10.canPrefetch(9) === false;        // pinned.size >= cap
          res.admitCapExisting = c10.canPrefetch(0) === true;

          res.noUrlsInDebug = !Object.values(c2.debug()).some(v =>
            typeof v === "string" && v.indexOf("/") >= 0);
          return res;
        }"""
    )
    for k, v in out.items():
        assert v is True, f"{k}: {v}"


# ── 9. review-driven cache/commit regressions ────────────────────────────────

def test_cache_admission_counts_unpinned_and_inflight(page, server):
    """canPrefetch judged one page at a time and accepted prefetches that
    trim() then evicted back out. Admission must project the whole unpinned
    set — in-flight entries still at bytes=0 estimate at the largest known
    size — plus the new entry."""
    page.goto(f"{server}/?reader=ring")
    # Module loads on demand at viewer-open; inject it directly —
    # these tests exercise the cache API, not the load path.
    page.add_script_tag(url=f"{server}/static/viewer/ringCache.js")
    page.wait_for_function("!!window.ReaderImageCache")
    out = page.evaluate(
        """async () => {
          const mk = (opts) => window.ReaderImageCache.create(Object.assign({
            urlFor: i => "/static/icons/icon-192.png?i=" + i,
          }, opts));
          const loads = (img) => new Promise(res => {
            img.addEventListener("load", () => res(true));
            img.addEventListener("error", () => res(false));
          });
          const B = 192 * 192 * 4;
          const res = {};

          // pinned 2B warm, two unpinned entries still in flight: the new
          // page projects 2B + 2B + B > 4B — must refuse (old code: 2B+B
          // <= 4B admitted, then trim() threw the just-fetched entry out).
          const c = mk({ maxEntries: 10, maxBytes: 4 * B });
          const z0 = c.ensure(0), z1 = c.ensure(1);
          c.pin([0, 1]);
          await Promise.all([loads(z0), loads(z1)]);
          c.ensure(2); c.ensure(3);            // unpinned, still in flight
          res.projectedRefusal = c.canPrefetch(4) === false;
          // One unpinned in flight still fits: 2B + B + B = 4B <= 4B.
          const c2 = mk({ maxEntries: 10, maxBytes: 4 * B });
          const y0 = c2.ensure(0), y1 = c2.ensure(1);
          c2.pin([0, 1]);
          await Promise.all([loads(y0), loads(y1)]);
          c2.ensure(2);
          res.fittingAdmit = c2.canPrefetch(4) === true;
          return res;
        }"""
    )
    for k, v in out.items():
        assert v is True, f"{k}: {v}"


def test_cache_failed_entry_retries_on_next_ensure(page, server):
    """A failed PINNED page must not stay broken until the reader walks two
    spreads away — ensure() on a failed pinned entry rebuilds and refetches.
    Unpinned failures must NOT retry (far-prefetch misses don't churn)."""
    page.goto(f"{server}/?reader=ring")
    # Module loads on demand at viewer-open; inject it directly —
    # these tests exercise the cache API, not the load path.
    page.add_script_tag(url=f"{server}/static/viewer/ringCache.js")
    page.wait_for_function("!!window.ReaderImageCache")
    out = page.evaluate(
        """async () => {
          const loads = (img) => new Promise(res => {
            img.addEventListener("load", () => res(true));
            img.addEventListener("error", () => res(false));
          });
          const c = window.ReaderImageCache.create({
            urlFor: () => "/definitely-not-here.png" });
          c.pin([0]);
          const first = c.ensure(0);
          await loads(first);
          const failedMarked = c.debug().failed.indexOf(0) >= 0;
          const second = c.ensure(0);      // pinned retry → NEW element
          const pinnedRetried = second !== first && c.debug().entries === 1;
          await loads(second);
          const c2 = window.ReaderImageCache.create({
            urlFor: () => "/definitely-not-here.png" });
          const u0 = c2.ensure(3);         // unpinned failure
          await loads(u0);
          const u1 = c2.ensure(3);
          return { failedMarked, pinnedRetried,
                   unpinnedKept: u1 === u0 };
        }"""
    )
    for k, v in out.items():
        assert v is True, f"{k}: {v}"


def test_prefetch_resumes_once_pinned_neighbour_loads(page, server):
    """A pinned neighbour still loading makes canPrefetch refuse — and
    nothing re-asked until the next turn, so far prefetch silently stalled.
    The pin set reporting ready must reschedule the refused prefetch."""
    held = []
    page.route("**/image/long/p002.png", lambda r: held.append(r))
    _ring_book(page, server, p=0)
    page.wait_for_function("document.querySelectorAll('.ring-slot').length >= 2")
    for _ in range(50):
        if held:
            break
        page.wait_for_timeout(20)
    assert held, "neighbour fetch must be in flight"
    # Portrait single-page: pinned window = pages {0,1}; far prefetch wants
    # idx 2 (spread s+2). While p002 (idx 1) is held it must NOT be fetched…
    page.wait_for_timeout(200)            # let the prefetch path run+refuse
    assert 2 not in page.evaluate("window.__ringReaderDebug().cache.indices")
    for r in held:
        r.continue_()
    page.unroute("**/image/long/p002.png")
    # …and once the pin set reports ready the retry must land it.
    page.wait_for_function(
        "window.__ringReaderDebug().cache.indices.indexOf(2) >= 0")


def test_regroup_during_ring_commit_still_lands_turn(page, server):
    """regroupCells used to cancelRingSwipe() — a rotation/split-screen
    resize inside the 250ms commit window silently ate the reader's turn.
    It must finish the commit first, like strip's surviving finalize timer."""
    _ring_book(page, server, p=4)
    s = touch_session(page)
    drag(s, 200, 450, 330, 450)          # forward swipe → commit in flight
    s.detach()
    # The regroup MUST run inside the ~260ms commit window — if the fallback
    # timer already finalized, currentIdx===5 holds even for the old
    # cancelRingSwipe() code and this test proves nothing. Assert pending
    # and invoke the interrupt in ONE task (a resize's own route is
    # window resize → updateOrientation → regroupCells; calling it in-page
    # removes the CDP round-trip that could outlast the window).
    page.evaluate(
        """() => {
          if (!window.__ringCommitPending || !window.__ringCommitPending())
            throw new Error('ring commit already finalized before regroup');
          window.__orientDebug.regroup();
        }"""
    )
    page.wait_for_function("window.currentIdx === 5", timeout=3000)
    page.wait_for_function("window.__ringCommitPending() === false")


def test_touch_during_ring_commit_rebases_anchor(ios_page, server):
    """A touch landing inside a pending ring commit used to finish the turn
    while anchorSpread stayed on the pre-slide spread — corrupting the pinch
    measureBox and the next tap's jump-distance check. armDrag must re-base."""
    page = ios_page
    _ring_book(page, server, p=1)
    s = touch_session(page)
    drag(s, 200, 450, 330, 450)          # forward swipe → commit pending
    s.detach()
    # Same interleave guarantee as the regroup test — and atomic: the
    # pending assert and the touch dispatch share one JS task, so no CDP
    # round-trip can outlast the commit window. A synthetic touchstart on
    # #img-container runs the app's real listener chain (armDrag →
    # finishRingSwipe + rebaseAnchor); the touchend mirrors a centre tap —
    # arms a drag, turns nothing.
    page.evaluate(
        """() => {
          if (!window.__ringCommitPending || !window.__ringCommitPending())
            throw new Error('ring commit already finalized before touch');
          const el = document.getElementById('img-container');
          const r = el.getBoundingClientRect();
          const mk = () => new Touch({identifier: 7, target: el,
            clientX: r.left + r.width / 2, clientY: r.top + r.height / 2});
          el.dispatchEvent(new TouchEvent('touchstart',
            {touches: [mk()], targetTouches: [mk()], changedTouches: [mk()],
             bubbles: true, cancelable: true}));
          el.dispatchEvent(new TouchEvent('touchend',
            {touches: [], changedTouches: [mk()],
             bubbles: true, cancelable: true}));
        }"""
    )
    page.wait_for_function(
        "window.currentIdx === 2 && "
        "window.anchorSpread === window.SpreadView.spreadOf(2)")
    # …and the property that actually matters: the box a pinch would capture
    # via Zoom.measureBox() (keyed on anchorSpread) is the CURRENT spread's
    # rect, not the pre-commit neighbour's.
    box = page.evaluate("window.__zoomDebug.measureBox()")
    rect = page.evaluate(
        """(() => {
          const c = document.getElementById('img-container')
                            .getBoundingClientRect();
          const r = window.spreadEls[window.SpreadView.spreadOf(
                            window.currentIdx)].getBoundingClientRect();
          return {x: r.left - c.left, w: r.width};
        })()"""
    )
    assert abs(box["x"] - rect["x"]) < 2 and abs(box["w"] - rect["w"]) < 2


def test_chapter_crossing_tap_rebinds_window(page, server):
    """A next-zone tap on a chapter's last page crosses into the next
    chapter (tap zones use crossChapter:true). The ring must land there —
    chapter info, bounded window and viewport position all consistent."""
    _ring_book(page, server, folder=SERIES, p=4, total=5)   # ep-1 last page
    assert page.evaluate(
        "window.getCurrentChapterInfo(window.currentIdx).chapterIdx") == 0
    s = touch_session(page)
    tap(s, LX, Y)                        # next-zone tap (tapForward='left')
    s.detach()
    # ep-2's first page — the global idx is chapter-local numbering plus
    # chapter-boundary spacers, so key on chapterIdx instead of a literal.
    page.wait_for_function(
        "window.getCurrentChapterInfo(window.currentIdx).chapterIdx === 1")
    settle(page)
    idx = page.evaluate("window.currentIdx")
    _assert_window(page, idx)
    assert _mounted_imgs(page) <= 6
    assert page.evaluate(
        """(() => {
          const s = window.SpreadView.spreadOf(window.currentIdx);
          const r = (window.spreadEls || {})[s].getBoundingClientRect();
          return r.left < window.innerWidth && r.right > 0;
        })()"""
    ) is True


def test_chapter_boundary_swipe_does_not_cross(page, server):
    """Swipes clamp to the chapter range — only taps cross. A forward swipe
    on a chapter's last page must bounce back: no turn, no commit left
    pending, window still bound around the same spread."""
    _ring_book(page, server, folder=SERIES, p=4, total=5)
    s = touch_session(page)
    drag(s, 200, 450, 330, 450)          # forward swipe at chapter end
    s.detach()
    settle(page)
    assert page.evaluate("window.currentIdx") == 4
    assert page.evaluate("window.__ringCommitPending()") is False
    _assert_window(page, 4)


# ── 7. harness toggle / flag precedence ─────────────────────────────────────

def _clear_reader_flag(page):
    page.evaluate("try { localStorage.removeItem('bookq_reader'); } catch(e) {}")


def test_index_document_is_reader_flag_invariant(page, server):
    """The ring modules load client-side on demand — the served '/' must be
    one identical document for every flag state, or the SW-precached copy
    could silently disagree with the client's reader decision offline."""
    a = page.request.get(server + "/").text()
    b = page.request.get(server + "/?reader=ring").text()
    c = page.request.get(
        server + "/", headers={"Cookie": "bookq_reader=ring"}).text()
    assert a == b == c
    assert "viewer/ring" not in a


def test_reader_localstorage_flag_and_param_precedence(ios_page, server):
    """The harness §7 toggle sets localStorage bookq_reader=ring — a purely
    client-side flag (the ring modules load on demand; the document served
    never varies). No reader param → flag selects ring; an explicit
    ?reader= param wins both ways so measurement URLs stay deterministic —
    and ?reader=strip must not even REQUEST the ring modules."""
    page = ios_page
    _clear_reader_flag(page)
    page.evaluate("localStorage.setItem('bookq_reader', 'ring')")
    # (?_=1 is a dummy; only 'reader' is looked at, so this stays the
    # "no reader param" path.)
    page.goto(f"{server}/?_=1#/view/{FOLDER}?p=0")
    expect(page.locator("#counter")).to_have_text("1 / 30")
    assert page.evaluate("window.__bookqReader") == "ring"
    assert _slots(page) == 3
    # An explicit param overrides the flag both ways.
    seen = []
    page.on(
        "request",
        lambda r: seen.append(r.url) if r.resource_type == "script" else None,
    )
    page.goto(f"{server}/?reader=strip#/view/{FOLDER}?p=1")
    expect(page.locator("#counter")).to_have_text("2 / 30")
    assert page.evaluate("window.__bookqReader") == "strip"
    assert _slots(page) == 0
    assert not any("ring" in u for u in seen), seen   # strip stays lean
    page.goto(f"{server}/?reader=ring#/view/{FOLDER}?p=1")
    expect(page.locator("#counter")).to_have_text("2 / 30")
    assert page.evaluate("window.__bookqReader") == "ring"
    _clear_reader_flag(page)


def test_harness_ring_toggle_enables_ring_next_visit(ios_page, server):
    """The real flow: toggle ring ON in the harness, return via the title
    link, open a book — the client-side flag loads the ring modules on
    demand and the viewer comes up ring without any URL param."""
    page = ios_page
    page.goto(server + "/static/offline-harness.html?harness=1")
    page.locator("#btn-ring-toggle").click()
    expect(page.locator("#harness-ring-status")).to_contain_text("reader: ring")
    page.locator("header h1 a").click()          # title → tile view
    page.wait_for_selector("#tile-grid .tile", timeout=10000)
    # In-app hash navigation reuses this document; the flag was read at
    # load and the modules are fetched on first open (SW-precached, so
    # this path works offline too).
    page.evaluate("location.hash = '#/view/long?p=0'")
    expect(page.locator("#counter")).to_have_text("1 / 30")
    assert page.evaluate("window.__bookqReader") == "ring"
    assert _slots(page) == 3
    _clear_reader_flag(page)


def test_harness_ring_toggle_on_off(ios_page, server):
    """Harness §7's ring toggle writes/clears localStorage bookq_reader and
    reflects the current state in its badge — same shape as the perf HUD
    toggle it sits next to."""
    page = ios_page
    page.goto(server + "/static/offline-harness.html?harness=1")
    page.wait_for_selector("#harness-ring-status", timeout=10000)
    expect(page.locator("#harness-ring-status")).to_contain_text("reader: strip")
    page.locator("#btn-ring-toggle").click()
    expect(page.locator("#harness-ring-status")).to_contain_text("reader: ring")
    assert page.evaluate("localStorage.getItem('bookq_reader')") == "ring"
    page.locator("#btn-ring-toggle").click()
    expect(page.locator("#harness-ring-status")).to_contain_text("reader: strip")
    assert page.evaluate("localStorage.getItem('bookq_reader')") is None


def test_harness_title_links_back_to_tiles(ios_page, server):
    """The header title is the way back — it must navigate to the app's
    tile grid (the harness page has no other visible exit at the top)."""
    page = ios_page
    page.goto(server + "/static/offline-harness.html?harness=1")
    link = page.locator("header h1 a")
    assert link.get_attribute("href") == "/"
    link.click()
    page.wait_for_selector("#tile-grid .tile", timeout=10000)
