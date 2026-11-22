"""User directory + `/admin` gate (#608).

The `users` table is the only membership source (no env allowlist). These
tests exercise the production binary end to end:

- test-mode server (mock user) for the page / menu / CLI contract
- real-auth server (signed session cookie + Bearer) for the membership and
  role matrix, including live add/disable without a restart.

Each test resets the shared module state to `test@example.com` = member and
`outside@example.com` = absent. Do not share the session-scoped `server`
fixture: its state dir is shared by unrelated tests.

The #613 UI tests use their own `ui_server` / `ui_state` fixtures, where the
mock user is an admin and `OUTSIDE` is an active member; they do not share
the session-scoped `server` fixture.
"""

import base64
import hashlib
import hmac
import http.client
import json
import time
import urllib.parse

import pytest

from rs_spawn import spawn_backend, users_cli

TEST_EMAIL = "test@example.com"
OUTSIDE = "outside@example.com"
SECRET = "ci-not-a-secret"


def _request(server, method, path, headers=None, body=None):
    """One raw HTTP request — no redirect following, so 303 is observable."""
    u = urllib.parse.urlsplit(server)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
    finally:
        conn.close()


def _wait_http(server, method, path, headers, want_status, timeout=5.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        status, _, body = _request(server, method, path, headers)
        last = (status, body)
        if status == want_status:
            return status, body
        time.sleep(0.1)
    assert last is not None and last[0] == want_status, f"{path}: {last}"


def _wait_api(server, headers, want_status, timeout=5.0):
    return _wait_http(server, "GET", "/api/v1/version", headers, want_status, timeout)


def _wait_home(server, headers=None, want_link=True, timeout=5.0):
    deadline = time.time() + timeout
    last_status = None
    last_body = b""
    while time.time() < deadline:
        status, _, body = _request(server, "GET", "/", headers)
        last_status, last_body = status, body
        if status == 200 and (b'id="admin-link"' in body) == want_link:
            return body
        time.sleep(0.1)
    raise AssertionError(
        f"/ admin-link want={want_link}: status={last_status} body={last_body[:400]!r}"
    )


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


def _cookie_header(email):
    return {"Cookie": f"session={_session_cookie(_user(email))}"}


def _form_headers(server, **overrides):
    host = urllib.parse.urlsplit(server).netloc
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": f"http://{host}",
        "Sec-Fetch-Site": "same-origin",
    }
    headers.update(overrides)
    return headers


def _post_admin(server, action, email, headers=None):
    data = urllib.parse.urlencode({"email": email}).encode("utf-8")
    return _request(
        server,
        "POST",
        f"/admin/users/{action}",
        headers if headers is not None else _form_headers(server),
        data,
    )


def _row(body, email):
    needle = email.encode("utf-8")
    for line in body.splitlines():
        if needle in line and line.lstrip().startswith(b"<tr>"):
            return line
    raise AssertionError(f"row not found: {email}")


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
def reset_test_user(admin_state, admin_server):
    """Ensure the mock user is an active member (never admin) around a test."""
    users_cli(admin_state, "add", TEST_EMAIL, "--role", "member")
    users_cli(admin_state, "remove", OUTSIDE)
    yield
    users_cli(admin_state, "add", TEST_EMAIL, "--role", "member")
    users_cli(admin_state, "remove", OUTSIDE)


def test_member_is_not_admin(admin_server, admin_state, reset_test_user):
    status, headers, body = _request(admin_server, "GET", "/admin")
    assert status == 404
    # Generic body: the admin surface is not advertised.
    assert b"admin" not in body.lower()
    assert "管理".encode("utf-8") not in body
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "same-origin"
    assert "cookie" in headers.get("vary", "").lower()

    body = _wait_home(admin_server, want_link=False)
    assert b'id="tile-grid"' in body
    cp = users_cli(admin_state, "check", TEST_EMAIL)
    assert cp.returncode == 0
    assert cp.stdout.strip() == "member"


