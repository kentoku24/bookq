"""JSTouchSession — CDP ``Input.dispatchTouchEvent`` shim over synthetic
``TouchEvent`` dispatch for Safari.

``tests/viewer_touch.py`` routes ``touch_session(page)`` here under
``BROWSER=safari``: the same ``session.send("Input.dispatchTouchEvent", …)``
calls drive ``window.__bookqTouch`` instead of CDP, so the 13 touch-based
files (gesture/zoom/overview/tap/pair helpers) run unchanged. Synthetic
``TouchEvent`` objects carry real ``touches``/``changedTouches`` lists, which
is all the app's handlers read.
"""

from tests.safari.exceptions import SafariUnsupported

_TYPE_MAP = {
    "touchStart": "touchstart",
    "touchMove": "touchmove",
    "touchEnd": "touchend",
    "touchCancel": "touchcancel",
}


class JSTouchSession:
    def __init__(self, page):
        self._page = page
        if not page._touch_installed:
            page._install_touch()

    def send(self, command: str, params: dict):
        if command != "Input.dispatchTouchEvent":
            raise SafariUnsupported(f"CDP command {command} is not supported on Safari")
        t = _TYPE_MAP.get(params.get("type"))
        if t is None:
            raise SafariUnsupported(f"unknown touch type {params.get('type')}")
        pts = [
            {"x": float(p["x"]), "y": float(p["y"]), "id": int(p.get("id", i + 1))}
            for i, p in enumerate(params.get("touchPoints") or [])
        ]
        self._page._eval(
            "window.__bookqTouch._mk(arguments[0], arguments[1])", t, pts
        )
