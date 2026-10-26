"""API contract tests for GET/PUT /api/folder-visibility.

Covers: auth (401), owner header (403 missing / 409 mismatch), strict input
validation (400/415), root existence/type checks (404/403 via _is_within),
per-user isolation + email normalization, idempotent writes, missing-root
unhide, generic 503 on store failure, private no-store + Vary: Cookie headers,
and the index template's opaque owner context.

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt \\
        pytest tests/test_folder_visibility_api.py -q
"""

import os
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

os.environ["BOOKQ_TEST_MODE"] = "true"

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from app.auth import require_auth  # noqa: E402
from app.folder_visibility import (  # noqa: E402
    FolderVisibilityStore,
    visibility_owner,
)

OWNER_HEADER = "X-Bookq-Visibility-Owner"
SECRET = "fv-test-secret"


@pytest.fixture
def library(tmp_path: Path) -> Path:
    root = tmp_path / "downloads"
    (root / "作品A").mkdir(parents=True)
    (root / "series").mkdir()
    (root / "book.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    (root / "comic.cbz").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    (root / "doc.pdf").write_bytes(b"%PDF-1.4")
    (root / "cover.png").write_bytes(b"\x89PNG")
    (root / "notes.txt").write_text("not a work")
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(str(outside), root / "escape")
    os.symlink("loop", root / "loop")  # self-referential symlink
    return root


@pytest.fixture
def make_client(library: Path, tmp_path: Path, monkeypatch):
    """Build a TestClient; each call gets a fresh app over the same state dir."""
    state_dir = tmp_path / "state"
    monkeypatch.setenv("DOWNLOADS_DIR", str(library))
    monkeypatch.setenv("BOOKQ_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SECRET_KEY", SECRET)

    def _make(email: str | None = "alice@example.com", unauth: bool = False,
              state_dir: Path | None = None) -> TestClient:
        if state_dir is not None:
            monkeypatch.setenv("BOOKQ_STATE_DIR", str(state_dir))
        from app.main import create_app

        app = create_app()
        if unauth:
            def _deny():
                raise HTTPException(status_code=401, detail="Not authenticated")

            app.dependency_overrides[require_auth] = _deny
        else:
            app.dependency_overrides[require_auth] = (
                lambda: {"email": email} if email is not None else {}
            )
        return TestClient(app)

    return _make


def _owner(client: TestClient) -> str:
    resp = client.get("/api/folder-visibility")
    assert resp.status_code == 200
    return resp.json()["owner"]


def _put(client: TestClient, root: str, hidden: bool, owner: str):
    return client.put(
        "/api/folder-visibility",
        json={"root": root, "hidden": hidden},
        headers={OWNER_HEADER: owner},
    )


def _private_headers(resp) -> None:
    assert resp.headers.get("Cache-Control") == "private, no-store"
    assert resp.headers.get("Vary") == "Cookie"


# ── auth / headers ────────────────────────────────────────────────────


def test_get_requires_auth(make_client):
    client = make_client(unauth=True)
    resp = client.get("/api/folder-visibility")
    assert resp.status_code == 401
    _private_headers(resp)


def test_put_requires_auth(make_client):
    client = make_client(unauth=True)
    resp = client.put("/api/folder-visibility", json={"root": "作品A", "hidden": True})
    assert resp.status_code == 401


def test_get_empty_snapshot(make_client):
    client = make_client()
    resp = client.get("/api/folder-visibility")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["owner"], str) and body["owner"]
    assert body["revision"] == 0
    assert body["hiddenRoots"] == []
    _private_headers(resp)


def test_user_query_param_cannot_select_another_user(make_client):
    client = make_client()
    resp = client.get("/api/folder-visibility?user=bob@example.com")
    assert resp.status_code == 200
    assert resp.json()["owner"] == _owner(client)


def test_error_responses_are_private_no_store(make_client):
    client = make_client()
    resp = client.put(
        "/api/folder-visibility",
        json={"root": "a//b", "hidden": True},
        headers={OWNER_HEADER: _owner(client)},
    )
    assert resp.status_code == 400
    _private_headers(resp)


# ── happy path ────────────────────────────────────────────────────────