def test_admin_role_opens_page_and_menu(admin_server, admin_state, reset_test_user):
    cp = users_cli(admin_state, "add", TEST_EMAIL, "--role", "admin")
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.strip() == f"updated {TEST_EMAIL} role=admin"

    status, headers, body = _request(admin_server, "GET", "/admin")
    assert status == 200
    assert "ユーザーを追加".encode("utf-8") in body
    assert headers["content-type"].startswith("text/html")
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "same-origin"
    assert headers["x-frame-options"] == "DENY"
    csp = headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert headers["x-robots-tag"] == "noindex"
    assert "cookie" in headers.get("vary", "").lower()

    home = _wait_home(admin_server, want_link=True)
    assert b'href="/admin"' in home

    # Both spellings map to the same gate.
    assert _request(admin_server, "GET", "/admin/")[0] == 200

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

    cp = users_cli(admin_state, "list")
    assert cp.returncode == 0, cp.stderr
    row = next(
        line.split("\t") for line in cp.stdout.splitlines() if line.startswith(TEST_EMAIL + "\t")
    )
    assert len(row) == 6
    assert row[0] == TEST_EMAIL
    assert row[1] == "admin"
    assert row[2] == "active"
    assert row[3].endswith("GMT")

    cp = users_cli(admin_state, "check", TEST_EMAIL)
    assert cp.returncode == 0
    assert cp.stdout.strip() == "admin"


def test_demote_and_disable(admin_server, admin_state, reset_test_user):
    assert users_cli(admin_state, "add", TEST_EMAIL, "--role", "admin").returncode == 0
    assert _request(admin_server, "GET", "/admin")[0] == 200

    assert users_cli(admin_state, "add", TEST_EMAIL, "--role", "member").returncode == 0
    assert _request(admin_server, "GET", "/admin")[0] == 404
    _wait_home(admin_server, want_link=False)

    cp = users_cli(admin_state, "remove", TEST_EMAIL)
    assert cp.returncode == 0
    assert cp.stdout.strip() == f"disabled {TEST_EMAIL}"
    assert _request(admin_server, "GET", "/admin")[0] == 404
    cp = users_cli(admin_state, "check", TEST_EMAIL)
    assert cp.returncode == 1
    assert cp.stdout.strip() == "disabled"

    cp = users_cli(admin_state, "add", TEST_EMAIL, "--role", "admin")
    assert cp.returncode == 0
    assert cp.stdout.strip() == f"reactivated {TEST_EMAIL} role=admin"
    assert _request(admin_server, "GET", "/admin")[0] == 200


def test_users_cli_contract(tmp_path):
    state = tmp_path / "cli-state"
    cp = users_cli(state, "list")
    assert cp.returncode == 0
    assert cp.stdout == ""
    assert str(state) in cp.stderr

    cp = users_cli(state, "add", "a@b.com", "--note", "initial", "--by", "t")
    assert cp.returncode == 0
    assert cp.stdout.strip() == "added a@b.com role=member"
    cp = users_cli(state, "check", "a@b.com")
    assert cp.returncode == 0 and cp.stdout.strip() == "member"

    cp = users_cli(state, "add", "a@b.com", "--role", "admin")
    assert cp.returncode == 0
    assert cp.stdout.strip() == "updated a@b.com role=admin"
    cp = users_cli(state, "add", "a@b.com", "--role", "admin")
    assert cp.returncode == 0 and cp.stdout.strip() == "already a@b.com role=admin"

    cp = users_cli(state, "remove", "a@b.com")
    assert cp.returncode == 0 and cp.stdout.strip() == "disabled a@b.com"
    cp = users_cli(state, "remove", "a@b.com")
    assert cp.returncode == 0 and cp.stdout.strip() == "already disabled: a@b.com"
    cp = users_cli(state, "check", "a@b.com")
    assert cp.returncode == 1 and cp.stdout.strip() == "disabled"

    cp = users_cli(state, "add", "a@b.com")
    assert cp.returncode == 0 and cp.stdout.strip() == "reactivated a@b.com role=member"

    cp = users_cli(state, "list")
    assert cp.returncode == 0
    # Keep the trailing empty note column (a bare .strip() would eat the tab).
    cols = cp.stdout.splitlines()[0].split("\t")
    assert len(cols) == 6
    assert cols[0] == "a@b.com"
    assert cols[1] == "member"
    assert cols[2] == "active"
    assert cols[3].endswith("GMT")
    assert cols[4] == "t"
    assert cols[5] == "initial"

    assert users_cli(state, "add", "not-an-email").returncode == 2
    assert users_cli(state, "add", "a@b.com", "--role", "owner").returncode == 2
    assert users_cli(state, "check", "not-an-email").returncode == 2
    assert users_cli(state, "add").returncode == 2
    cp = users_cli(state, "--help")
    assert cp.returncode == 0
    assert "usage: viewer-rs users" in cp.stdout


