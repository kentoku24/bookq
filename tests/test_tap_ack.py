"""Tap acknowledgement: a zone tap is acted on at touchend (not the ~40ms-later
synthesized click), the counter/slider/frame update in the same synchronous
turn, and prefetch-window/evict/fetch work is deferred past the first frame."""
from playwright.sync_api import expect

from viewer_touch import open_book, touch_session, touch_start, touch_end

FOLDER = "long"


def test_touchend_alone_turns_the_page(ios_page, server):
    """No click event at all: touchStart + touchEnd via CDP must already turn."""
    page = ios_page
    open_book(page, server, FOLDER, 0, 30)
    s = touch_session(page)
    touch_start(s, [{"x": 60, "y": 500, "id": 1}])
    touch_end(s, [{"x": 60, "y": 500, "id": 1}])
    # counter is written synchronously inside snapTo, before any frame
    assert page.evaluate("document.querySelector('#counter').textContent.trim()") == "2 / 30"
    assert page.evaluate("window.currentIdx") == 1
    touch_start(s, [{"x": 330, "y": 500, "id": 1}])
    touch_end(s, [{"x": 330, "y": 500, "id": 1}])
    assert page.evaluate("window.currentIdx") == 0
    s.detach()


def test_tap_with_synthesized_click_turns_exactly_once(ios_page, server):
    page = ios_page
    open_book(page, server, FOLDER, 0, 30)
    page.touchscreen.tap(60, 500)  # touch events + the click WebKit/Chromium synthesize
    page.wait_for_timeout(500)
    expect(page.locator("#counter")).to_have_text("2 / 30")


def test_center_tap_toggles_toolbar_once(ios_page, server):
    page = ios_page
    open_book(page, server, FOLDER, 0, 30)
    hidden0 = page.evaluate("document.querySelector('#toolbar').classList.contains('hidden')")
    page.touchscreen.tap(195, 500)
    page.wait_for_timeout(500)
    assert page.evaluate("document.querySelector('#toolbar').classList.contains('hidden')") == (not hidden0)
    expect(page.locator("#counter")).to_have_text("1 / 30")


def test_window_work_is_deferred_past_the_acknowledging_frame(ios_page, server):
    page = ios_page
    open_book(page, server, FOLDER, 0, 30)
    page.wait_for_function("window.__windowWorkDone >= 1")
    # The window job is deferred via rAF→rAF, so "has it run yet" cannot be
    # checked from a post-tap evaluate: the CDP round-trip can span those
    # frames under CI load. Stamp the tap's dispatch frame (a capture-phase
    # touchend listener runs in the same task as the app handler) and the
    # frame of the job's counter write (setter interception) in-page.
    page.evaluate(
        """() => {
            window.__frameNo = 0;
            requestAnimationFrame(function loop(){
                window.__frameNo++;
                requestAnimationFrame(loop);
            });
            window.__tapFrame = null;
            window.__workFrame = null;
            window.addEventListener('touchend',
                () => { window.__tapFrame = window.__frameNo; }, true);
            let _wd = 0;
            Object.defineProperty(window, '__windowWorkDone', {
                configurable: true,
                get(){ return _wd; },
                set(v){ _wd = v; window.__workFrame = window.__frameNo; }
            });
        }"""
    )
    s = touch_session(page)
    touch_start(s, [{"x": 60, "y": 500, "id": 1}])
    touch_end(s, [{"x": 60, "y": 500, "id": 1}])
    # Synchronously: page acknowledged.
    st = page.evaluate("({idx: window.currentIdx, counter: document.querySelector('#counter').textContent.trim()})")
    assert st["idx"] == 1 and st["counter"] == "2 / 30"
    # The prefetch/evict job ran in a strictly later frame than the tap's turn.
    page.wait_for_function("window.__workFrame !== null")
    frames = page.evaluate("({tap: window.__tapFrame, work: window.__workFrame})")
    assert frames["tap"] is not None
    assert frames["work"] > frames["tap"], frames
    # and the window did arrive afterwards (PREFETCH_AHEAD=8 → page 10 exists)
    assert page.evaluate("!!window.stripImgs[9]")
    s.detach()


def test_burst_defers_only_the_last_turns_window_job(ios_page, server):
    page = ios_page
    open_book(page, server, FOLDER, 0, 30)
    page.wait_for_function("window.__windowWorkDone >= 1")
    before = page.evaluate("window.__windowWorkDone")
    s = touch_session(page)
    for _ in range(5):
        touch_start(s, [{"x": 60, "y": 500, "id": 1}])
        touch_end(s, [{"x": 60, "y": 500, "id": 1}])
    assert page.evaluate("window.currentIdx") == 5
    # Every acknowledged turn happened synchronously; the window job for the
    # final page lands afterwards (older jobs may or may not have run, depending
    # on how many frames each CDP round-trip spanned — that is not the contract).
    page.wait_for_function("!!window.stripImgs[13]")  # window built around the final page
    assert page.evaluate("window.__windowWorkDone") > before
    s.detach()