def test_hide_get_unhide_flow(make_client):
    client = make_client()
    owner = _owner(client)

    resp = _put(client, "作品A", True, owner)
    assert resp.status_code == 200
    body = resp.json()
    assert body["revision"] == 1
    assert body["hiddenRoots"] == ["作品A"]
    _private_headers(resp)

    resp = client.get("/api/folder-visibility")
    assert resp.json()["hiddenRoots"] == ["作品A"]

    resp = _put(client, "作品A", False, owner)
    assert resp.status_code == 200
    body = resp.json()
    assert body["revision"] == 2
    assert body["hiddenRoots"] == []


def test_same_value_put_is_idempotent(make_client):
    client = make_client()
    owner = _owner(client)
    _put(client, "作品A", True, owner)
    resp = _put(client, "作品A", True, owner)
    assert resp.status_code == 200
    assert resp.json()["revision"] == 1


def test_state_persists_across_app_instances(make_client):
    client = make_client()
    _put(client, "作品A", True, _owner(client))
    # Fresh app instance over the same BOOKQ_STATE_DIR.
    client2 = make_client()
    resp = client2.get("/api/folder-visibility")
    assert resp.json()["hiddenRoots"] == ["作品A"]
    assert resp.json()["revision"] == 1


def test_supported_root_kinds(make_client):
    client = make_client()
    owner = _owner(client)
    for root in ("作品A", "book.zip", "comic.cbz", "doc.pdf"):
        resp = _put(client, root, True, owner)
        assert resp.status_code == 200, root
    assert sorted(client.get("/api/folder-visibility").json()["hiddenRoots"]) == sorted(
        ["作品A", "book.zip", "comic.cbz", "doc.pdf"]
    )


@pytest.mark.parametrize("root", ["missing", "cover.png", "notes.txt", "a/b", "作品A/sub"])
def test_unsupported_or_missing_root_is_404(make_client, root):
    client = make_client()
    resp = _put(client, root, True, _owner(client))
    assert resp.status_code == 404


def test_outside_symlink_and_loop_do_not_500(make_client):
    client = make_client()
    owner = _owner(client)
    # "escape" scans as a real top-level folder node (symlinked dir stays
    # browsable) — tree membership subsumes containment, so it is hideable.
    resp = _put(client, "escape", True, owner)
    assert resp.status_code == 200
    # "loop" never resolves — not a tree node, never a 500.
    resp = _put(client, "loop", True, owner)
    assert resp.status_code == 404


def test_unhide_allowed_for_gone_root(make_client):
    client = make_client()
    owner = _owner(client)
    _put(client, "作品A", True, owner)
    # Root disappears from the library; the stored row stays and is removable.
    import shutil

    shutil.rmtree(Path(os.environ["DOWNLOADS_DIR"]) / "作品A")
    resp = client.get("/api/folder-visibility")
    assert resp.json()["hiddenRoots"] == ["作品A"]
    resp = _put(client, "作品A", False, owner)
    assert resp.status_code == 200
    assert resp.json()["hiddenRoots"] == []


def test_unhide_never_existed_root(make_client):
    client = make_client()
    resp = _put(client, "never-was", False, _owner(client))
    assert resp.status_code == 200
    assert resp.json()["hiddenRoots"] == []


# ── owner header / user isolation ─────────────────────────────────────


def test_put_missing_owner_header_is_403(make_client):
    client = make_client()
    resp = client.put("/api/folder-visibility", json={"root": "作品A", "hidden": True})
    assert resp.status_code == 403
    _private_headers(resp)


def test_put_owner_mismatch_is_409_and_writes_nothing(make_client):
    alice = make_client(email="alice@example.com")
    bob = make_client(email="bob@example.com")
    bob_owner = _owner(bob)
    # Alice's client sends Bob's owner token (e.g. stale tab after account switch).
    resp = _put(alice, "作品A", True, bob_owner)
    assert resp.status_code == 409
    _private_headers(resp)
    assert alice.get("/api/folder-visibility").json()["hiddenRoots"] == []
    assert bob.get("/api/folder-visibility").json()["hiddenRoots"] == []