def test_admin_template_is_not_public(admin_server):
    # The page lives under templates/ and is only served by the gated route.
    for path in (
        "/templates/admin.html",
        "/static/admin.html",
        "/static/%2e%2e/templates/admin.html",
    ):
        status, _, body = _request(admin_server, "GET", path)
        assert status != 200, path
        assert "ユーザーを追加".encode("utf-8") not in body


def test_cli_first_bootstrap_survives_restart(library_tree, tmp_path):
    state = tmp_path / "state"
    # CLI-first: enroll the admin before the server starts.
    cp = users_cli(state, "add", TEST_EMAIL, "--role", "admin")
    assert cp.returncode == 0, cp.stderr

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


def test_retired_allowed_emails_env_neither_seeds_nor_authorizes(
    library_tree, tmp_path
):
    """#610: an inherited legacy allowlist must not enroll or authorize."""
    state = tmp_path / "legacy-env-state"
    proc, base_url = spawn_backend(
        library_dir=library_tree,
        state_dir=state,
        test_mode=False,
        env_extra={"ALLOWED_EMAILS": OUTSIDE},
    )
    try:
        # The retired env is ignored at startup: nobody is seeded.
        cp = users_cli(state, "list")
        assert cp.returncode == 0, cp.stderr
        assert cp.stdout == ""
        headers = _cookie_header(OUTSIDE)
        _wait_api(base_url, headers, 401)
        assert _request(base_url, "GET", "/admin", headers)[0] == 303

        # CLI enrollment still authorizes; the UI is member-only.
        cp = users_cli(state, "add", OUTSIDE)
        assert cp.returncode == 0, cp.stderr
        _wait_api(base_url, headers, 200)
        assert _request(base_url, "GET", "/admin", headers)[0] == 404
    finally:
        proc.terminate()
        proc.wait(timeout=5)


# ── #613: /admin user management UI ────────────────────────────────────────

NEW_USER = "new-member@example.com"
CSRF_USER = "csrf-member@example.com"
XSS_USER = "<img@example.com>"


@pytest.fixture(scope="module")
def ui_state(tmp_path_factory):
    return tmp_path_factory.mktemp("admin-ui-state")


@pytest.fixture(scope="module")
def ui_server(library_tree, ui_state):
    proc, base_url = spawn_backend(library_dir=library_tree, state_dir=ui_state)
    yield base_url
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def ui_reset(ui_state, ui_server):
    """Admin actor + a member target; restore a safe state after each test."""
    users_cli(ui_state, "add", TEST_EMAIL, "--role", "admin")
    users_cli(ui_state, "add", OUTSIDE)
    users_cli(ui_state, "remove", NEW_USER)
    users_cli(ui_state, "remove", CSRF_USER)
    users_cli(ui_state, "remove", XSS_USER)
    yield
    users_cli(ui_state, "add", TEST_EMAIL, "--role", "member")
    users_cli(ui_state, "remove", OUTSIDE)
    users_cli(ui_state, "remove", NEW_USER)
    users_cli(ui_state, "remove", CSRF_USER)
    users_cli(ui_state, "remove", XSS_USER)


