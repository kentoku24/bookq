"""UI logic regression tests for the .omo/ui-ux-audit.md remediation batch.

Covers three fixes from audit section 4, all TDD / characterization style:

- H7 (L-F1): the ⚠ warning suffix on #counter must only appear for a GENUINE
  load failure of the current page's image (naturalWidth === 0). Recycled
  images firing stale `error` events must not pollute the counter.
- H8 (S-F4): the spinner must stay visible through the pre-decode window —
  it must not be hidden before the current page image has actually decoded.
- H10 (B-N2): search result tiles fall back to the folder's tree thumbnail
  when the metadata carries no embedded thumbnail.

Determinism note: `cur.complete` is true at snapTo time whenever the fetch
has not started yet (HTML spec: complete is true when "no request has yet
been made"), so on a fast local server the pre-decode window collapses to
zero and snapTo attaches no error listener. The H7/H8 tests therefore run
through a local threaded HTTP proxy that delays /image/ responses by 1 s —
a deterministic, generous pre-decode / in-flight window. (Playwright route
handlers can't do this: in the sync API they block the driver, and CDP
Network.emulateNetworkConditions does not slow down <img> element loads.)

Run:
    uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_ui_logic.py -q
"""

import http.server
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from urllib.parse import urlparse

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

WARNING_MARK = "⚠"


