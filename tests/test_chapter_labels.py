"""Chapter divider labels are windowed (#27): only chapters near the page being
read keep a label, so #img-track never spans the full book width (~1.4M px on
3667-page titles balloons its composited layer and slows every slide on a
tile-GPU device). Fixture group/episodes has 4 chapters (ch-1/-2/-10/-11)."""

import os

import pytest

from viewer_touch import open_book, touch_session, touch_start, touch_end

# The ring reader is a documented prototype: three fixed slots, NO chapter
# divider labels on the strip (the toolbar chapter name still works). These
# tests pin a strip-DOM feature, so a ring-mode suite run skips them.
pytestmark = pytest.mark.skipif(
    os.environ.get("BOOKQ_TEST_READER") == "ring",
    reason="ring reader has no strip chapter-divider labels (documented limitation)",
)

EP = "group/episodes"


def _label_set(page):
    return page.evaluate(
        "[...document.querySelectorAll('.chapter-divider')]"
        ".map(e => +e.dataset.chapterIdx).sort((a, b) => a - b)"
    )


def _chapter_idx(page):
    return page.evaluate("window.getCurrentChapterInfo(window.currentIdx).chapterIdx")


def _wait_labels(page, exp, timeout=5000):
    import json

    page.wait_for_function(
        "JSON.stringify([...document.querySelectorAll('.chapter-divider')]"
        ".map(e => +e.dataset.chapterIdx).sort((a, b) => a - b))"
        f" === '{json.dumps(exp, separators=(',', ':'))}'",
        timeout=timeout,
    )


def test_labels_windowed_around_opened_chapter(ios_page, server):
    # ch-1 (chapterIdx 0): window is {1, 2}, not all {1, 2, 3}
    open_book(page := ios_page, server, f"{EP}/ch-1", 0, 1)
    assert _chapter_idx(page) == 0
    assert _label_set(page) == [1, 2]
    # every live label is positioned (same math as before, now on the live set)
    lefts = page.evaluate(
        "[...document.querySelectorAll('.chapter-divider')].map(e => parseFloat(e.style.left))"
    )
    assert len(lefts) == 2
    assert all(isinstance(x, (int, float)) and x == x for x in lefts), lefts
    names = page.evaluate(
        "[...document.querySelectorAll('.chapter-divider')].map(e => e.textContent)"
    )
    assert all(n for n in names)


def _tap_left(page, n=1):
    s = touch_session(page)
    for _ in range(n):
        touch_start(s, [{"x": 78, "y": 500, "id": 1}])
        touch_end(s, [{"x": 78, "y": 500, "id": 1}])
        page.wait_for_timeout(600)
    s.detach()


def test_labels_follow_page_turns(ios_page, server):
    open_book(page := ios_page, server, f"{EP}/ch-1", 0, 1)
    assert _label_set(page) == [1, 2]
    # forward until at least 2 chapters ahead (spacer pads vary turns/chapter)
    for _ in range(10):
        if _chapter_idx(page) >= 2:
            break
        _tap_left(page)
    center = _chapter_idx(page)
    assert center >= 2, center
    exp = [i for i in range(max(1, center - 2), min(3, center + 2) + 1)]
    _wait_labels(page, exp)
    assert _label_set(page) == exp
    # back to the first chapter: far labels are dropped again
    for _ in range(10):
        if _chapter_idx(page) == 0:
            break
        s = touch_session(page)
        touch_start(s, [{"x": 312, "y": 500, "id": 1}])
        touch_end(s, [{"x": 312, "y": 500, "id": 1}])
        page.wait_for_timeout(600)
        s.detach()
    assert _chapter_idx(page) == 0
    _wait_labels(page, [1, 2])
    assert _label_set(page) == [1, 2]