def test_admin_page_lists_users_and_forms(ui_server, ui_reset):
    status, headers, body = _request(ui_server, "GET", "/admin")
    assert status == 200
    assert headers["cache-control"] == "no-store"
    csp = headers["content-security-policy"]
    assert "form-action 'self'" in csp
    assert "script-src 'none'" in csp
    assert b'action="/admin/users/add"' in body
    assert b'<form class="admin-add" method="post" action="/admin/users/add">' in body
    assert b'name="email"' in body
    assert b"admin-users" in body

    member_row = _row(body, OUTSIDE)
    assert b'action="/admin/users/disable"' in member_row
    admin_row = _row(body, TEST_EMAIL)
    assert b"/admin/users/disable" not in admin_row
    assert b"/admin/users/enable" not in admin_row
    assert b"CLI" in admin_row


def test_admin_status_message_and_unknown_ignored(ui_server, ui_reset):
    status, _, body = _request(ui_server, "GET", "/admin?status=added")
    assert status == 200
    assert "ユーザーを追加しました".encode("utf-8") in body
    status, _, body = _request(ui_server, "GET", "/admin?status=bogus")
    assert status == 200
    assert b"admin-flash" not in body
    _, _, body = _request(ui_server, "GET", "/admin?status=admin-readonly")
    assert b"admin-flash-error" in body
    _, _, body = _request(ui_server, "GET", "/admin?status=exists")
    assert b"admin-flash-info" in body


def test_admin_add_member_then_disable_and_enable(ui_server, ui_state, ui_reset):
    status, headers, _ = _post_admin(ui_server, "add", NEW_USER)
    assert status == 303
    assert headers["location"] == "/admin?status=added"
    cp = users_cli(ui_state, "check", NEW_USER)
    assert cp.returncode == 0 and cp.stdout.strip() == "member"

    _, _, body = _request(ui_server, "GET", "/admin")
    row = _row(body, NEW_USER)
    assert b"/admin/users/disable" in row
    assert b"admin-status-active" in row

    status, headers, _ = _post_admin(ui_server, "disable", NEW_USER)
    assert status == 303
    assert headers["location"] == "/admin?status=disabled"
    cp = users_cli(ui_state, "check", NEW_USER)
    assert cp.returncode == 1 and cp.stdout.strip() == "disabled"
    _, _, body = _request(ui_server, "GET", "/admin")
    row = _row(body, NEW_USER)
    assert b"/admin/users/enable" in row
    assert b"admin-status-disabled" in row

    status, headers, _ = _post_admin(ui_server, "enable", NEW_USER)
    assert status == 303
    assert headers["location"] == "/admin?status=reactivated"
    cp = users_cli(ui_state, "check", NEW_USER)
    assert cp.returncode == 0 and cp.stdout.strip() == "member"


def test_admin_add_existing_member_is_idempotent(ui_server, ui_state, ui_reset):
    status, headers, _ = _post_admin(ui_server, "add", OUTSIDE)
    assert status == 303
    assert headers["location"] == "/admin?status=exists"
    cp = users_cli(ui_state, "check", OUTSIDE)
    assert cp.returncode == 0 and cp.stdout.strip() == "member"


def test_admin_add_admin_is_rejected(ui_server, ui_state, ui_reset):
    status, headers, _ = _post_admin(ui_server, "add", TEST_EMAIL)
    assert status == 303
    assert headers["location"] == "/admin?status=admin-readonly"
    cp = users_cli(ui_state, "check", TEST_EMAIL)
    assert cp.returncode == 0 and cp.stdout.strip() == "admin"


