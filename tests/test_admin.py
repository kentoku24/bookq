"""Admin registry + `/admin` gate (#608).

The registry lives in the shared user-state SQLite DB and is written only by
`viewer-rs admin …`. These tests exercise the production binary end to end:

- test-mode server (mock user) for the page / menu / CLI contract
- real-auth server (signed session cookie + Bearer) for the authorization
  matrix, including the `ALLOWED_EMAILS` re-check (#373) that must also gate
  admin access.

Each test sets up its own grant and removes it again. Do not share the
session-scoped `server` fixture: its state dir is shared by unrelated tests
and an admin row would leak into their `/` HTML.
"""

import base64
import hashlib
import hmac
import http.client
import json
import time
import urllib.parse

import pytest

from rs_spawn import admin_cli, spawn_backend

TEST_EMAIL = "test@example.com"
SECRET = "ci-not-a-secret"


def _request(server, method, path, headers=None):
    """One raw HTTP request — no redirect following, so 303 is observable."""
    u = urllib.parse.urlsplit(server)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
    try:
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, body
    finally:
        conn.close()


def _session_cookie(user, secret=SECRET):
    """Forge the exact cookie starlette SessionMiddleware emits."""
    import itsdangerous

    signer = itsdangerous.TimestampSigner(secret)
    data = base64.b64encode(json.dumps({"user": user}).encode("utf-8"))
    return signer.sign(data).decode("utf-8")


def _corrupt_cookie(secret=SECRET):
    """A validly signed cookie whose payload is not JSON → middleware 500."""
    import itsdangerous

    signer = itsdangerous.TimestampSigner(secret)
    return signer.sign(base64.b64encode(b"not json")).decode("utf-8")


