"""S06 — one page per device from a shared committed spread (#81).

The paired renderer consumes ``ViewerNavigation.applyCommittedState`` input
and projects the SHARED spread to one device-side page via
``PairModel.projectSpread`` (leader -> ``leaderSide``, follower -> the
opposite; SWAP flips ``leaderSide`` only, ``spreadId`` stays). Each device
renders exactly its own PageRef — never the peer's, never both. A blank
side renders a distinguishable empty state (``pair-blank`` class, zero
<img> in the display), not a stale image.

Manifest/state fixtures are injected — no transport, no WS. All asserts are
DOM/src state; screenshot marker matching is S10's job (#85).
"""

import os
import sys
from pathlib import Path

import pytest
from playwright.sync_api import expect

os.environ["BOOKQ_TEST_MODE"] = "true"

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from app.reader_sync import model  # noqa: E402
from viewer_touch import open_book, pinch, settle, touch_session  # noqa: E402


def _manifest(folder="long", count=30, offset=0):
    """S04-shaped wire manifest ({v, chapters, pageRefs, spreadTable, digest})."""
    names = [f"p{i:03d}.png" for i in range(1, count + 1)]
    plan = model.build_spread_plan([{"folder": folder, "pages": names}], offset)
    return {
        "v": 1,
        "folder": folder,
        "offset": offset,
        "chapters": [
            {
                "name": folder,
                "folder": folder,
                "pages": [{"folder": folder, "name": n} for n in names],
            }
        ],
        "pageRefs": [{"folder": folder, "name": n} for n in names],
        "spreadTable": [model.serialize_spread(s) for s in plan],
    }


def _snapshot(spread_id, leader_side="right", revision=1, phase="reading"):
    return {
        "v": 1,
        "type": "STATE",
        "sessionId": "sess-1",
        "revision": revision,
        "workEpoch": 0,
        "manifestVersion": 1,
        "phase": phase,
        "spreadId": spread_id,
        "offset": 0,
        "leaderSide": leader_side,
        "presence": {},
    }


def _commit(page, snapshot, manifest, role, **options):
    """Inject a committed shared state through the S02 receive-only channel."""
    opts = {"role": role}
    opts.update(options)
    return page.evaluate(
        "([snap, man, opts]) => window.ViewerNavigation.applyCommittedState(snap, man, opts)",
        [snapshot, manifest, opts],
    )


def _visible_srcs(page):
    """src of every #img-track <img> whose box intersects the viewport."""
    return page.evaluate(
        """() => [...document.querySelectorAll('#img-track img')]
          .filter(i => { const r = i.getBoundingClientRect();
            return r.width > 0 && r.height > 0
              && r.right > 0 && r.left < window.innerWidth
              && r.bottom > 0 && r.top < window.innerHeight; })
          .map(i => i.getAttribute('src'))"""
    )


def _center_img(page):
    """{src,w} of the <img> covering the viewport centre — the displayed page."""
    return page.evaluate(
        """() => {
          const cx = window.innerWidth / 2, cy = window.innerHeight / 2;
          const i = [...document.querySelectorAll('#img-track img')].find(el => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.left <= cx && r.right >= cx
              && r.top <= cy && r.bottom >= cy;
          });
          if (!i) return null;
          const r = i.getBoundingClientRect();
          return {src: i.getAttribute('src'), w: r.width}; }"""
    )


def _state(page):
    return page.evaluate("window.PairedView.state()")


def _loaded_ready(page):
    page.wait_for_function(
        """() => { const i = document.querySelector('#img-track img');
                  return i && i.complete && i.classList.contains('loaded'); }"""
    )


# ---------------------------------------------------------------------------
# 2 independent contexts: leader -> right, follower -> left
# ---------------------------------------------------------------------------

