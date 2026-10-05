"""Regression tests for PWA icon unification (Todo 6).

Covers:
  (a) manifest valid JSON with 192 any, 512 any, 512 maskable, theme #0f172a, no display_override
  (b) HTML head for / contains favicon 3, apple-touch 180 sizes, manifest, theme-color, apple-mobile metas and no data URI nor old 192 link
  (c) offline.html same head
  (d) /favicon.ico and /static/favicon.ico 200 same content
  (e) all assets 200

Uses the server fixture from conftest.py (BOOKQ_TEST_MODE=true, random port,
DOWNLOADS_DIR tmp). Pure stdlib + urllib – no extra deps.

Run:
    uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_pwa_icons.py -q
"""

import json
import re
import urllib.request
import urllib.error
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _get(server: str, path: str) -> tuple[int, bytes, dict]:
    """GET path on the test server; return (status, body, headers lower-cased)."""
    url = server + path
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read()
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, body, headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), {k.lower(): v for k, v in (e.headers or {}).items()}


def _get_text(server: str, path: str) -> tuple[int, str]:
    status, body, _ = _get(server, path)
    return status, body.decode("utf-8", errors="replace")


# ── (a) manifest ─────────────────────────────────────────────────────────────

def test_manifest_valid_json_and_icons(server):
    status, body, headers = _get(server, "/static/manifest.json")
    assert status == 200, f"manifest status {status}"
    # content-type should be json (StaticFiles may return application/json)
    ct = headers.get("content-type", "")
    assert "json" in ct.lower(), f"manifest Content-Type {ct!r} should contain json"

    data = json.loads(body.decode("utf-8"))
    # required top-level fields
    assert data.get("name") == "bookq"
    assert data.get("short_name") == "bookq"
    assert data.get("id") == "/"
    assert data.get("start_url") == "/"
    assert data.get("scope") == "/"
    assert data.get("display") == "standalone"
    assert data.get("orientation") == "any"
    assert data.get("lang") == "ja"
    assert data.get("theme_color") == "#0f172a"
    assert data.get("background_color") == "#0f172a"

    icons = data.get("icons")
    assert isinstance(icons, list) and len(icons) >= 3, f"icons {icons}"

    # helper: find by sizes+purpose
    def has_icon(sizes: str, purpose: str, src_substr: str | None = None) -> bool:
        for ic in icons:
            if ic.get("sizes") == sizes and ic.get("purpose") == purpose:
                if src_substr is None or src_substr in ic.get("src", ""):
                    return True
        return False

    assert has_icon("192x192", "any", "icon-192.png"), f"missing 192 any: {icons}"
    assert has_icon("512x512", "any", "icon-512.png"), f"missing 512 any: {icons}"
    assert has_icon("512x512", "maskable", "icon-512-maskable.png"), f"missing 512 maskable: {icons}"

    # each all-512 maskable entry must not be the non-padded icon-512.png alone
    maskable = [ic for ic in icons if ic.get("purpose") == "maskable"]
    assert any("icon-512-maskable" in ic.get("src", "") for ic in maskable)

    # optional: explicit type fields
    for ic in icons:
        assert ic.get("type") == "image/png"


def test_manifest_no_display_override(server):
    status, body, _ = _get(server, "/static/manifest.json")
    assert status == 200
    data = json.loads(body.decode("utf-8"))
    assert "display_override" not in data, "display_override must not be present (WCO not implemented)"
    # purpose must be exactly any or maskable, never combined
    for ic in data.get("icons", []):
        assert ic.get("purpose") in ("any", "maskable"), f"unexpected purpose {ic!r}"
        assert ic.get("purpose") != "any maskable"


# ── (b) HTML head for / ──────────────────────────────────────────────────────

