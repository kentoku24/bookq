"""Regression tests for app.routes._is_within traversal guard (#93 review #5).

Covers the traversal guard used at api_images and serve_image (403 on
escape). Tests exercise the CURRENT implementation (Path.resolve +
is_relative_to / commonpath fallback) without changing its behavior.

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_traversal.py -q
"""

import sys
import urllib.error
import urllib.parse as up
import urllib.request
from pathlib import Path

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from app.routes import _is_within  # noqa: E402


def test_is_within_direct_child(tmp_path: Path):
    base = tmp_path / "downloads"
    base.mkdir()
    target = base / "manga" / "p1.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")
    assert _is_within(base, target) is True


def test_is_within_dotdot_escape(tmp_path: Path):
    base = tmp_path / "downloads"
    base.mkdir()
    # base/../secret escapes base -> must be blocked
    target = base / ".." / "secret" / "p1.png"
    assert _is_within(base, target) is False


def test_is_within_sibling_prefix_bypass(tmp_path: Path):
    # base=/x/downloads must NOT allow /x/downloads_evil/...
    # String prefix "/x/downloads" is a prefix of "/x/downloads_evil" but
    # not a path-parent.
    base = tmp_path / "downloads"
    base.mkdir()
    sibling = tmp_path / "downloads_evil"
    sibling.mkdir()
    target = sibling / "p1.png"
    target.write_bytes(b"x")
    assert _is_within(base, target) is False
    # Also direct string-joined sibling path
    target2 = Path(str(base) + "_evil") / "p1.png"
    # target2 is same as sibling/p1.png already, but ensure is_relative_to logic holds
    assert _is_within(base, target2) is False


def test_is_within_nonexistent_inside(tmp_path: Path):
    base = tmp_path / "downloads"
    base.mkdir()
    # Path does not exist yet, but is logically inside -> True (404 handled downstream)
    target = base / "new_series" / "vol1" / "p1.png"
    assert not target.exists()
    assert _is_within(base, target) is True


def test_is_within_symlink_escape(tmp_path: Path):
    # tmp/outside/secret + tmp/inside/link -> outside, probe inside/link/secret
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret"
    secret.write_bytes(b"secret")
    inside = tmp_path / "inside"
    inside.mkdir()
    link = inside / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as e:
        # Skip on filesystems that disallow symlinks
        import pytest

        pytest.skip(f"symlink not supported: {e}")
    target = link / "secret"
    # Resolved target is outside/secret, not inside -> must be blocked
    assert _is_within(inside, target) is False


def test_is_within_url_decoded_payload(tmp_path: Path):
    base = tmp_path / "downloads"
    base.mkdir()
    # Simulate FastAPI decoding of a URL path segment such as "..%2F..%2Fetc"
    payload = up.unquote("..%2F..%2Fetc%2Fpasswd")
    assert payload == "../../etc/passwd"
    target = base / payload
    assert _is_within(base, target) is False
    # Also single-encoded dot-dot with slash
    payload2 = up.unquote("..%2Fsecret")
    target2 = base / payload2
    assert _is_within(base, target2) is False


def _get_status_body(server: str, target: str):
    try:
        with urllib.request.urlopen(server + target, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_serve_image_bare_filename_flows_to_guards(server: str):
    """Root-loose allowance: a bare filename is a valid shape now.

    ``/image/foo.jpg`` has no folder segment (root-level loose file), so it
    must flow into the _is_within/is_file guards -> 404 here (missing file),
    not 400 "Invalid image path". Serving an existing root-loose file (200)
    is pinned by the reachability matrix (e) on its own tree.
    """
    status, body = _get_status_body(server, "/image/foo.jpg")
    assert status == 404
    assert b"\x89PNG" not in body


def test_serve_image_dotdot_escape_blocked(server: str):
    # Dots stay percent-encoded on the wire so no client/server normalization
    # can collapse them before the guard sees them; they resolve outside the
    # downloads dir -> 403, never file bytes.
    status, body = _get_status_body(server, "/image/%2E%2E/secret.png")
    assert status in (400, 403)
    assert b"\x89PNG" not in body


def test_serve_image_absolute_path_blocked(server: str):
    # A double slash puts the absolute path into the path param, where the
    # kept leading-slash rejection still blocks it.
    status, body = _get_status_body(server, "/image//etc/passwd")
    assert status in (400, 403)
    assert b"root:" not in body


def test_serve_thumb_mirrors_image_guards(server: str):
    status, body = _get_status_body(server, "/thumb/foo.jpg?w=400")
    assert status == 404
    status, body = _get_status_body(server, "/thumb/%2E%2E/secret.png?w=400")
    assert status in (400, 403)
    assert b"\x89PNG" not in body and b"\xff\xd8" not in body