def test_two_contexts_render_assigned_sides(browser, server):
    man = _manifest("long", 30, 0)          # c0:s2 = {right:p005, left:p006}
    snap = _snapshot("c0:s2")
    ctx_a = browser.new_context(viewport={"width": 390, "height": 844})
    ctx_b = browser.new_context(viewport={"width": 390, "height": 844})
    pa, pb = ctx_a.new_page(), ctx_b.new_page()
    try:
        for p in (pa, pb):
            p.goto(server)
            p.wait_for_selector(".tile")
        _commit(pa, snap, man, "leader")
        _commit(pb, snap, man, "follower")
        settle(pa, 3)
        settle(pb, 3)
        _loaded_ready(pa)
        _loaded_ready(pb)

        # one and only one page image on each device, and it is the ASSIGNED one
        assert _visible_srcs(pa) == ["/image/long/p005.png"]
        assert _visible_srcs(pb) == ["/image/long/p006.png"]
        sta, stb = _state(pa), _state(pb)
        assert sta["spreadId"] == "c0:s2" == stb["spreadId"]
        assert sta["side"] == "right" and sta["ref"] == {"folder": "long", "name": "p005.png"}
        assert stb["side"] == "left" and stb["ref"] == {"folder": "long", "name": "p006.png"}
        # URL is exactly OfflineCore.imageUrl output
        assert pa.evaluate("window.OfflineCore.imageUrl('long','p005.png')") == "/image/long/p005.png"
        # peer page must not leak into DOM: follower strip holds left pages only
        srcs = pb.evaluate(
            "[...document.querySelectorAll('#img-track img')].map(i => i.getAttribute('src'))"
        )
        assert srcs, "expected prefetched own cells"
        for s in srcs:
            name = s.rsplit("/", 1)[-1]
            assert int(name[1:4]) % 2 == 0, f"peer page leaked into follower DOM: {s}"
    finally:
        ctx_a.close()
        ctx_b.close()


def test_swap_changes_side_not_spread_id(page, server):
    page.goto(server)
    page.wait_for_selector(".tile")
    man = _manifest("long", 30, 0)
    _commit(page, _snapshot("c0:s0", "right", 1), man, "leader")
    settle(page, 3)
    _loaded_ready(page)
    assert _visible_srcs(page) == ["/image/long/p001.png"]

    # SWAP: same spreadId, leaderSide flips -> leader now renders the left page
    _commit(page, _snapshot("c0:s0", "left", 2), man, "leader")
    settle(page, 3)
    _loaded_ready(page)
    assert _visible_srcs(page) == ["/image/long/p002.png"]
    st = _state(page)
    assert st["spreadId"] == "c0:s0"
    assert st["leaderSide"] == "left" and st["side"] == "left"
    assert st["ref"] == {"folder": "long", "name": "p002.png"}
    # the old (right) page must be gone from the DOM entirely
    assert page.evaluate(
        "[...document.querySelectorAll('#img-track img')]"
        ".some(i => i.getAttribute('src').includes('p001'))"
    ) is False


def test_rotation_preserves_spread_id_revision_and_ref(page, server):
    page.goto(server)
    page.wait_for_selector(".tile")
    man = _manifest("long", 30, 0)
    _commit(page, _snapshot("c0:s4", "right", 7), man, "leader")
    settle(page, 3)
    _loaded_ready(page)
    assert _visible_srcs(page) == ["/image/long/p009.png"]
    before = _state(page)

    page.set_viewport_size({"width": 844, "height": 390})
    page.wait_for_function("document.body.className === 'landscape'")
    settle(page, 3)
    after = _state(page)
    assert after["spreadId"] == before["spreadId"] == "c0:s4"
    assert after["revision"] == before["revision"] == 7
    assert after["ref"] == before["ref"] == {"folder": "long", "name": "p009.png"}
    # still exactly ONE page image in the landscape viewport
    assert _visible_srcs(page) == ["/image/long/p009.png"]

    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_function("document.body.className === 'portrait'")
    settle(page, 3)
    assert _state(page)["ref"] == before["ref"]
    assert _visible_srcs(page) == ["/image/long/p009.png"]


def test_blank_side_zero_img_distinguishable_no_stale_dom(page, server):
    page.goto(server)
    page.wait_for_selector(".tile")
    man = _manifest("long", 30, 1)   # offset=1 -> c0:s0 = {right:p001, left:null}
    # follower renders the (null) left side of the first spread
    _commit(page, _snapshot("c0:s0"), man, "follower")
    settle(page, 4)
    # zero images displayed, none peeking at the viewport edge
    assert _visible_srcs(page) == []
    st = _state(page)
    assert st["ref"] is None and st["blank"] is True and st["side"] == "left"
    # distinguishable blank state: own class, NOT spinner, NOT error
    assert page.evaluate(
        "document.getElementById('img-container').classList.contains('pair-blank')"
    ) is True
    assert page.evaluate(
        "document.querySelectorAll('#img-track .page-cell.pair-blank').length"
    ) >= 1
    assert page.evaluate(
        "getComputedStyle(document.getElementById('spinner')).display"
    ) == "none"
    assert "⚠" not in page.locator("#counter").inner_text()
    assert "空白" in page.locator("#counter").inner_text()
    # the blank cell itself holds no <img> and no stale src
    assert page.evaluate(
        "[...document.querySelectorAll('#img-track .page-cell.pair-blank img')].length"
    ) == 0
    # current spread container carries no leftover image DOM/src at all
    cur = page.evaluate(
        """() => { const tr = document.getElementById('img-track');
            for (const el of tr.querySelectorAll('.spread-container.pair-blank')) {
              if (el.querySelector('img') || el.querySelector('[src]')) return false;
            }
            return true; }"""
    )
    assert cur is True
    # ...while the LEADER on the same spread sees the real right page
    _commit(page, _snapshot("c0:s0", revision=2), man, "leader")
    settle(page, 3)
    _loaded_ready(page)
    assert _visible_srcs(page) == ["/image/long/p001.png"]


