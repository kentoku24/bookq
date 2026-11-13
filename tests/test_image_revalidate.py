"""Image cache revalidation — a replaced page image must reach the client.

Repro of the 2026-10-13 device report (rawmanga/<work>/ch-* rendered at 73% of
the viewport width): the page files on disk carried baked-in #111 side bands
(1140x1188 canvas), and after the library was re-downloaded with clean images
the phone kept rendering the old bytes. Root cause: the SW answered ``/image/``
from Cache storage forever (a work's image URLs never change) without looking at
ETag/Last-Modified, and ``downloadWork`` skips already-cached URLs — so a
replacement on disk could never surface.

Contract pinned here:
  1. replaced file  → the next ``/image/`` GET through the SW returns the NEW bytes
     and swaps the cache entry;
  2. the viewer paints the refreshed bytes after a relaunch (user-visible path);
  3. a missing/failed origin (here: the file was deleted) → the cached copy is
     still served, so revalidation can never turn a readable page into an error;
  4. offline        → the cached copy is still served.

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt \\
        pytest tests/test_image_revalidate.py -q
"""

import hashlib
import http.client
import os
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.conftest import _make_png

REPO_ROOT = Path(__file__).resolve().parent.parent
SW_PATH = REPO_ROOT / "app" / "static" / "sw.js"


def _is_webkit():
    return os.environ.get("BROWSER") == "webkit"


pytestmark = pytest.mark.skipif(
    _is_webkit(),
    reason="SW fetch interception is needed to observe cache revalidation (Chromium-only contract)",
)


def goto_offline(page, server):
    page.goto(server + "/?offline=1")
    page.wait_for_selector(".tile", timeout=10000)


from tests.safari.sw import ensure_sw_interception as _ensure_sw_interception


def wait_for_sw(page, timeout=15000):
    for _ in range(2):
        try:
            page.wait_for_function(
                "() => navigator.serviceWorker && navigator.serviceWorker.controller !== null",
                timeout=4000,
            )
            _ensure_sw_interception(page)
            return True
        except Exception:
            pass
        try:
            page.wait_for_function(
                "() => navigator.serviceWorker && !!navigator.serviceWorker.ready",
                timeout=timeout,
            )
            page.wait_for_timeout(500)
            _ensure_sw_interception(page)
            return True
        except Exception:
            continue
    return False


def open_simple_manga(page):
    """Open the 5-page fixture work and wait for page 1 to be painted."""
    page.locator('.tile[data-path="simple_manga"]').click()
    expect(page.locator("#counter")).to_have_text("1 / 5", timeout=8000)
    img = page.locator("#img-track .page-cell img").first
    expect(img).to_have_class(re.compile(r"\bloaded\b"), timeout=8000)
    return img


def fetch_sha256(page, url):
    """GET *url* from the page (through the SW) and return its SHA-256 hex digest."""
    return page.evaluate(
        """async (u) => {
            const r = await fetch(u);
            const buf = await r.arrayBuffer();
            const h = await crypto.subtle.digest('SHA-256', buf);
            return [...new Uint8Array(h)].map(x => x.toString(16).padStart(2, '0')).join('');
        }""",
        url,
    )


def sha256_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cached(page, url):
    return page.evaluate(
        "async (u) => { const k = await caches.keys(); for (const n of k) { const c = await caches.open(n); if (await c.match(u)) return true; } return false; }",
        url,
    )


def prime(page, url):
    """Make sure the URL is in Cache storage before the test touches the file.

    The viewer's first ``<img>`` load can race SW registration, so the test
    performs one explicit SW-mediated GET and verifies the entry exists.
    """
    fetch_sha256(page, url)
    assert cached(page, url), f"the page image must be cached before the swap: {url}"


def open_work(page, downloads_tree):
    """Open simple_manga and return (image url, on-disk path, <img> locator)."""
    img = open_simple_manga(page)
    url = img.evaluate("e => e.src")
    target = downloads_tree / "simple_manga" / url.rsplit("/", 1)[-1]
    assert target.is_file(), f"fixture image missing: {target}"
    return url, target, img


@pytest.fixture(autouse=True)
def _restore_page1(downloads_tree):
    """Put the mutated fixture page back so the shared tree stays pristine.

    ``downloads_tree`` is session-scoped and every other module reads the same
    ``simple_manga/1.png`` (40x60 red), so a replacement here must not leak.
    """
    target = downloads_tree / "simple_manga" / "1.png"
    original = target.read_bytes()
    yield
    target.write_bytes(original)