_HEAD_FAVICON_ICO_RE = re.compile(r'<link[^>]+rel="icon"[^>]+href="/static/favicon\.ico"[^>]*>', re.I)
_HEAD_FAVICON_SVG_RE = re.compile(r'<link[^>]+rel="icon"[^>]+href="/static/favicon\.svg"[^>]*>', re.I)
_HEAD_FAVICON_32_RE = re.compile(r'<link[^>]+rel="icon"[^>]+href="/static/favicon-32x32\.png"[^>]*>', re.I)
_HEAD_APPLE_RE = re.compile(r'<link[^>]+rel="apple-touch-icon"[^>]+href="/static/apple-touch-icon\.png"[^>]*>', re.I)
_HEAD_MANIFEST_RE = re.compile(r'<link[^>]+rel="manifest"[^>]+href="/static/manifest\.json"[^>]*>', re.I)
_HEAD_THEME_RE = re.compile(r'<meta[^>]+name="theme-color"[^>]+content="#0f172a"[^>]*>', re.I)


def _assert_viewer_head(html: str) -> None:
    # must contain favicon 3 + apple-touch 180 + manifest + theme-color + apple metas
    assert _HEAD_FAVICON_ICO_RE.search(html), "missing favicon.ico link"
    # sizes="any" on ico
    assert re.search(r'<link[^>]+href="/static/favicon\.ico"[^>]+sizes="any"', html, re.I), "favicon.ico must have sizes=any"
    assert _HEAD_FAVICON_SVG_RE.search(html), "missing favicon.svg link"
    assert re.search(r'type="image/svg\+xml"', html, re.I), "favicon.svg must have type image/svg+xml"
    assert _HEAD_FAVICON_32_RE.search(html), "missing favicon-32x32.png link"
    assert re.search(r'sizes="32x32"', html, re.I), "favicon-32x32 must have sizes=32x32"
    assert _HEAD_APPLE_RE.search(html), "missing apple-touch-icon link"
    assert re.search(r'apple-touch-icon[^>]+sizes="180x180"', html, re.I), "apple-touch-icon must have sizes=180x180"
    assert _HEAD_MANIFEST_RE.search(html), "missing manifest link"
    assert _HEAD_THEME_RE.search(html), "missing theme-color #0f172a"
    assert re.search(r'name="apple-mobile-web-app-capable"', html, re.I), "missing apple-mobile-web-app-capable"
    assert re.search(r'name="apple-mobile-web-app-title"[^>]+content="bookq"', html, re.I), "missing apple-mobile-web-app-title"
    assert re.search(r'name="apple-mobile-web-app-status-bar-style"', html, re.I), "missing apple-mobile-web-app-status-bar-style"
    assert re.search(r'name="mobile-web-app-capable"', html, re.I), "missing mobile-web-app-capable"

    # no favicon data URI (Google button img src is allowed – only check link rel=icon)
    assert not re.search(r'<link[^>]+rel="icon"[^>]+data:image', html, re.I), "favicon data URI must not remain in link rel=icon"
    # no old 192 apple-touch link
    assert "icons/icon-192.png" not in html, "old icons/icon-192.png apple-touch link must be removed"


def test_index_head_viewer(server):
    status, html = _get_text(server, "/")
    assert status == 200, f"/ status {status}"
    _assert_viewer_head(html)