def test_move_to_next_shared_spread(page, server):
    page.goto(server)
    page.wait_for_selector(".tile")
    man = _manifest("long", 30, 0)
    _commit(page, _snapshot("c0:s0", "right", 1), man, "follower")
    settle(page, 3)
    _loaded_ready(page)
    assert _visible_srcs(page) == ["/image/long/p002.png"]
    # committed turn: next spread, same revision semantics
    _commit(page, _snapshot("c0:s1", "right", 2), man, "follower")
    settle(page, 3)
    _loaded_ready(page)
    assert _visible_srcs(page) == ["/image/long/p004.png"]
    assert _state(page)["spreadId"] == "c0:s1"


def test_stale_load_event_cannot_overwrite_new_assignment(page, server):
    page.goto(server)
    page.wait_for_selector(".tile")
    man = _manifest("long", 30, 0)
    _commit(page, _snapshot("c0:s0", "right", 1), man, "leader")
    settle(page, 3)
    _loaded_ready(page)
    old_img = page.evaluate_handle("document.querySelector('#img-track .page-cell img')")
    assert _visible_srcs(page) == ["/image/long/p001.png"]

    # swap assignment p001 -> p002; then fire a stale load on the OLD element
    _commit(page, _snapshot("c0:s0", "left", 2), man, "leader")
    settle(page, 3)
    assert _visible_srcs(page) == ["/image/long/p002.png"]
    page.evaluate("el => el && el.dispatchEvent(new Event('load'))", old_img)
    page.evaluate("el => el && el.dispatchEvent(new Event('error'))", old_img)
    settle(page, 2)
    assert _visible_srcs(page) == ["/image/long/p002.png"]
    assert _state(page)["ref"] == {"folder": "long", "name": "p002.png"}


def test_image_url_and_contain_fit(page, server):
    page.goto(server)
    page.wait_for_selector(".tile")
    man = _manifest("long", 30, 0)
    _commit(page, _snapshot("c0:s0"), man, "leader")
    settle(page, 3)
    _loaded_ready(page)
    src = page.evaluate("document.querySelector('#img-track img').getAttribute('src')")
    assert src == page.evaluate("window.OfflineCore.imageUrl('long','p001.png')")
    # contain-fit equivalence: cell box = min(viewW, AR*viewH) same rule as solo
    # fixture pages are 40x60 (AR 2/3): viewW=390 -> h=585, w=390 (width-bound)
    box = page.evaluate(
        "() => { const c = document.querySelector('#img-track .page-cell');"
        " const r = c.getBoundingClientRect(); return {w: r.width, h: r.height}; }"
    )
    assert abs(box["w"] - 390) < 1.5
    assert abs(box["h"] - 585) < 2.5
    # existing object-fit DOM contract kept: img fills its cell
    assert page.evaluate(
        "() => { const i = document.querySelector('#img-track img');"
        " const r = i.getBoundingClientRect(), c = i.closest('.page-cell').getBoundingClientRect();"
        " return Math.abs(r.width - c.width) < 1 && Math.abs(r.height - c.height) < 1; }"
    ) is True


def test_prefetch_window_limit(page, server):
    page.goto(server)
    page.wait_for_selector(".tile")
    man = _manifest("long", 30, 0)
    _commit(page, _snapshot("c0:s5"), man, "leader")
    settle(page, 4)
    _loaded_ready(page)
    n = page.evaluate("document.querySelectorAll('#img-track img').length")
    assert n <= 12, f"paired strip must stay inside the existing prefetch window, got {n}"
    # every live img belongs to THIS device (right side = odd page numbers)
    srcs = page.evaluate(
        "[...document.querySelectorAll('#img-track img')].map(i => i.getAttribute('src'))"
    )
    for s in srcs:
        assert int(s.rsplit("/", 1)[-1][1:4]) % 2 == 1