def test_admin_disable_admin_is_rejected(ui_server, ui_state, ui_reset):
    status, headers, _ = _post_admin(ui_server, "disable", TEST_EMAIL)
    assert status == 303
    assert headers["location"] == "/admin?status=admin-readonly"
    cp = users_cli(ui_state, "check", TEST_EMAIL)
    assert cp.returncode == 0 and cp.stdout.strip() == "admin"


def test_admin_invalid_email_and_xss_are_safe(ui_server, ui_state, ui_reset):
    status, headers, _ = _post_admin(ui_server, "add", "not-an-email")
    assert status == 303
    assert headers["location"] == "/admin?status=invalid"
    listing = users_cli(ui_state, "list")
    assert listing.returncode == 0, listing.stderr
    assert "not-an-email" not in listing.stdout

    status, headers, _ = _post_admin(ui_server, "add", XSS_USER)
    assert status == 303
    assert headers["location"] == "/admin?status=added"
    _, _, body = _request(ui_server, "GET", "/admin")
    assert b"<img@example.com>" not in body
    assert b"&lt;img@example.com&gt;" in body


def test_admin_mutations_require_admin(ui_server, ui_state):
    users_cli(ui_state, "add", TEST_EMAIL, "--role", "member")
    try:
        before = users_cli(ui_state, "list")
        assert before.returncode == 0, before.stderr
        assert TEST_EMAIL in before.stdout
        status, _, _ = _post_admin(ui_server, "add", NEW_USER)
        assert status == 404
        after = users_cli(ui_state, "list")
        assert after.returncode == 0, after.stderr
        assert after.stdout == before.stdout
    finally:
        users_cli(ui_state, "add", TEST_EMAIL, "--role", "admin")


def test_admin_action_get_is_405(ui_server, ui_reset):
    status, headers, _ = _request(ui_server, "GET", "/admin/users/add")
    assert status == 405
    assert headers["allow"] == "POST"
    # #613 review: 405 is still an /admin response — keep it out of caches.
    assert headers["cache-control"] == "no-store"


def test_admin_csrf_matrix(ui_server, ui_state, ui_reset):
    # Snapshot before any request: NEW_USER may already exist from an earlier
    # test (CLI `remove` is a soft delete), so "absent" is not the invariant —
    # "unchanged" is.
    listing = users_cli(ui_state, "list")
    assert listing.returncode == 0, listing.stderr
    status, _, _ = _request(
        ui_server,
        "POST",
        "/admin/users/add",
        {"Content-Type": "application/x-www-form-urlencoded"},
        b"email=csrf-missing%40example.com",
    )
    assert status == 403

    for override in (
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
    ):
        status, _, _ = _post_admin(
            ui_server, "add", NEW_USER, _form_headers(ui_server, **override)
        )
        assert status == 403, override

    unchanged = users_cli(ui_state, "list")
    assert unchanged.returncode == 0, unchanged.stderr
    assert unchanged.stdout == listing.stdout
    status, headers, _ = _post_admin(ui_server, "add", CSRF_USER)
    assert status == 303
    assert headers["location"] == "/admin?status=added"
    # Positive control: the listing above is live, so the equality check can
    # actually observe a write.
    control = users_cli(ui_state, "list")
    assert control.returncode == 0, control.stderr
    assert CSRF_USER in control.stdout


def test_admin_post_body_limit(ui_server, ui_reset):
    status, _, _ = _post_admin(ui_server, "add", "a" * 9000 + "@example.com")
    assert status == 413


def test_admin_remaining_flash_branches(ui_server, ui_state, ui_reset):
    assert _post_admin(ui_server, "disable", OUTSIDE)[0] == 303
    status, headers, _ = _post_admin(ui_server, "disable", OUTSIDE)
    assert status == 303
    assert headers["location"] == "/admin?status=already-disabled"

    status, headers, _ = _post_admin(ui_server, "disable", "missing@example.com")
    assert status == 303
    assert headers["location"] == "/admin?status=not-found"

    status, headers, _ = _post_admin(ui_server, "enable", TEST_EMAIL)
    assert status == 303
    assert headers["location"] == "/admin?status=admin-readonly"