def test_index_template_both_branches_contain_head():
    """File-level check: both login {% if not user %} and viewer {% else %} branches have the head set."""
    tpl = (REPO_ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
    # split at {% else %} to get login vs viewer branches
    assert "{% if not user %}" in tpl and "{% else %}" in tpl and "{% endif %}" in tpl
    login_part, rest = tpl.split("{% else %}", 1)
    viewer_part = rest.split("{% endif %}", 1)[0]

    for label, part in [("login", login_part), ("viewer", viewer_part)]:
        assert 'href="/static/favicon.ico"' in part, f"{label} missing favicon.ico"
        assert 'href="/static/favicon.svg"' in part, f"{label} missing favicon.svg"
        assert 'href="/static/favicon-32x32.png"' in part, f"{label} missing favicon-32x32"
        assert 'href="/static/apple-touch-icon.png"' in part, f"{label} missing apple-touch-icon"
        assert 'sizes="180x180"' in part, f"{label} missing sizes=180x180"
        assert 'rel="manifest"' in part, f"{label} missing manifest"
        assert 'theme-color' in part and "#0f172a" in part, f"{label} missing theme-color"
        assert "apple-mobile-web-app-capable" in part, f"{label} missing apple-mobile-web-app-capable"
        assert "data:image/svg+xml" not in part or 'rel="icon"' not in part.split("data:image/svg+xml")[0].rsplit("<link", 1)[-1] if "data:image/svg+xml" in part else True  # guard below
        # precise: no link rel=icon with data URI in this branch
        assert not re.search(r'<link[^>]+rel="icon"[^>]+data:image', part, re.I), f"{label} still has favicon data URI"
        assert "icons/icon-192.png" not in part, f"{label} still has old icon-192 link"


# ── (c) offline.html same head ───────────────────────────────────────────────

def test_offline_html_head(server):
    status, html = _get_text(server, "/static/offline.html")
    assert status == 200, f"offline.html status {status}"
    _assert_viewer_head(html)
    # offline should still link to "/" and keep style.css
    assert 'href="/"' in html or "href='/'" in html
    assert "/static/style.css" in html
    # no JS must be injected (offline is static)
    assert "<script" not in html.lower() or html.lower().count("<script") == 0, "offline.html should not contain script tags"

    # file-level also
    offline_file = (REPO_ROOT / "app" / "static" / "offline.html").read_text(encoding="utf-8")
    assert 'apple-touch-icon' in offline_file
    assert 'sizes="180x180"' in offline_file
    assert 'rel="manifest"' in offline_file
    assert 'theme-color' in offline_file


# ── (d) favicon.ico alias ────────────────────────────────────────────────────

def test_favicon_ico_alias_same_content(server):
    s1, b1, h1 = _get(server, "/favicon.ico")
    s2, b2, h2 = _get(server, "/static/favicon.ico")
    assert s1 == 200, f"/favicon.ico status {s1}"
    assert s2 == 200, f"/static/favicon.ico status {s2}"
    assert b1 == b2, "/favicon.ico and /static/favicon.ico content must be identical"
    assert len(b1) > 0
    # content-type
    assert "image/x-icon" in h1.get("content-type", "").lower() or "image/x-icon" in h1.get("content-type", "").lower() or "x-icon" in h1.get("content-type", "").lower()
    # cache header
    assert "max-age=86400" in h1.get("cache-control", "")
    # ETag equality if present
    if "etag" in h1 and "etag" in h2:
        assert h1["etag"] == h2["etag"]


# ── (e) all assets 200 ───────────────────────────────────────────────────────

_ASSET_PATHS = [
    "/static/favicon.ico",
    "/static/favicon.svg",
    "/static/favicon-32x32.png",
    "/static/apple-touch-icon.png",
    "/static/icons/icon-192.png",
    "/static/icons/icon-512.png",
    "/static/icons/icon-512-maskable.png",
    "/static/manifest.json",
    "/static/offline.html",
    "/static/style.css",
    "/sw.js",
    "/static/sw.js",
]


def test_all_assets_200(server):
    for path in _ASSET_PATHS:
        status, body, headers = _get(server, path)
        assert status == 200, f"{path} status {status}"
        assert len(body) > 0, f"{path} empty body"
        # spot-check content-types
        ct = headers.get("content-type", "").lower()
        if path.endswith(".ico"):
            assert "icon" in ct or "x-icon" in ct, f"{path} Content-Type {ct!r}"
        elif path.endswith(".svg"):
            assert "svg" in ct, f"{path} Content-Type {ct!r}"
        elif path.endswith(".png"):
            assert "png" in ct, f"{path} Content-Type {ct!r}"
        elif path.endswith(".json"):
            assert "json" in ct, f"{path} Content-Type {ct!r}"
        elif path.endswith(".html"):
            assert "html" in ct, f"{path} Content-Type {ct!r}"
        elif path.endswith("sw.js"):
            assert "javascript" in ct, f"{path} Content-Type {ct!r}"


def test_sw_has_service_worker_allowed_header(server):
    _, _, headers = _get(server, "/sw.js")
    assert headers.get("service-worker-allowed") == "/", "SW must have Service-Worker-Allowed: /"
    _, _, headers2 = _get(server, "/static/sw.js")
    # static mount may not set the header – at least /sw.js must
    assert headers2  # exists