def test_replaced_image_bytes_reach_the_client(page, server, downloads_tree):
    goto_offline(page, server)
    assert wait_for_sw(page), "SW did not take control"
    url, target, _img = open_work(page, downloads_tree)

    prime(page, url)
    before = fetch_sha256(page, url)
    assert before == sha256_file(target), "the SW must serve the file currently on disk"

    # Library-side replacement: different pixels → different size → new ETag.
    _make_png(target, w=64, h=96, rgb=(30, 200, 80))
    expected = sha256_file(target)
    assert expected != before

    after = fetch_sha256(page, url)
    assert after == expected, (
        "a replaced image must reach the client; a cache-first /image/ branch pins the old bytes forever"
    )
    assert cached(page, url), "the refreshed bytes must be stored back into Cache storage"


def test_refreshed_bytes_reach_the_viewer(page, server, downloads_tree):
    """The user-visible path: the painted <img> must pick up the replaced file."""
    goto_offline(page, server)
    assert wait_for_sw(page), "SW did not take control"
    url, target, img = open_work(page, downloads_tree)
    prime(page, url)
    assert img.evaluate("e => e.naturalWidth") == 40  # fixture page 1 is 40x60

    _make_png(target, w=64, h=96, rgb=(30, 200, 80))

    goto_offline(page, server)  # relaunch: the viewer builds a fresh <img>
    assert wait_for_sw(page), "SW did not take control after relaunch"
    img = open_simple_manga(page)
    assert img.evaluate("e => e.naturalWidth") == 64, (
        "the viewer must paint the replaced page, not the cached old bytes"
    )


def test_deleted_image_falls_back_to_the_cached_copy(page, server, downloads_tree):
    goto_offline(page, server)
    assert wait_for_sw(page), "SW did not take control"
    url, target, _img = open_work(page, downloads_tree)
    prime(page, url)
    cached_digest = fetch_sha256(page, url)

    # Revalidation must never turn a readable page into an error: a moved or
    # deleted file (404) keeps serving what the client already has.
    target.unlink()
    try:
        assert fetch_sha256(page, url) == cached_digest
    finally:
        _make_png(target, w=40, h=60, rgb=(200, 60, 60))


def test_offline_still_serves_the_cached_copy(page, server, downloads_tree):
    goto_offline(page, server)
    assert wait_for_sw(page), "SW did not take control"
    url, target, _img = open_work(page, downloads_tree)
    prime(page, url)
    cached_digest = fetch_sha256(page, url)

    # Change the file on disk AND cut the network: the cached copy must win,
    # and revalidation must not stall the read.
    _make_png(target, w=64, h=96, rgb=(10, 10, 200))
    assert sha256_file(target) != cached_digest

    page.context.set_offline(True)
    try:
        served = fetch_sha256(page, url)
    finally:
        page.context.set_offline(False)
    assert served == cached_digest, "offline reads must not wait on (or fail from) revalidation"


def test_sw_image_branch_revalidates_with_validators():
    """Source contract: the /image/ hit path is a conditional GET, not a blind trust.

    ``tests/test_sw_offline_contract.py`` pins the shell/install/static branches
    the same way — a browser test can see the refreshed bytes but not which
    request produced them.
    """
    text = SW_PATH.read_text(encoding="utf-8")
    start = text.find("function revalidateRequest(")
    assert start != -1, "the revalidation helper is missing from sw.js"
    end = text.find("\nasync function ", text.find("async function cacheFirst("))
    block = text[start:end] if end != -1 else text[start:]
    assert "cacheFirst" in block, "the revalidation helper must be part of the /image/ branch"
    assert "If-None-Match" in block and "If-Modified-Since" in block, (
        "a cache hit must revalidate with ETag/Last-Modified"
    )
    assert "304" in block, "a 304 must be handled explicitly (keep the cached copy)"
    assert "navigator.onLine" in block, "offline must short-circuit before revalidating"
    assert "AbortController" in block and "REVALIDATE_TIMEOUT_MS" in block, (
        "a slow/unreachable origin must not stall the read"
    )
    assert "no-store" in block, "the HTTP cache must be bypassed (Cache storage + ETag is the authority)"
    assert "res.clone()" in block, "refreshed bytes must be cloned before put()"


