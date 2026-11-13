"""Synthetic touch helpers for the zoom / overview tests.

Playwright has no pinch API; CDP Input.dispatchTouchEvent with two touch
points is deterministic in Chromium. All helpers are state-polled by the
callers (no time.sleep).
"""
from playwright.sync_api import expect


def open_book(page, server, folder: str, p: int, expect_local_total: int) -> None:
    page.goto(f"{server}/#/view/{folder}?p={p}")
    expect(page.locator("#counter")).to_have_text(f"{p + 1} / {expect_local_total}")


def touch_session(page):
    # BROWSER=safari: real Safari speaks WebDriver, not CDP — route the same
    # Input.dispatchTouchEvent calls through synthetic TouchEvent dispatch.
    if page.__class__.__name__ == "SafariPage":
        from tests.safari.touch import JSTouchSession

        return JSTouchSession(page)
    return page.context.new_cdp_session(page)


def _pts(points):
    return [{"x": float(p["x"]), "y": float(p["y"]), "id": int(p.get("id", i + 1))}
            for i, p in enumerate(points)]


def touch_start(session, points):
    session.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": _pts(points)})


def touch_move(session, points):
    session.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": _pts(points)})


def touch_end(session, points=()):
    """`points` are the touch points being RELEASED (Chromium CDP semantics); omit to release all."""
    session.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": _pts(points)})


def two(cx, cy, half):
    return [{"x": cx - half, "y": cy, "id": 1}, {"x": cx + half, "y": cy, "id": 2}]


def pinch(session, cx, cy, halves, hold=False):
    """Two fingers at cx±halves[0], then move through the remaining halves.
    Both fingers move symmetrically, so each crosses the 10px arm threshold
    as soon as |halves[i]-halves[0]| >= 10."""
    touch_start(session, two(cx, cy, halves[0]))
    for h in halves[1:]:
        touch_move(session, two(cx, cy, h))
    if not hold:
        touch_end(session)


def drag(session, x0, y0, x1, y1, steps=6, id=1, hold=False):
    touch_start(session, [{"x": x0, "y": y0, "id": id}])
    for i in range(1, steps + 1):
        t = i / steps
        touch_move(session, [{"x": x0 + (x1 - x0) * t, "y": y0 + (y1 - y0) * t, "id": id}])
    if not hold:
        touch_end(session)


def tap(session, x, y, id=1):
    touch_start(session, [{"x": x, "y": y, "id": id}])
    touch_end(session)


def zoom_state(page) -> dict:
    return page.evaluate("window.Zoom ? window.Zoom.state() : null")


def settle(page, frames: int = 2) -> None:
    """Wait `frames` animation frames.

    snapTo() defers rebaseAnchor() to a requestAnimationFrame, so `#img-track`'s
    inline transform is one frame behind every call that repositions the strip.
    Reading it without this returns the pre-rebase value.
    """
    page.evaluate(
        "(n) => new Promise(res => { const step = k => k "
        "? requestAnimationFrame(() => step(k - 1)) : res(); step(n); })",
        frames,
    )


def track_transform(page) -> str:
    return page.evaluate("document.querySelector('#img-track').style.transform")


def track_tx(page) -> float:
    """The px in `#img-track`'s inline `translateX(...)`, 0.0 when it has none."""
    import re

    m = re.search(r"translateX\((-?[\d.]+)px\)", track_transform(page) or "")
    return float(m.group(1)) if m else 0.0