def test_users_are_isolated_and_email_is_normalized(make_client):
    alice = make_client(email="Alice@Example.com")
    _put(alice, "作品A", True, _owner(alice))

    bob = make_client(email="bob@example.com")
    assert bob.get("/api/folder-visibility").json()["hiddenRoots"] == []

    # Same person, different casing/whitespace -> same user key, same data.
    alice2 = make_client(email="  alice@example.COM  ")
    resp = alice2.get("/api/folder-visibility")
    assert resp.json()["hiddenRoots"] == ["作品A"]


def test_session_without_email_gets_no_shared_user(make_client, tmp_path):
    client = make_client(email=None)
    resp = client.get("/api/folder-visibility")
    assert resp.status_code == 403
    resp = client.put(
        "/api/folder-visibility",
        json={"root": "作品A", "hidden": True},
        headers={OWNER_HEADER: "anything"},
    )
    assert resp.status_code == 403
    # Nothing was stored under a fallback "unknown" account.
    store = FolderVisibilityStore(Path(os.environ["BOOKQ_STATE_DIR"]) / "folder-visibility.sqlite3")
    snap = store.read("unknown")
    assert snap.revision == 0 and snap.hidden_roots == ()


# ── input validation ──────────────────────────────────────────────────


@pytest.mark.parametrize("hidden", ["true", "false", 1, 0, None, [True]])
def test_hidden_must_be_strict_bool(make_client, hidden):
    client = make_client()
    resp = client.put(
        "/api/folder-visibility",
        json={"root": "作品A", "hidden": hidden},
        headers={OWNER_HEADER: _owner(client)},
    )
    assert resp.status_code == 400


@pytest.mark.parametrize(
    "root",
    ["", "a\\b", "/abs", "a\x00b", ".", "..", ".hidden", "x" * 2049, 123, None, True,
     "a//b", "a/./b", "a/../b", "/a", "a/", "a/.b", ".h/x"],
)
def test_invalid_roots_are_400(make_client, root):
    client = make_client()
    resp = client.put(
        "/api/folder-visibility",
        json={"root": root, "hidden": True},
        headers={OWNER_HEADER: _owner(client)},
    )
    assert resp.status_code == 400


def test_unknown_and_missing_fields_are_400(make_client):
    client = make_client()
    owner = _owner(client)
    for body in (
        {"root": "作品A", "hidden": True, "user": "bob@example.com"},
        {"root": "作品A"},
        {"hidden": True},
        {},
    ):
        resp = client.put("/api/folder-visibility", json=body, headers={OWNER_HEADER: owner})
        assert resp.status_code == 400, body


@pytest.mark.parametrize("body", [[1, 2], "x", 42, True])
def test_non_object_body_is_400(make_client, body):
    client = make_client()
    resp = client.put("/api/folder-visibility", json=body, headers={OWNER_HEADER: _owner(client)})
    assert resp.status_code == 400


def test_malformed_json_is_400(make_client):
    client = make_client()
    resp = client.put(
        "/api/folder-visibility",
        data="{not json",
        headers={"Content-Type": "application/json", OWNER_HEADER: _owner(client)},
    )
    assert resp.status_code == 400


def test_non_json_content_type_is_415(make_client):
    client = make_client()
    owner = _owner(client)
    resp = client.put(
        "/api/folder-visibility",
        data='{"root": "作品A", "hidden": true}',
        headers={"Content-Type": "text/plain", OWNER_HEADER: owner},
    )
    assert resp.status_code == 415
    resp = client.put(
        "/api/folder-visibility",
        content='{"root": "作品A", "hidden": true}'.encode("utf-8"),
        headers={OWNER_HEADER: owner},
    )
    assert resp.status_code == 415


# ── concurrency semantics / failures ──────────────────────────────────


def test_different_roots_accumulate_same_root_last_write_wins(make_client):
    client = make_client()
    owner = _owner(client)
    _put(client, "作品A", True, owner)
    resp = _put(client, "series", True, owner)
    assert resp.status_code == 200
    assert sorted(resp.json()["hiddenRoots"]) == ["series", "作品A"]
    assert resp.json()["revision"] == 2
    resp = _put(client, "作品A", False, owner)
    assert resp.json()["hiddenRoots"] == ["series"]
    assert resp.json()["revision"] == 3