# ── real-auth server: membership / role / Bearer matrix ─────────────────────


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
def auth_reset(auth_state, auth_server):
    """Reset the module DB and wait for the 1s directory refresh."""
    users_cli(auth_state, "add", TEST_EMAIL, "--role", "member")
    users_cli(auth_state, "remove", OUTSIDE)
    time.sleep(1.2)
    yield
    users_cli(auth_state, "add", TEST_EMAIL, "--role", "member")
    users_cli(auth_state, "remove", OUTSIDE)


def test_auth_without_cookie_goes_to_login(auth_server, auth_reset):
    status, headers, _ = _request(auth_server, "GET", "/admin")
    assert status == 303
    assert headers["location"] == "/"
    assert headers["cache-control"] == "no-store"

    status, _, body = _request(auth_server, "GET", "/")
    assert status == 200
    assert b"login-box" in body
    assert b'id="tile-grid"' not in body
    assert b'id="admin-link"' not in body
    _wait_api(auth_server, None, 401)


def test_auth_seeded_member_is_member_not_admin(auth_server, auth_reset):
    headers = _cookie_header(TEST_EMAIL)
    _wait_api(auth_server, headers, 200)
    status, _, body = _request(auth_server, "GET", "/", headers)
    assert status == 200
    assert b'id="tile-grid"' in body
    assert b'id="admin-link"' not in body
    assert _request(auth_server, "GET", "/admin", headers)[0] == 404


def test_auth_non_member_session_is_denied(auth_server, auth_reset):
    headers = _cookie_header(OUTSIDE)
    _wait_api(auth_server, headers, 401)
    status, _, body = _request(auth_server, "GET", "/", headers)
    assert status == 200
    assert b"login-box" in body
    assert b'id="tile-grid"' not in body
    assert _request(auth_server, "GET", "/admin", headers)[0] == 303


def test_auth_added_member_is_live(auth_server, auth_state, auth_reset):
    headers = _cookie_header(OUTSIDE)
    _wait_api(auth_server, headers, 401)

    cp = users_cli(auth_state, "add", OUTSIDE)
    assert cp.returncode == 0, cp.stderr
    cp = users_cli(auth_state, "check", OUTSIDE)
    assert cp.returncode == 0 and cp.stdout.strip() == "member"

    # No restart: the 1s directory refresh admits the new member.
    _wait_api(auth_server, headers, 200)
    status, _, body = _request(auth_server, "GET", "/", headers)
    assert status == 200
    assert b'id="tile-grid"' in body
    assert b'id="admin-link"' not in body
    assert _request(auth_server, "GET", "/admin", headers)[0] == 404


def test_auth_admin_role_and_disable(auth_server, auth_state, auth_reset):
    headers = _cookie_header(TEST_EMAIL)
    assert _wait_api(auth_server, headers, 200)[0] == 200
    assert _request(auth_server, "GET", "/admin", headers)[0] == 404

    cp = users_cli(auth_state, "add", TEST_EMAIL, "--role", "admin")
    assert cp.returncode == 0, cp.stderr
    # Admin authorization is a per-request DB check: immediate.
    assert _request(auth_server, "GET", "/admin", headers)[0] == 200
    _wait_home(auth_server, headers, want_link=True)

    cp = users_cli(auth_state, "remove", TEST_EMAIL)
    assert cp.returncode == 0, cp.stderr
    # Revoked membership may resolve as an authenticated non-admin (404) or,
    # after the 1s directory refresh, as unauthenticated (303) — both deny.
    status, _, _ = _request(auth_server, "GET", "/admin", headers)
    assert status in (303, 404), status
    _wait_api(auth_server, headers, 401)
    body = _wait_home(auth_server, headers, want_link=False)
    assert b"login-box" in body