def _bearer(email, secret=SECRET):
    """Mint the same HS256 access JWT `/api/v1/auth/token` issues."""

    def enc(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    now = int(time.time())
    header = enc(b'{"alg":"HS256","typ":"JWT"}')
    payload = enc(
        json.dumps(
            {"sub": email, "iat": now - 5, "exp": now + 3600},
            separators=(",", ":"),
        ).encode("utf-8")
    )
    signature = hmac.new(
        secret.encode("utf-8"),
        f"{header}.{payload}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{header}.{payload}.{enc(signature)}"


def _user(email):
    return {"email": email, "name": "Test User", "picture": ""}


# ── test-mode server: page / menu / CLI contract ────────────────────────────


@pytest.fixture(scope="module")
def admin_state(tmp_path_factory):
    return tmp_path_factory.mktemp("admin-state")


@pytest.fixture(scope="module")
def admin_server(library_tree, admin_state):
    proc, base_url = spawn_backend(library_dir=library_tree, state_dir=admin_state)
    yield base_url
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def no_admin(admin_state):
    admin_cli(admin_state, "remove", TEST_EMAIL)  # exit 1 when absent is fine
    yield
    admin_cli(admin_state, "remove", TEST_EMAIL)


def test_without_row_is_generic_404(admin_server, no_admin):
    status, headers, body = _request(admin_server, "GET", "/admin")
    assert status == 404
    # Generic body: the admin surface is not advertised.
    assert b"admin" not in body.lower()
    assert b"\xe7\xae\xa1\xe7\x90\x86" not in body  # 管理
    assert headers["cache-control"] == "no-store"
    assert "cookie" in headers.get("vary", "").lower()

    status, _, body = _request(admin_server, "GET", "/")
    assert status == 200
    assert b'id="admin-link"' not in body


def test_add_makes_page_live_without_restart(admin_server, admin_state, no_admin):
    cp = admin_cli(
        admin_state, "add", TEST_EMAIL, "--note", "initial", "--by", "tester"
    )
    assert cp.returncode == 0, cp.stderr
    assert "added" in cp.stdout

    status, headers, body = _request(admin_server, "GET", "/admin")
    assert status == 200
    assert "管理機能はまだありません".encode("utf-8") in body
    assert headers["content-type"].startswith("text/html")
    assert headers["cache-control"] == "no-store"
    assert headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-robots-tag"] == "noindex"
    assert "cookie" in headers.get("vary", "").lower()

    # Both spellings map to the same gate.
    assert _request(admin_server, "GET", "/admin/")[0] == 200

    # The menu entry is server-rendered for the admin only.
    status, _, home = _request(admin_server, "GET", "/")
    assert status == 200
    assert b'id="admin-link"' in home
    assert b'href="/admin"' in home

    # HEAD is bodyless; other methods carry Allow.
    status, _, head_body = _request(admin_server, "HEAD", "/admin")
    assert status == 200 and head_body == b""
    status, headers, _ = _request(admin_server, "POST", "/admin")
    assert status == 405
    assert headers["allow"] == "GET, HEAD"

    # Never precache the admin HTML in the Service Worker shell.
    status, _, sw = _request(admin_server, "GET", "/sw.js")
    assert status == 200
    assert b"/admin" not in sw

    cp = admin_cli(admin_state, "list")
    assert cp.returncode == 0, cp.stderr
    cols = cp.stdout.strip().split("\t")
    assert cols[0] == TEST_EMAIL
    assert cols[1].endswith("GMT")
    assert cols[2] == "tester"
    assert cols[3] == "initial"

    assert admin_cli(admin_state, "check", TEST_EMAIL).returncode == 0


def test_remove_revokes_immediately(admin_server, admin_state, no_admin):
    assert admin_cli(admin_state, "add", TEST_EMAIL).returncode == 0
    assert _request(admin_server, "GET", "/admin")[0] == 200

    cp = admin_cli(admin_state, "remove", TEST_EMAIL)
    assert cp.returncode == 0, cp.stderr
    assert "removed" in cp.stdout
    assert _request(admin_server, "GET", "/admin")[0] == 404

    cp = admin_cli(admin_state, "check", TEST_EMAIL)
    assert cp.returncode == 1
    assert cp.stdout.strip() == "not admin"


def test_invalid_input_and_empty_list(admin_state, no_admin):
    cp = admin_cli(admin_state, "add", "not-an-email")
    assert cp.returncode == 2
    assert "invalid email" in cp.stderr

    cp = admin_cli(admin_state, "list")
    assert cp.returncode == 0
    assert cp.stdout == ""

    cp = admin_cli(admin_state, "check", TEST_EMAIL)
    assert cp.returncode == 1
    assert cp.stdout.strip() == "not admin"

    assert admin_cli(admin_state, "check", "not-an-email").returncode == 2
    assert admin_cli(admin_state, "add").returncode == 2
    cp = admin_cli(admin_state, "--help")
    assert cp.returncode == 0
    assert "usage: viewer-rs admin" in cp.stdout


# ── real-auth server: session / Bearer / ALLOWED_EMAILS matrix ──────────────


@pytest.fixture(scope="module")
def auth_state(tmp_path_factory):
    return tmp_path_factory.mktemp("admin-auth-state")


@pytest.fixture(scope="module")
def auth_server(library_tree, auth_state):
    proc, base_url = spawn_backend(
        library_dir=library_tree, state_dir=auth_state, test_mode=False
    )
    yield base_url
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def auth_no_admin(auth_state):
    admin_cli(auth_state, "remove", TEST_EMAIL)
    yield
    admin_cli(auth_state, "remove", TEST_EMAIL)


def test_auth_without_cookie_goes_to_login(auth_server):
    status, headers, _ = _request(auth_server, "GET", "/admin")
    assert status == 303
    assert headers["location"] == "/"
    assert headers["cache-control"] == "no-store"


def test_auth_non_admin_cookie_is_404(auth_server, auth_no_admin):
    cookie = _session_cookie(_user(TEST_EMAIL))
    status, _, body = _request(
        auth_server, "GET", "/admin", {"Cookie": f"session={cookie}"}
    )
    assert status == 404
    assert b"admin" not in body.lower()

    status, _, body = _request(
        auth_server, "GET", "/", {"Cookie": f"session={cookie}"}
    )
    assert status == 200
    assert b'id="admin-link"' not in body


def test_auth_admin_cookie_is_200(auth_server, auth_state, auth_no_admin):
    assert admin_cli(auth_state, "add", TEST_EMAIL).returncode == 0
    cookie = _session_cookie(_user(TEST_EMAIL))

    status, _, body = _request(
        auth_server, "GET", "/admin", {"Cookie": f"session={cookie}"}
    )
    assert status == 200
    assert "管理機能はまだありません".encode("utf-8") in body

    status, _, body = _request(
        auth_server, "GET", "/", {"Cookie": f"session={cookie}"}
    )
    assert status == 200
    assert b'id="admin-link"' in body


def test_auth_allowlist_recheck_blocks_admin_row(auth_server, auth_state):
    outside = "outside@example.com"
    cp = admin_cli(
        auth_state,
        "add",
        outside,
        env_extra={"ALLOWED_EMAILS": TEST_EMAIL},
    )
    assert cp.returncode == 0, cp.stderr
    # The CLI warns (does not refuse) when the grant is not in ALLOWED_EMAILS.
    assert "not in ALLOWED_EMAILS" in cp.stderr
    try:
        cookie = _session_cookie(_user(outside))
        status, headers, _ = _request(
            auth_server, "GET", "/admin", {"Cookie": f"session={cookie}"}
        )
        assert status == 303
        assert headers["location"] == "/"

        # The shell still renders the viewer (existing #373 behavior), but
        # the admin entry uses auth::resolve and must stay hidden.
        status, _, body = _request(
            auth_server, "GET", "/", {"Cookie": f"session={cookie}"}
        )
        assert status == 200
        assert b'id="admin-link"' not in body
    finally:
        admin_cli(auth_state, "remove", outside)


def test_auth_corrupt_signed_session_is_500(auth_server):
    cookie = _corrupt_cookie()
    status, _, _ = _request(
        auth_server, "GET", "/admin", {"Cookie": f"session={cookie}"}
    )
    assert status == 500


def test_auth_bearer_admin_is_200(auth_server, auth_state, auth_no_admin):
    assert admin_cli(auth_state, "add", TEST_EMAIL).returncode == 0
    token = _bearer(TEST_EMAIL)
    status, _, body = _request(
        auth_server,
        "GET",
        "/admin",
        {"Authorization": f"Bearer {token}"},
    )
    assert status == 200
    assert "管理機能はまだありません".encode("utf-8") in body


# ── persistence across a real restart ───────────────────────────────────────


def test_admin_row_survives_server_restart(library_tree, tmp_path):
    state = tmp_path / "state"
    assert admin_cli(state, "add", TEST_EMAIL).returncode == 0

    proc, base_url = spawn_backend(library_dir=library_tree, state_dir=state)
    try:
        assert _request(base_url, "GET", "/admin")[0] == 200
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    proc2, base_url2 = spawn_backend(library_dir=library_tree, state_dir=state)
    try:
        assert _request(base_url2, "GET", "/admin")[0] == 200
    finally:
        proc2.terminate()
        proc2.wait(timeout=5)