def test_zoom_pan_local_only(ios_page, server):
    page = ios_page
    page.goto(server)
    page.wait_for_selector(".tile")
    man = _manifest("long", 30, 0)
    _commit(page, _snapshot("c0:s2"), man, "follower")   # own page p006 (left)
    settle(page, 3)
    _loaded_ready(page)
    before = _center_img(page)
    assert before["src"].endswith("p006.png")

    s = touch_session(page)
    cx = page.viewport_size["width"] // 2
    cy = page.viewport_size["height"] // 2
    pinch(s, cx, cy, [40, 55, 70, 80])   # ratio 2.0
    page.wait_for_function("window.Zoom.state().zoom > 1.5")
    settle(page, 2)

    tf = page.evaluate("document.getElementById('zoom-layer').style.transform")
    assert "scale(" in tf
    after = _center_img(page)
    # own page grew; still the same assigned PageRef, still the only displayed page
    assert after["src"].endswith("p006.png")
    assert after["w"] > before["w"] * 1.4
    # and there is no second DOM image the transform could leak onto
    assert _visible_srcs(page) == ["/image/long/p006.png"]


def test_overview_current_points_at_displayed_ref(page, server):
    page.goto(server)
    page.wait_for_selector(".tile")
    man = _manifest("long", 30, 0)
    _commit(page, _snapshot("c0:s2"), man, "follower")   # own p006 (idx5), anchor p005 (idx4)
    settle(page, 4)
    page.evaluate("window.Overview.open()")
    page.wait_for_selector("#overview .ov-cell.cur", state="attached")
    gidx = int(page.evaluate("document.querySelector('#overview .ov-cell.cur').dataset.gidx"))
    # overview .cur = DISPLAYED PageRef (p006, idx 5), not the anchor p005 (idx4)
    assert gidx == 5
    assert page.evaluate("window.images[5].name") == "p006.png"


def test_decode_and_error_states(page, server):
    page.goto(server)
    page.wait_for_selector(".tile")
    man = _manifest("long", 30, 0)
    _commit(page, _snapshot("c0:s0"), man, "leader")
    _loaded_ready(page)
    assert page.evaluate(
        "getComputedStyle(document.getElementById('spinner')).display"
    ) == "none"
    assert page.evaluate(
        "document.getElementById('img-container').classList.contains('loading')"
    ) is False

    # committed spread whose assigned page 404s -> error state (⚠)
    bad = _manifest("long", 30, 0)
    bad["pageRefs"].append({"folder": "long", "name": "missing.png"})
    bad["chapters"][0]["pages"].append({"folder": "long", "name": "missing.png"})
    bad["spreadTable"].append(
        {"id": "c0:s15", "chapterFolder": "long",
         "right": {"folder": "long", "name": "missing.png"}, "left": None}
    )
    _commit(page, _snapshot("c0:s15", "right", 9), bad, "leader")
    page.wait_for_function(
        "document.getElementById('img-container').classList.contains('loading') === false"
    )
    assert "⚠" in page.locator("#counter").inner_text()


def test_render_path_emits_no_intents(page, server):
    page.goto(server)
    page.wait_for_selector(".tile")
    man = _manifest("long", 30, 0)
    page.evaluate(
        "window.__navIntents = [];"
        "window.ViewerNavigation.setDefaultHandler(i => { window.__navIntents.push(i); });"
    )
    _commit(page, _snapshot("c0:s0"), man, "leader")
    settle(page, 4)
    _loaded_ready(page)
    # applying/渲染 a committed state must not produce any outgoing intent
    assert page.evaluate("window.__navIntents") == []


def test_leave_restores_solo_layout(page, server):
    open_book(page, server, "long", 1, 30)     # solo reading p002
    man = _manifest("long", 30, 0)
    _commit(page, _snapshot("c0:s0"), man, "leader")
    settle(page, 3)
    _loaded_ready(page)
    assert _visible_srcs(page) == ["/image/long/p001.png"]
    assert _state(page)["active"] is True

    page.evaluate("window.PairedView.leave()")
    settle(page, 3)
    assert _state(page)["active"] is False
    assert page.evaluate(
        "document.getElementById('img-container').classList.contains('pair-mode')"
    ) is False
    # pre-pair solo layout fully restored
    expect(page.locator("#counter")).to_have_text("2 / 30")
    assert page.evaluate("window.currentIdx") == 1
    assert _visible_srcs(page) == ["/image/long/p002.png"]
    # solo turn still works afterwards
    page.keyboard.press("ArrowLeft")
    expect(page.locator("#counter")).to_have_text("3 / 30")
