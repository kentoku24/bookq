"""Swipe-commit slide must settle on the TRACK's transform transitionend only.

The 250ms commit slide waits on a bubbling `transitionend` listener on
#img-track. Page images fade in with an opacity transition and those events
bubble too — the live-reproduced bug: a child IMG's TransitionEvent(opacity)
reached the waiter, called snapTo() early (currentIdx advanced) and set
transition:none, killing the slide mid-flight. The waiter's guard must match
snapTo's settle helper isTrackSlideEnd (e.target === #img-track &&
propertyName === 'transform'); the 260ms timer passes no event and must keep
working.
"""
import pytest
from playwright.sync_api import expect

from viewer_touch import open_book, settle

FOLDER = "long"  # 30 pages, single chapter


def _fire_swipe(page, direction):
    """One synchronous swipe on #img-container starting at idx 1.

    direction +1 commits a forward turn (dragPx > 0), -1 a back turn. Returns
    transitionend-snapshot list: [{idx, tr}, ...] taken after the gesture and
    after each synthetic event dispatch.
    """
    return page.evaluate(
        """([dir]) => {
          const c = document.getElementById('img-container');
          const track = document.getElementById('img-track');
          const img = track.querySelector('img');
          const mk = (x) => new Touch({identifier: 91, target: c, clientX: x, clientY: 450});
          const fire = (type, x) => {
            const t = mk(x);
            c.dispatchEvent(new TouchEvent(type, {
              touches: type === 'touchend' ? [] : [t],
              changedTouches: [t],
              bubbles: true, cancelable: true,
            }));
          };
          const snap = (label) => ({label, idx: window.currentIdx, tr: track.style.transition});
          const snaps = [];

          fire('touchstart', 200);
          for (const x of [230, 260, 290, 320].map(v => dir > 0 ? v : 400 - v)) fire('touchmove', x);
          fire('touchend', 200 + dir * 120);
          snaps.push(snap('after-gesture'));

          const fireTe = (el, prop) => el.dispatchEvent(
            new TransitionEvent('transitionend', {propertyName: prop, bubbles: true}));
          fireTe(img, 'opacity');    snaps.push(snap('img-opacity'));
          fireTe(img, 'transform');  snaps.push(snap('img-transform'));
          fireTe(track, 'opacity');  snaps.push(snap('track-opacity'));
          fireTe(track, 'transform');snaps.push(snap('track-transform'));
          return snaps;
        }""",
        [direction],
    )


@pytest.mark.parametrize(
    "direction,target_idx",
    [(1, 2), (-1, 0)],
    ids=["forward", "back"],
)
def test_swipe_slide_ignores_bubbled_child_transitionend(ios_page, server, direction, target_idx):
    open_book(ios_page, server, FOLDER, 1, 30)
    assert ios_page.evaluate("window.currentIdx") == 1

    snaps = _fire_swipe(ios_page, direction)

    # The commit armed a 250ms slide on the track.
    assert snaps[0]["idx"] == 1, snaps[0]
    assert "transform" in snaps[0]["tr"] and ".25s" in snaps[0]["tr"], snaps[0]

    # Bubbled image events (opacity AND transform) and track's own opacity
    # transitionend must not end the slide early.
    for s in snaps[1:4]:
        assert s["idx"] == 1, s
        assert "transform" in s["tr"], s

    # Only the track's transform transitionend finalizes the turn.
    assert snaps[4]["idx"] == target_idx, snaps[4]
    assert snaps[4]["tr"] == "none", snaps[4]


def test_swipe_finalize_timer_fallback_without_transitionend(ios_page, server):
    """If no transitionend ever reaches the waiter, the 260ms timer still
    finalizes — and exactly once (a late duplicate event must not re-run)."""
    open_book(ios_page, server, FOLDER, 1, 30)
    # Swallow every transitionend before the swipe waiter's bubble listener
    # can see it (capture handler registered first runs first).
    ios_page.evaluate(
        """() => { document.getElementById('img-track').addEventListener(
             'transitionend', e => e.stopImmediatePropagation(), true); }"""
    )
    snaps = _fire_swipe(ios_page, 1)
    # The capture blocker ate our synthetic track-transform event too — the
    # turn is still pending; only the timer can land it.
    assert snaps[4]["idx"] == 1, snaps[4]
    ios_page.wait_for_function("window.currentIdx === 2")
    # Stale duplicate events after the settle must not turn another page.
    ios_page.evaluate(
        """() => { const t = document.getElementById('img-track');
           t.dispatchEvent(new TransitionEvent('transitionend',
             {propertyName: 'transform', bubbles: true})); }"""
    )
    settle(ios_page)
    assert ios_page.evaluate("window.currentIdx") == 2
    expect(ios_page.locator("#counter")).to_have_text("3 / 30")