def test_store_failure_is_generic_503(make_client, tmp_path):
    blocker = tmp_path / "state-file"
    blocker.write_text("not a directory")
    client = make_client(state_dir=blocker)
    resp = client.get("/api/folder-visibility")
    assert resp.status_code == 503
    _private_headers(resp)
    assert "作品A" not in resp.text
    assert "alice" not in resp.text
    owner = visibility_owner("alice@example.com", SECRET)
    resp = client.put(
        "/api/folder-visibility",
        json={"root": "作品A", "hidden": True},
        headers={OWNER_HEADER: owner},
    )
    assert resp.status_code == 503


# ── index owner context ───────────────────────────────────────────────


def test_index_embeds_opaque_owner_not_email(make_client):
    # index() uses get_current_user (test-mode mock test@example.com).
    client = make_client()
    resp = client.get("/")
    assert resp.status_code == 200
    expected = visibility_owner("test@example.com", SECRET)
    line = next(
        (l for l in resp.text.splitlines() if "__BOOKQ_VISIBILITY_OWNER__" in l),
        None,
    )
    assert line is not None
    assert f'"{expected}"' in line
    assert "test@example.com" not in line


# ── flattened scanner roots ────────────────────────────────────────────


@pytest.fixture
def flat_client(tmp_path: Path, monkeypatch):
    """A library whose ONLY root entry flattens: wrap/ holds the works.

    scan_tree(root) sees exactly one dir and no files/archives/pdfs, so its
    children hoist up with ``wrap/``-prefixed names — ``wrap/wA`` IS a
    top-level tree node while ``wrap`` itself is not a node at all. The
    archive inside wrap hoists the same way → ``wrap/arc.zip``.
    """
    root = tmp_path / "flat_downloads"
    for w in ("wA", "wB"):
        (root / "wrap" / w).mkdir(parents=True)
        (root / "wrap" / w / "1.png").write_bytes(b"\x89PNG")
    (root / "wrap" / "arc.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    monkeypatch.setenv("DOWNLOADS_DIR", str(root))
    monkeypatch.setenv("BOOKQ_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SECRET_KEY", SECRET)
    from app.main import create_app

    app = create_app()
    app.dependency_overrides[require_auth] = lambda: {"email": "alice@example.com"}
    return TestClient(app)


def test_flattened_root_is_hideable_by_node_name(flat_client):
    """``wrap/wA`` is a real top-level tree node — hideable by its full name."""
    owner = _owner(flat_client)
    resp = _put(flat_client, "wrap/wA", True, owner)
    assert resp.status_code == 200
    assert resp.json()["hiddenRoots"] == ["wrap/wA"]
    resp = flat_client.get("/api/folder-visibility")
    assert resp.json()["hiddenRoots"] == ["wrap/wA"]


def test_flattened_non_node_paths_are_404(flat_client):
    """``wrap`` was flattened away (never a node); deep paths are not roots."""
    owner = _owner(flat_client)
    assert _put(flat_client, "wrap", True, owner).status_code == 404
    assert _put(flat_client, "wrap/wA/1.png", True, owner).status_code == 404


def test_flattened_root_unhide_roundtrip(flat_client):
    owner = _owner(flat_client)
    _put(flat_client, "wrap/wA", True, owner)
    resp = _put(flat_client, "wrap/wA", False, owner)
    assert resp.status_code == 200
    assert resp.json()["hiddenRoots"] == []


def test_flattened_archive_root_is_hideable(flat_client):
    """A flattened archive name (multi-segment + extension) validates by the
    same tree-membership rule — no special casing for .zip/.pdf."""
    owner = _owner(flat_client)
    resp = _put(flat_client, "wrap/arc.zip", True, owner)
    assert resp.status_code == 200
    assert resp.json()["hiddenRoots"] == ["wrap/arc.zip"]


# ── owner header bytes safety ──────────────────────────────────────────


def test_non_ascii_owner_header_is_409_not_500(make_client):
    """hmac.compare_digest on a non-ASCII str raises TypeError — the route
    compares encoded bytes, so a non-ASCII header value mismatches to 409
    instead of crashing to a 500."""
    client = make_client()
    resp = client.put(
        "/api/folder-visibility",
        json={"root": "作品A", "hidden": True},
        headers=[(OWNER_HEADER.encode("ascii"), "öwner-tōken".encode("utf-8"))],
    )
    assert resp.status_code == 409
    _private_headers(resp)
