"""Public-exposure hardening — the unauthenticated surface after #349.

The site is reachable from the public Internet via the Cloudflare Tunnel
(``bookq.mats.dpdns.org``). These tests pin the holes found in the
pre-publication audit:

- ``/auth/logout`` must not just clear the session — it returns a wipe
  interstitial that erases client-held library data (image caches,
  IndexedDB, the cached ``/`` shell entry) before landing on ``/``.
- ``/image/{path}`` plain-file serving must honour the scanner's exposure
  contract (image extensions only, no dotfiles) — a stray ``.html``/``.txt``
  under downloads would otherwise be served same-origin (stored XSS /
  data leak to any authenticated session).
- SVG responses on ``/image/`` (plain and archive entry) must carry the
  same ``default-src 'none'; sandbox`` CSP ``/thumb/`` and ``/view/``
  already apply — a top-level SVG is an active document.
- ``BOOKQ_ENV=production`` disables the interactive API docs, which
  disclose the full endpoint surface to unauthenticated visitors.
- ``sw.js`` source contract: a 401/403 revalidation must evict the cached
  media (logout / expired session / rotated SECRET_KEY), and a successful
  ``/`` navigation must refresh the shell-cache ``/`` entry so the
  authenticated viewer page can't survive logout offline.
"""

import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import _make_png

REPO_ROOT = Path(__file__).resolve().parent.parent
SW_PATH = REPO_ROOT / "app" / "static" / "sw.js"

SVG = '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


@pytest.fixture()
def media_client(tmp_path: Path, monkeypatch):
    """App whose downloads dir mixes servable images with non-image files."""
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    _make_png(downloads / "ok.png")
    _make_png(downloads / ".hidden.png")
    (downloads / "note.txt").write_text("not an image", encoding="utf-8")
    (downloads / "page.html").write_text("<script>alert(1)</script>", encoding="utf-8")
    (downloads / "pic.svg").write_text(SVG, encoding="utf-8")
    (downloads / "work.zip").write_bytes(b"")
    with zipfile.ZipFile(downloads / "work.zip", "w") as zf:
        zf.writestr("a.svg", SVG)
        zf.writestr("b.png", (downloads / "ok.png").read_bytes())
        zf.writestr("readme.txt", "hi")
    monkeypatch.setenv("DOWNLOADS_DIR", str(downloads))
    from app.main import create_app

    return TestClient(create_app())


# ── /auth/logout wipe interstitial ─────────────────────────────────────────


def test_logout_returns_wipe_page_not_redirect(media_client):
    res = media_client.get("/auth/logout")
    assert res.status_code == 200
    # No redirect — the interstitial must run the client-side wipe first.
    assert "offline-core.js" in res.text
    assert "wipeClientData" in res.text
    assert "bookq-shell-" in res.text
    assert res.headers.get("cache-control") == "no-store"


def test_logout_page_is_not_cached_by_sw_shell(media_client):
    """The wipe page itself must never be precached/served stale."""
    res = media_client.get("/auth/logout")
    assert "no-store" in res.headers.get("cache-control", "")


# ── /image/ exposure contract ──────────────────────────────────────────────


def test_image_serves_real_image(media_client):
    res = media_client.get("/image/ok.png")
    assert res.status_code == 200
    assert res.content[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize("name", ["note.txt", "page.html", ".hidden.png"])
def test_image_refuses_non_image_files(media_client, name):
    """Only IMAGE_EXTENSIONS, no dotfiles — same contract as list_images."""
    res = media_client.get(f"/image/{name}")
    assert res.status_code == 404


def test_image_svg_gets_sandbox_csp(media_client):
    res = media_client.get("/image/pic.svg")
    assert res.status_code == 200
    assert res.headers.get("content-type", "").startswith("image/svg")
    assert "sandbox" in res.headers.get("content-security-policy", "")
    assert "default-src 'none'" in res.headers.get("content-security-policy", "")


def test_image_archive_svg_gets_sandbox_csp(media_client):
    res = media_client.get("/image/work.zip/a.svg")
    assert res.status_code == 200
    csp = res.headers.get("content-security-policy", "")
    assert "sandbox" in csp and "default-src 'none'" in csp


def test_image_archive_png_has_no_csp(media_client):
    res = media_client.get("/image/work.zip/b.png")
    assert res.status_code == 200
    assert "content-security-policy" not in res.headers


def test_image_archive_txt_still_404(media_client):
    res = media_client.get("/image/work.zip/readme.txt")
    assert res.status_code == 404


def test_thumb_and_view_refuse_non_image_files(media_client):
    """Same exposure contract on the thumbnail/view source resolvers."""
    for prefix in ("/thumb", "/view"):
        for name in ("note.txt", "page.html", ".hidden.png"):
            res = media_client.get(f"{prefix}/{name}")
            assert res.status_code == 404, f"{prefix}/{name} -> {res.status_code}"


# ── production docs gating ─────────────────────────────────────────────────


def test_production_disables_api_docs(monkeypatch):
    monkeypatch.setenv("BOOKQ_ENV", "production")
    from app.main import create_app

    client = TestClient(create_app())
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path


def test_non_production_keeps_api_docs(monkeypatch):
    monkeypatch.setenv("BOOKQ_ENV", "development")
    from app.main import create_app

    client = TestClient(create_app())
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


# ── sw.js source contract ──────────────────────────────────────────────────


def test_sw_cachefirst_evicts_on_auth_failure():
    """A 401/403 revalidation must drop the cached copy, not serve stale."""
    text = SW_PATH.read_text(encoding="utf-8")
    start = text.find("async function cacheFirst(")
    assert start != -1
    end = text.find("\nasync function ", start + 1)
    block = text[start:end] if end != -1 else text[start:]
    assert "401" in block and "403" in block, (
        "cacheFirst must special-case auth rejection"
    )
    assert "c.delete" in block, "auth rejection must evict the cached entry"
    assert "thumbWidthFallbacks" in block, "thumb width variants must be evicted too"


def test_sw_networkfirst_evicts_on_auth_failure():
    """Same contract for the /api/* NetworkFirst branch: a 401/403 must not
    leave the stale library-metadata copy for the next offline window."""
    text = SW_PATH.read_text(encoding="utf-8")
    start = text.find("async function networkFirst(")
    assert start != -1
    end = text.find("\nasync function ", start + 1)
    block = text[start:end] if end != -1 else text[start:]
    assert "401" in block and "403" in block, (
        "networkFirst must special-case auth rejection"
    )
    assert "c.delete" in block, "auth rejection must evict the cached entry"


def test_sw_navigate_refreshes_root_shell_entry():
    """A successful '/' navigation must overwrite the shell-cache entry."""
    text = SW_PATH.read_text(encoding="utf-8")
    assert "caches.open(SHELL_CACHE)" in text
    assert "new Request('/')" in text
    assert "res.clone()" in text
    assert "url.pathname === '/'" in text