def _make_png(path: Path, w: int = 40, h: int = 60, rgb: tuple = (200, 60, 60)) -> None:
    """Write a minimal solid-color RGB PNG without external deps."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(rgb) * w
    idat = zlib.compress(row * h)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
    )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(downloads: Path):
    """Boot a uvicorn test-mode server against *downloads*; yield base URL."""
    port = _free_port()
    env = os.environ.copy()
    env["BOOKQ_TEST_MODE"] = "true"
    env["DOWNLOADS_DIR"] = str(downloads)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("Server exited early")
        try:
            with urllib.request.urlopen(base_url + "/api/health", timeout=1):
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.terminate()
        raise RuntimeError("Server did not become healthy within 10s")
    try:
        yield base_url
    finally:
        proc.terminate()
        proc.wait(timeout=5)


class _DelayingImageProxy(http.server.ThreadingHTTPServer):
    """Forwards every request to *target*, sleeping IMAGE_DELAY_S before
    responding to /image/* paths — a deterministic slow-image network.

    Used instead of Playwright route handlers (which block the sync driver)
    or CDP throttling (which does not apply to <img> element loads).
    """

    target = ""
    image_delay_s = 1.0

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlparse(self.path).path
            try:
                with urllib.request.urlopen(self.server.target + path, timeout=10) as resp:
                    body = resp.read()
                    status = resp.status
                    headers = {
                        k: v
                        for k, v in resp.headers.items()
                        if k.lower() not in ("content-length", "connection", "transfer-encoding")
                    }
            except urllib.error.HTTPError as e:
                body = e.read()
                status = e.code
                headers = {
                    k: v
                    for k, v in e.headers.items()
                    if k.lower() not in ("content-length", "connection", "transfer-encoding")
                }
            except Exception as e:
                body = str(e).encode()
                status = 502
                headers = {}
            if path.startswith("/image/"):
                time.sleep(self.server.image_delay_s)
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass


def _start_proxy(target: str, delay_s: float = 1.0):
    """Threaded image-delaying proxy; yields its base URL."""
    srv = _DelayingImageProxy(("127.0.0.1", _free_port()), _DelayingImageProxy._Handler)
    srv.target = target
    srv.image_delay_s = delay_s
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        thread.join(timeout=5)


class _FailingPathProxy(http.server.ThreadingHTTPServer):
    """Forwards every request to *target*, except the paths listed in
    *fail_paths*, which are answered directly with their configured status
    (e.g. 404 for a specific image, 500 for the images API). Mirrors
    _DelayingImageProxy's forwarding structure."""

    target = ""
    fail_paths = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlparse(self.path).path
            status = self.server.fail_paths.get(path)
            if status is not None:
                body = b"failing path"
                headers = {}
            else:
                try:
                    with urllib.request.urlopen(self.server.target + path, timeout=10) as resp:
                        body = resp.read()
                        status = resp.status
                        headers = {
                            k: v
                            for k, v in resp.headers.items()
                            if k.lower() not in ("content-length", "connection", "transfer-encoding")
                        }
                except urllib.error.HTTPError as e:
                    body = e.read()
                    status = e.code
                    headers = {
                        k: v
                        for k, v in e.headers.items()
                        if k.lower() not in ("content-length", "connection", "transfer-encoding")
                    }
                except Exception as e:
                    body = str(e).encode()
                    status = 502
                    headers = {}
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass


def _start_path_fail_proxy(target: str, fail_paths: dict):
    """Threaded proxy answering *fail_paths* with their status codes and
    forwarding everything else to *target*; yields its base URL."""
    srv = _FailingPathProxy(("127.0.0.1", _free_port()), _FailingPathProxy._Handler)
    srv.target = target
    srv.fail_paths = dict(fail_paths)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        thread.join(timeout=5)


# ── H7: warning suffix gate ────────────────────────────────────────────────────


def test_counter_no_warning_after_restore(page, server):
    """H7 characterization: deep-linking a second book in the same session
    (restore path) must leave #counter free of the ⚠ suffix.

    The underlying race (an aborted prefetch/recycled-img `error` event) is
    non-deterministic, so this locks the user-visible invariant: the restore
    flow itself never appends the warning.
    """
    # Open the first book and turn one page (left zone = NEXT, RTL).
    page.locator('.tile[data-path="simple_manga"]').click()
    page.wait_for_selector("#img-track img.loaded")
    page.locator("#zone-prev").click()

    # Deep-link a second book in the same session.
    page.goto(server + "/#/view/long?p=0")
    page.wait_for_selector("#img-track img.loaded")

    counter = page.text_content("#counter") or ""
    assert "1 / 30" in counter, f"unexpected counter text: {counter!r}"
    assert WARNING_MARK not in counter, f"spurious warning after restore: {counter!r}"


def test_counter_no_warning_on_synthetic_error_after_decode(slow_page, slow_proxy):
    """H7 gate: an `error` event fired on the CURRENT page's img after it has
    fully decoded (naturalWidth > 0) must NOT append ⚠ — the image is fine;
    the error is a recycling/abort artifact.

    The page turn lands on the prefetched page-2 img while its fetch is still
    in flight (the proxy delays /image/ by 1 s, so complete === false at
    snapTo), which is exactly when the snapTo error listener gets attached.
    RED pre-fix (the listener appends ⚠ unconditionally), GREEN post-fix
    (gated on naturalWidth === 0).
    """
    slow_page.goto(slow_proxy + "/#/view/mix?p=0")
    # Turn the page only while page 2 is still fetching, so snapTo attaches
    # its error listener to a not-yet-loaded img (the abort-race shape).
    slow_page.wait_for_function(
        """() => {
          const imgs = [...document.querySelectorAll('#img-track img')];
          const cur = imgs.find(i => i.getAttribute('src') === '/image/mix/p002.png');
          return !!cur && !cur.complete;
        }""",
        timeout=15000,
    )
    slow_page.locator("#zone-prev").click()  # left zone = NEXT page (RTL)

    # Wait for the current page image to have fully decoded.
    slow_page.wait_for_function(
        """() => {
          const imgs = [...document.querySelectorAll('#img-track img')];
          const cur = imgs.find(i => i.getAttribute('src') === '/image/mix/p002.png');
          return !!cur && cur.complete && cur.naturalWidth > 0;
        }""",
        timeout=20000,
    )

    # Dispatch a synthetic error on the current page img.
    slow_page.evaluate(
        """() => {
          const imgs = [...document.querySelectorAll('#img-track img')];
          const cur = imgs.find(i => i.getAttribute('src') === '/image/mix/p002.png');
          if (!cur) throw new Error('current page img not found in strip');
          cur.dispatchEvent(new Event('error'));
        }"""
    )

    counter = slow_page.text_content("#counter") or ""
    assert "2 / 3" in counter, f"unexpected counter text: {counter!r}"
    assert WARNING_MARK not in counter, f"false-alarm warning: {counter!r}"


# ── H8: spinner stays visible during the pre-decode window ─────────────────────


def test_spinner_visible_during_predecode(slow_page, slow_proxy):
    """H8: between opening a book and the current page image decoding, the
    spinner must stay visible (loading class + display:block). After the
    decode it must be hidden again.

    The proxy delays the current page's image by 1 s while loadImages ran
    synchronously into snapTo — so pre-fix (where snapTo treats a
    not-yet-started img as "complete" and hides the spinner instantly) the
    first poll already sees loading removed (RED); post-fix the window is
    observable (GREEN).
    """
    # Deep-link restore, not a tile click: the module-scoped page may have the
    # previous test's book open (sidebar closed), and the restore path is the
    # same loadImages → snapTo flow anyway. Open on page 2 (p002): page 1 is
    # already in the browser cache from the root grid's tile thumbnail, so it
    # would skip the proxy delay entirely.
    slow_page.goto(slow_proxy + "/#/view/slow?p=1")

    saw_predecode_window = False
    saw_decode = False
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not saw_decode:
        state = slow_page.evaluate(
            """() => {
              const c = document.getElementById('img-container');
              const imgs = [...document.querySelectorAll('#img-track img')];
              const p2 = imgs.find(i => i.getAttribute('src') === '/image/slow/p002.png');
              return {
                loading: c.classList.contains('loading'),
                spinner: document.getElementById('spinner').style.display,
                p2Decoded: !!p2 && p2.complete && p2.naturalWidth > 0,
              };
            }"""
        )
        if not state["p2Decoded"] and state["loading"] and state["spinner"] == "block":
            saw_predecode_window = True
        if state["p2Decoded"]:
            saw_decode = True
        slow_page.wait_for_timeout(50)

    assert saw_decode, "current page image never decoded (image too large?)"
    assert saw_predecode_window, (
        "spinner was never visible while the current page image was undecoded — "
        "it is being hidden before the decode finishes (H8 regression)"
    )

    # Once decoded, the spinner is gone.
    final = slow_page.evaluate(
        """() => ({
          loading: document.getElementById('img-container').classList.contains('loading'),
          spinner: document.getElementById('spinner').style.display,
        })"""
    )
    assert not final["loading"], "loading class still set after decode"
    assert final["spinner"] == "none", f"spinner still visible after decode: {final['spinner']!r}"


# ── H10: search tiles fall back to the tree thumbnail ──────────────────────────


@pytest.fixture(scope="module")
def slow_downloads(tmp_path_factory) -> Path:
    """Tiny tree for the H7/H8 determinism tests; the slow network comes from
    the image-delaying proxy, not the files. "other" keeps the folders from
    being collapsed by the single-child flattening rule."""
    root = tmp_path_factory.mktemp("slow_downloads")
    slow = root / "slow"
    slow.mkdir()
    for name in ["p001.png", "p002.png", "p003.png"]:
        _make_png(slow / name)
    mix = root / "mix"
    mix.mkdir()
    for name in ["p001.png", "p002.png", "p003.png"]:
        _make_png(mix / name)
    other = root / "other"
    other.mkdir()
    _make_png(other / "1.png")
    return root


@pytest.fixture(scope="module")
def slow_server(slow_downloads: Path):
    yield from _start_server(slow_downloads)


@pytest.fixture(scope="module")
def slow_proxy(slow_server: str):
    yield from _start_proxy(slow_server, delay_s=1.0)


@pytest.fixture(scope="module")
def slow_page(request, slow_proxy):
    b = request.getfixturevalue("webkit_browser" if os.environ.get("BROWSER") == "webkit" else "browser")
    context = b.new_context(viewport={"width": 390, "height": 844}, proxy={"server": slow_proxy})
    page = context.new_page()
    page.goto(slow_proxy)  # anything the page loads reaches the proxy first
    page.wait_for_selector(".tile")  # folder tree fetched and rendered
    yield page
    context.close()


@pytest.fixture(scope="module")
def search_downloads(tmp_path_factory) -> Path:
    """Dedicated downloads tree WITH metadata.toml (the shared conftest tree
    has none, so /api/search would return 0 results there). "other" keeps
    "sample" from being collapsed by the single-child flattening rule."""
    root = tmp_path_factory.mktemp("search_downloads")
    folder = root / "sample"
    folder.mkdir()
    for name in ["1.png", "2.png"]:
        _make_png(folder / name)
    # Metadata with an artist but no embedded thumbnail — the H10 fallback path.
    (folder / "metadata.toml").write_text(
        '[source]\n'
        'name = "generic"\n'
        '\n'
        '[comic]\n'
        'id = "1"\n'
        'title_jp = "テスト作品"\n'
        'total_pages = 2\n'
        '\n'
        '[tags]\n'
        'artist = ["テスト作者"]\n'
    )
    other = root / "other"
    other.mkdir()
    _make_png(other / "1.png")
    return root


@pytest.fixture(scope="module")
def search_server(search_downloads: Path):
    yield from _start_server(search_downloads)


@pytest.fixture(scope="module")
def search_page(request, search_server):
    """A fresh browser page against the metadata-bearing server."""
    b = request.getfixturevalue("webkit_browser" if os.environ.get("BROWSER") == "webkit" else "browser")
    context = b.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    page.goto(search_server)
    page.wait_for_selector(".tile")  # folder tree fetched and rendered
    yield page
    context.close()


def test_search_results_show_thumbnail(search_page):
    """H10: a search result whose metadata has no embedded thumbnail renders a tile
    <img> backed by the folder's tree thumbnail (/thumb/...?w=200), not the 📁
    placeholder.

    RED pre-fix (placeholder div), GREEN post-fix (tree-thumb fallback).
    """
    with search_page.expect_response(lambda r: "/api/v1/search" in r.url):
        search_page.locator("#search-input").fill("テスト")
    # .tile-metadata only exists on SEARCH tiles (the root grid's tiles lack
    # it), so this proves the search render — not a stale root tile.
    search_page.wait_for_selector("#tile-grid .tile-metadata", timeout=5000)
    assert search_page.locator("#tile-grid .tile").count() == 1

    tile = search_page.locator("#tile-grid .tile").first
    img = tile.locator("img.tile-img")
    assert img.count() == 1, "search tile has no <img> — still rendering the placeholder"
    src = img.get_attribute("src")
    assert src and src.startswith("/thumb/"), f"expected tree-thumb src, got {src!r}"
    assert tile.locator(".tile-placeholder").count() == 0

    # The fallback thumb should actually be served and decodable.
    img.scroll_into_view_if_needed()
    search_page.wait_for_function(
        """(src) => {
          const i = document.querySelector('#tile-grid .tile img.tile-img');
          return i && i.getAttribute('src') === src && i.complete && i.naturalWidth > 0;
        }""",
        arg=src,
        timeout=3000,
    )


# ── F-key: closing the sidebar must hide+inert the resume pill ────────────────


def test_f_key_close_inerts_resume_pill(page):
    """F-key regression: closing the sidebar with the "f" shortcut must take
    the resume pill out of the view AND the tab order (inert), exactly like
    closeSidebar/toggleSidebar do. The F-key handler used to toggle the
    sidebar classes and sync the sidebar's own a11y attributes but skipped
    hideResumeBtn, leaving an inert-less, still-visible resume pill floating
    over a closed sidebar.

    RED pre-fix (no inert on #resume-btn after "f"), GREEN post-fix.
    """
    # Seed a reading position so the resume pill is eligible, then reload so
    # the init flow (loadFolders → renderView → updateResumeButton) shows it.
    page.evaluate(
        """() => localStorage.setItem('bookq_progress', JSON.stringify(
             {'simple_manga': {page: 1, total: 5, ts: Date.now()}}
           ))"""
    )
    page.reload()
    page.wait_for_selector(".tile")

    resume = page.locator("#resume-btn")
    assert resume.is_visible(), "resume pill should show with sidebar open + saved progress"
    assert resume.get_attribute("inert") is None

    page.keyboard.press("f")  # sidebar toggle — closes it
    assert not page.locator("#sidebar").evaluate("el => el.classList.contains('open')"), (
        "sidebar should be closed after the F-key toggle"
    )
    assert resume.get_attribute("inert") is not None, (
        "resume pill must be inert after the F-key closes the sidebar"
    )


# ── Search cards: tree-thumb fallback in card view mode ───────────────────────


def test_search_cards_show_thumbnail(search_page):
    """H10 in card view mode: a search result whose metadata has no embedded thumbnail
    must render a .card <img> backed by the folder's tree thumbnail
    (/thumb/...?w=400), not the 📁 placeholder — renderSearchCards has to mirror
    renderSearchTiles/renderCards.

    RED pre-fix (placeholder div only), GREEN post-fix.
    """
    # tile → list → card (two clicks from the tile default).
    search_page.locator("#footer-refresh-btn").click()
    search_page.locator("#footer-refresh-btn").click()
    search_page.wait_for_selector("#tile-grid.card-mode")

    with search_page.expect_response(lambda r: "/api/v1/search" in r.url):
        search_page.locator("#search-input").fill("テスト")
    search_page.wait_for_selector("#tile-grid .card", timeout=5000)
    # Exactly one result ("sample") — the root grid would show 2+ cards, so
    # count disambiguates search-rendered cards from a stale root render.
    assert search_page.locator("#tile-grid .card").count() == 1

    card = search_page.locator("#tile-grid .card").first
    assert card.get_attribute("data-path") == "sample"
    img = card.locator("img.card-img")
    assert img.count() == 1, "search card has no <img> — still rendering the placeholder"
    src = img.get_attribute("src")
    assert src and src.startswith("/thumb/"), f"expected tree-thumb src, got {src!r}"
    assert card.locator(".tile-placeholder").count() == 0


# ── H7 positive: a genuine current-page failure DOES show ⚠ ───────────────────


@pytest.fixture(scope="module")
def fail404_proxy(server: str):
    """Proxy that 404s exactly /image/mixed_ar/p3.png and forwards everything
    else to the shared (session-scoped) fixture server."""
    yield from _start_path_fail_proxy(server, {"/image/mixed_ar/p3.png": 404})


def test_counter_shows_warning_on_current_page_failure(page, server, fail404_proxy):
    """H7 positive: a GENUINE load failure of the CURRENT page's image — a
    real HTTP 404 so naturalWidth === 0 and the src matches the current page —
    must append ⚠ to the counter.

    Characterization test: the H7 gate (idx === currentIdx, naturalWidth ===
    0, src match) is already in place, so RED pre-fix does not apply; this
    locks the positive path, and GREEN on first run is expected.
    """
    # Page origin = the proxy, so every image request goes through it.
    page.goto(fail404_proxy + "/#/view/mixed_ar?p=0")
    page.wait_for_selector("#img-track img.loaded")

    # Two NEXT taps (left zone, RTL) land on page 3 = p3.png, the proxied 404.
    page.locator("#zone-prev").click()
    page.locator("#zone-prev").click()
    page.wait_for_function(
        """() => {
          const imgs = [...document.querySelectorAll('#img-track img')];
          const cur = imgs.find(i => i.getAttribute('src') === '/image/mixed_ar/p3.png');
          return !!cur && cur.complete && cur.naturalWidth === 0;
        }""",
        timeout=15000,
    )

    counter = page.text_content("#counter") or ""
    assert WARNING_MARK in counter, f"expected ⚠ for a genuine current-page 404, got: {counter!r}"


# ── transient error: chapter state must reset (not keep the previous book) ─────


@pytest.fixture(scope="module")
def fail500_proxy(server: str):
    """Proxy that 500s every /api/images request (any ?folder= query) and
    forwards everything else to the shared fixture server."""
    yield from _start_path_fail_proxy(server, {"/api/v1/images": 500})


def test_transient_error_resets_chapter_state(page, server, fail500_proxy):
    """A transient error (HTTP 500 from /api/images) must leave the viewer in
    a consistent error state: toast visible, counter empty (not a stale value
    from the previous book), and no crash on further navigation.

    Characterization test: loadImages clears the counter at entry and all
    navigation is guarded by images.length, so these observable invariants
    hold pre-fix too; the fix itself (resetting chapterList/chapterBoundaries
    in the non-404 catch branch, so the error state cannot point at the
    previous book) is internal IIFE state and verified structurally.
    """
    # Populate chapter state by opening a book first.
    page.goto(fail500_proxy + "/#/view/long?p=0")
    page.wait_for_selector("#img-track img.loaded")
    assert "1 / 30" in (page.text_content("#counter") or "")

    # "group" is a non-leaf node, so loadImages takes the /api/images fetch
    # path (a leaf folder is served from the tree and never fetches).
    page.goto(fail500_proxy + "/#/view/group?p=0")
    page.wait_for_selector("#toast.visible", timeout=5000)
    assert "500" in (page.text_content("#toast") or ""), "expected the 500 toast"

    counter = page.text_content("#counter") or ""
    assert counter == "", f"counter must be empty after the failed load, got: {counter!r}"

    # No crash: page turns and Home stay guarded with no images loaded.
    page.keyboard.press("ArrowLeft")
    page.keyboard.press("ArrowRight")
    page.keyboard.press("Home")
    assert page.evaluate("() => document.getElementById('counter').textContent") == ""