class _RecordingProxy(BaseHTTPRequestHandler):
    """Pass-through proxy that records the headers the app actually received.

    A browser test can see *which bytes* came back but not *which request* the SW
    sent, so the revalidation contract has to be observed on the wire.
    """

    protocol_version = "HTTP/1.1"
    upstream = ""
    received: list = []

    def log_message(self, *args):  # silence the default stderr access log
        pass

    def _proxy(self):
        length = self.headers.get("content-length")
        body = self.rfile.read(int(length)) if length else None
        self.received.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
        host, port = urllib.parse.urlsplit(self.upstream).netloc.rsplit(":", 1)
        conn = http.client.HTTPConnection(host, int(port), timeout=30)
        conn.request(self.command, self.path, body=body, headers=dict(self.headers))
        res = conn.getresponse()
        payload = res.read()
        conn.close()
        self.send_response(res.status)
        for key, value in res.getheaders():
            if key.lower() in ("connection", "transfer-encoding", "content-length"):
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    do_GET = do_POST = do_HEAD = _proxy


@pytest.fixture
def recording_proxy(server):
    """Serve the app through a recording proxy: yields (base_url, received_headers)."""
    received: list = []
    handler = type("Handler", (_RecordingProxy,), {"upstream": server, "received": received})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", received
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_nocors_image_load_revalidates_with_validators(page, recording_proxy, downloads_tree):
    """An ``<img>``-shaped (no-cors) hit must revalidate with If-None-Match.

    The viewer loads pages with ``<img>``, i.e. mode:"no-cors". Re-deriving a
    no-cors Request silently drops every non-CORS-safelisted header, so the SW has
    to re-issue the same-origin request in a mode that keeps the validators —
    otherwise the "revalidation" is a full re-download (no 304 ever) and the
    1200ms budget has to cover the whole body, which on a slow origin means the
    replaced image never reaches the client.
    """
    base_url, received = recording_proxy
    goto_offline(page, base_url)
    assert wait_for_sw(page), "SW did not take control"
    url, _target, _img = open_work(page, downloads_tree)
    path = urllib.parse.urlsplit(url).path
    prime(page, url)

    del received[:]  # only the revalidation interests us
    # Same mode as the viewer's <img>, minus the HTML image cache in the way.
    page.evaluate(
        """async (u) => { await fetch(u, {mode: 'no-cors'}).then(r => r.arrayBuffer()).catch(() => {}); }""",
        url,
    )
    hits = [h for p, h in received if p == path]
    assert hits, "a cached image must be revalidated over the network"
    assert any(h.get("if-none-match") for h in hits), (
        "the revalidation must carry If-None-Match; a no-cors Request drops it silently, "
        f"turning every hit into a full re-download. received={hits}"
    )


def test_cover_thumb_is_cached_with_the_work(page, server, downloads_tree):
    """A cover thumb belongs in the same per-work cache prefetch.js writes.

    ``cacheNameFor(folder)`` is where prefetch puts covers and where
    ``caches.match()`` reads them back; storing a refreshed thumb in the fallback
    cache updated a copy nothing ever returns, so the entry that *is* returned
    revalidated (and re-downloaded) forever.
    """
    goto_offline(page, server)
    assert wait_for_sw(page), "SW did not take control"
    # w=800: prefetch.js only writes 200/400, so this URL is untouched by it.
    url = server + "/thumb/simple_manga/1.png?w=800"
    page.evaluate(
        """async (u) => {
            for (const n of await caches.keys()) {
                if (n.startsWith('bookq-shell-')) continue;
                try { await (await caches.open(n)).delete(u); } catch (e) {}
            }
        }""",
        url,
    )
    page.evaluate(
        """async (u) => {
            await new Promise((res) => {
                const im = new Image();
                im.onload = () => res();
                im.onerror = () => res();
                im.src = u;
            });
        }""",
        url,
    )
    where = page.evaluate(
        """async (u) => {
            const out = [];
            for (const n of await caches.keys()) {
                if (n.startsWith('bookq-shell-')) continue;
                if (await (await caches.open(n)).match(u)) out.push(n);
            }
            return out;
        }""",
        url,
    )
    work = page.evaluate("() => window.OfflineCore.cacheNameFor('simple_manga')")
    assert where == [work], f"the thumb must live in the per-work cache only: {where}"