def test_auth_corrupt_signed_session_is_500(auth_server, auth_reset):
    cookie = _corrupt_cookie()
    status, headers, _ = _request(
        auth_server, "GET", "/admin", {"Cookie": f"session={cookie}"}
    )
    assert status == 500
    # #613 review: the gate's corrupt-session branch keeps /admin headers too.
    assert headers["cache-control"] == "no-store"


def test_auth_bearer_member_and_admin(auth_server, auth_state, auth_reset):
    outside = {"Authorization": f"Bearer {_bearer(OUTSIDE)}"}
    assert _request(auth_server, "GET", "/admin", outside)[0] == 303
    _wait_api(auth_server, outside, 401)

    member = {"Authorization": f"Bearer {_bearer(TEST_EMAIL)}"}
    _wait_api(auth_server, member, 200)
    assert _request(auth_server, "GET", "/admin", member)[0] == 404

    cp = users_cli(auth_state, "add", TEST_EMAIL, "--role", "admin")
    assert cp.returncode == 0, cp.stderr
    assert _request(auth_server, "GET", "/admin", member)[0] == 200


def test_admin_post_without_cookie_goes_to_login(auth_server):
    status, headers, _ = _request(
        auth_server,
        "POST",
        "/admin/users/add",
        _form_headers(auth_server),
        b"email=x%40example.com",
    )
    assert status == 303
    assert headers["location"] == "/"


def test_admin_post_as_member_is_404(auth_server, auth_reset):
    headers = _cookie_header(TEST_EMAIL)
    headers.update(_form_headers(auth_server))
    status, _, _ = _request(
        auth_server,
        "POST",
        "/admin/users/add",
        headers,
        urllib.parse.urlencode({"email": OUTSIDE}).encode(),
    )
    assert status == 404


def test_admin_ui_disable_blocks_login(auth_server, auth_state):
    try:
        users_cli(auth_state, "add", TEST_EMAIL, "--role", "admin")
        users_cli(auth_state, "add", OUTSIDE)
        time.sleep(1.2)
        admin_headers = _cookie_header(TEST_EMAIL)
        admin_headers.update(_form_headers(auth_server))
        outside_headers = _cookie_header(OUTSIDE)
        _wait_api(auth_server, outside_headers, 200)

        status, headers, _ = _request(
            auth_server,
            "POST",
            "/admin/users/disable",
            admin_headers,
            urllib.parse.urlencode({"email": OUTSIDE}).encode(),
        )
        assert status == 303
        assert headers["location"] == "/admin?status=disabled"
        _wait_api(auth_server, outside_headers, 401)

        status, headers, _ = _request(
            auth_server,
            "POST",
            "/admin/users/enable",
            admin_headers,
            urllib.parse.urlencode({"email": OUTSIDE}).encode(),
        )
        assert status == 303
        assert headers["location"] == "/admin?status=reactivated"
        _wait_api(auth_server, outside_headers, 200)
    finally:
        users_cli(auth_state, "add", TEST_EMAIL, "--role", "member")
        users_cli(auth_state, "remove", OUTSIDE)


def test_auth_mixed_session_bearer_uses_the_bearer_admin(auth_server, auth_state, auth_reset):
    # A non-member session does not veto a valid Bearer admin; both the shell
    # and `/admin` resolve to the Bearer identity.
    assert users_cli(auth_state, "add", TEST_EMAIL, "--role", "admin").returncode == 0
    headers = {
        "Cookie": f"session={_session_cookie(_user(OUTSIDE))}",
        "Authorization": f"Bearer {_bearer(TEST_EMAIL)}",
    }
    _wait_home(auth_server, headers, want_link=True)
    assert _request(auth_server, "GET", "/admin", headers)[0] == 200
