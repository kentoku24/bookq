"""Library edit API tests — /api/v1/library/* (upload, delete, chunked).

These tests MUTATE the library tree, so they spawn a dedicated viewer-rs
against a private tmp root (same pattern as the folder-visibility write
tests) instead of the shared session `server` fixture.
"""

import io
import os
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))

from rs_spawn import _viewer_rs_binary  # noqa: E402

UPLOAD_DIR_NAME = ".bookq-uploads"


def _zip_bytes(member: str = "p.png") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(member, b"\x89PNG\r\n\x1a\nfake")
    return buf.getvalue()


@pytest.fixture(scope="module")
def lib_root(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("lib-edit")
    (root / "series").mkdir()
    (root / "series" / "work.cbz").write_bytes(_zip_bytes())
    (root / "plain.txt").write_bytes(b"not an archive")
    (root / "empty").mkdir()
    return root


@pytest.fixture(scope="module")
def lib_server(lib_root, tmp_path_factory):
    from rs_spawn import spawn_backend

    state = tmp_path_factory.mktemp("lib-edit-state")
    proc, base_url = spawn_backend(library_dir=lib_root, state_dir=state)
    yield base_url
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture(scope="module")
def lib_server_auth(lib_root, tmp_path_factory):
    """A backend WITHOUT BOOKQ_TEST_MODE — library writes must 401."""
    state = tmp_path_factory.mktemp("lib-edit-auth-state")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.pop("BOOKQ_TEST_MODE", None)
    env.update(
        {
            "LIBRARY_DIR": str(lib_root),
            "BOOKQ_STATE_DIR": str(state),
            "BOOKQ_BIND": f"127.0.0.1:{port}",
            "GOOGLE_CLIENT_ID": "ci-dummy",
            "GOOGLE_CLIENT_SECRET": "ci-dummy",
            "BASE_URL": base_url,
            "ALLOWED_EMAILS": "test@example.com",
            "SECRET_KEY": "ci-not-a-secret",
            "BOOKQ_WEB_DIR": str(REPO_ROOT / "web"),
            "BOOKQ_THUMB_CACHE_DIR": str(state / "thumbs"),
        }
    )
    proc = subprocess.Popen(
        [str(_viewer_rs_binary())],
        env=env,
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("auth test backend exited early")
        try:
            urllib.request.urlopen(base_url + "/api/v1/health", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.terminate()
        raise RuntimeError("auth test backend did not become healthy")
    yield base_url
    proc.terminate()
    proc.wait(timeout=5)


def _tree_names(base: str, path: str, timeout: float = 15.0) -> list[str]:
    """Poll /api/v1/tree shallow mode until it 200s; return child names.

    ``path`` only scopes the response when ``depth`` is given — without it
    the handler returns the whole-tree envelope and ignores ``path``.
    ``depth=1`` answers ``kind:"children"`` — the folder/archive nodes
    directly under ``path``.
    """
    deadline = time.time() + timeout
    while True:
        r = httpx.get(
            f"{base}/api/v1/tree",
            params={"path": path, "depth": 1},
            timeout=10,
        )
        if r.status_code == 200:
            return [n["name"] for n in r.json()["nodes"]]
        if r.status_code != 202 or time.time() > deadline:
            r.raise_for_status()
        time.sleep(0.5)


# ── POST /api/v1/library/files ───────────────────────────────────────────────


def test_upload_single_file(lib_server, lib_root):
    r = httpx.post(
        f"{lib_server}/api/v1/library/files",
        params={"dir": "series"},
        files={"file": ("one-shot.cbz", _zip_bytes(), "application/zip")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["errors"] == []
    assert body["files"][0]["name"] == "one-shot.cbz"
    assert body["files"][0]["path"] == "series/one-shot.cbz"
    assert body["files"][0]["size"] > 0
    assert (lib_root / "series" / "one-shot.cbz").exists()


def test_upload_multiple_files_mixed_result(lib_server, lib_root):
    r = httpx.post(
        f"{lib_server}/api/v1/library/files",
        params={"dir": "series"},
        files=[
            ("file", ("multi-a.cbz", _zip_bytes(), "application/zip")),
            ("file", ("multi-b.txt", b"x", "text/plain")),
            ("file", ("multi-c.pdf", b"%PDF-1.4 fake", "application/pdf")),
        ],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [f["name"] for f in body["files"]] == ["multi-a.cbz", "multi-c.pdf"]
    assert body["errors"] == [{"name": "multi-b.txt", "code": "unsupported_type"}]


def test_upload_collision_autorenames(lib_server, lib_root):
    for want in ("work (1).cbz", "work (2).cbz"):
        r = httpx.post(
            f"{lib_server}/api/v1/library/files",
            params={"dir": "series"},
            files={"file": ("work.cbz", _zip_bytes(), "application/zip")},
        )
        assert r.status_code == 200, r.text
        assert [f["name"] for f in r.json()["files"]] == [want]
        assert (lib_root / "series" / want).exists()


def test_upload_root_dir(lib_server, lib_root):
    r = httpx.post(
        f"{lib_server}/api/v1/library/files",
        params={"dir": ""},
        files={"file": ("root-file.rar", b"rar!", "application/vnd.rar")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["files"][0]["path"] == "root-file.rar"
    assert (lib_root / "root-file.rar").exists()


def test_upload_dir_validation(lib_server):
    for bad_dir, want in [
        ("missing-dir", 404),
        ("plain.txt", 400),       # a file, not a dir
        ("..", 400),
        ("a/../b", 400),
        ("/abs", 400),
        (UPLOAD_DIR_NAME, 400),   # staging area is off-limits
    ]:
        r = httpx.post(
            f"{lib_server}/api/v1/library/files",
            params={"dir": bad_dir},
            files={"file": ("x.cbz", _zip_bytes(), "application/zip")},
        )
        assert r.status_code == want, (bad_dir, r.status_code, r.text)


def test_upload_rejects_bad_filename(lib_server):
    r = httpx.post(
        f"{lib_server}/api/v1/library/files",
        params={"dir": "series"},
        files={"file": ("../escape.cbz", _zip_bytes(), "application/zip")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["files"] == []
    assert body["errors"] == [{"name": "../escape.cbz", "code": "invalid_name"}]


def test_upload_no_file_fields(lib_server):
    r = httpx.post(
        f"{lib_server}/api/v1/library/files",
        params={"dir": "series"},
        files={"other": ("x.cbz", _zip_bytes(), "application/zip")},
    )
    assert r.status_code == 400


def test_upload_non_multipart_rejected(lib_server):
    r = httpx.post(
        f"{lib_server}/api/v1/library/files",
        params={"dir": "series"},
        json={"name": "x.cbz"},
    )
    assert r.status_code in (400, 415)


# ── DELETE /api/v1/library/files ─────────────────────────────────────────────


def test_delete_file_and_dir(lib_server, lib_root):
    victim = lib_root / "series" / "delete-me.cbz"
    victim.write_bytes(_zip_bytes())
    r = httpx.delete(
        f"{lib_server}/api/v1/library/files", params={"path": "series/delete-me.cbz"}
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": "series/delete-me.cbz", "kind": "file"}
    assert not victim.exists()

    doomed = lib_root / "doomed"
    (doomed / "inner").mkdir(parents=True)
    (doomed / "inner" / "x.cbz").write_bytes(b"x")
    r = httpx.delete(
        f"{lib_server}/api/v1/library/files", params={"path": "doomed"}
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": "doomed", "kind": "dir"}
    assert not doomed.exists()


def test_delete_rejects_unsafe_paths(lib_server):
    for bad_path, want in [
        ("..", 400),
        ("a/../b", 400),
        ("/abs", 400),
        (UPLOAD_DIR_NAME, 400),
        ("missing-file.cbz", 404),
        ("missing/dir", 404),
    ]:
        r = httpx.delete(
            f"{lib_server}/api/v1/library/files", params={"path": bad_path}
        )
        assert r.status_code == want, (bad_path, r.status_code, r.text)
    r = httpx.delete(f"{lib_server}/api/v1/library/files")
    assert r.status_code == 422


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlink support")
def test_delete_symlink_escape_forbidden(lib_server, lib_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.cbz").write_bytes(b"s")
    link = lib_root / "evil-link"
    link.symlink_to(outside, target_is_directory=True)
    r = httpx.delete(
        f"{lib_server}/api/v1/library/files", params={"path": "evil-link"}
    )
    assert r.status_code == 403, r.text
    assert (outside / "secret.cbz").exists()


# ── chunked upload sessions ─────────────────────────────────────────────────


def _init(base: str, name: str, size: int, dir: str = "series") -> str:
    r = httpx.post(
        f"{base}/api/v1/library/uploads",
        json={"dir": dir, "name": name, "size": size},
    )
    assert r.status_code == 201, r.text
    return r.json()["upload_id"]


def _put(base: str, uid: str, data: bytes, offset: int):
    return httpx.put(
        f"{base}/api/v1/library/uploads/{uid}",
        content=data,
        headers={"Upload-Offset": str(offset)},
    )


def test_chunked_upload_roundtrip(lib_server, lib_root):
    payload = _zip_bytes() + b"tail"
    half = len(payload) // 2
    uid = _init(lib_server, "chunked.cbz", len(payload))

    r = _put(lib_server, uid, payload[:half], 0)
    assert r.status_code == 200, r.text
    assert r.json()["offset"] == half

    # state probe reports the partial offset
    g = httpx.get(f"{lib_server}/api/v1/library/uploads/{uid}")
    assert g.status_code == 200
    assert g.json()["offset"] == half

    # wrong offset → 409 carrying the authoritative offset
    r = _put(lib_server, uid, payload[half:], half + 7)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "offset_mismatch"
    assert r.json()["offset"] == half

    # resume from the returned offset and finalize
    r = _put(lib_server, uid, payload[half:], half)
    assert r.status_code == 200
    r = httpx.post(f"{lib_server}/api/v1/library/uploads/{uid}/finalize")
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "chunked.cbz"
    assert (lib_root / "series" / "chunked.cbz").read_bytes() == payload
    # session dir is gone
    assert not list((lib_root / UPLOAD_DIR_NAME).glob("*"))


def test_finalize_incomplete_conflict(lib_server):
    uid = _init(lib_server, "incomplete.cbz", 100)
    r = httpx.post(f"{lib_server}/api/v1/library/uploads/{uid}/finalize")
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "incomplete"
    assert r.json()["offset"] == 0


def test_chunked_init_validation(lib_server):
    for body, want in [
        ({"dir": "series", "name": "bad.txt", "size": 10}, 415),
        ({"dir": "series", "name": "../x.cbz", "size": 10}, 400),
        ({"dir": "series", "name": "x.cbz", "size": 0}, 400),
        ({"dir": "missing", "name": "x.cbz", "size": 10}, 404),
        ({"dir": "..", "name": "x.cbz", "size": 10}, 400),
    ]:
        r = httpx.post(f"{lib_server}/api/v1/library/uploads", json=body)
        assert r.status_code == want, (body, r.status_code, r.text)


def test_upload_session_cancel(lib_server):
    uid = _init(lib_server, "cancel-me.cbz", 100)
    r = httpx.delete(f"{lib_server}/api/v1/library/uploads/{uid}")
    assert r.status_code == 204
    r = httpx.get(f"{lib_server}/api/v1/library/uploads/{uid}")
    assert r.status_code == 404


def test_upload_session_unknown_id(lib_server):
    r = httpx.get(f"{lib_server}/api/v1/library/uploads/{'f' * 32}")
    assert r.status_code == 404
    r = _put(lib_server, "f" * 32, b"x", 0)
    assert r.status_code == 404


# ── cross-cutting: staging hidden, index refresh, readability, auth ─────────


def test_staging_dir_invisible_in_tree(lib_server, lib_root):
    # a live session exists? create one, then confirm the tree never lists it
    uid = _init(lib_server, "staged.cbz", 10)
    assert (lib_root / UPLOAD_DIR_NAME / uid).exists()
    names = _tree_names(lib_server, "")
    assert UPLOAD_DIR_NAME not in names
    assert not any(n.startswith(".") for n in names)
    httpx.delete(f"{lib_server}/api/v1/library/uploads/{uid}")


def test_uploaded_file_appears_in_tree_and_is_readable(lib_server):
    r = httpx.post(
        f"{lib_server}/api/v1/library/files",
        params={"dir": "series"},
        files={"file": ("tree-visible.cbz", _zip_bytes("page.png"), "application/zip")},
    )
    assert r.status_code == 200
    deadline = time.time() + 20
    while time.time() < deadline:
        names = _tree_names(lib_server, "series")
        if "tree-visible.cbz" in names:
            break
        time.sleep(0.5)
    else:
        pytest.fail("uploaded file never appeared in the tree")
    # and the archive is actually readable through the images API
    imgs = httpx.get(
        f"{lib_server}/api/v1/images", params={"folder": "series/tree-visible.cbz"}
    )
    assert imgs.status_code == 200
    assert "page.png" in imgs.json()["images"]


def test_library_requires_auth(lib_server_auth):
    base = lib_server_auth
    r = httpx.post(
        f"{base}/api/v1/library/files",
        params={"dir": "series"},
        files={"file": ("x.cbz", _zip_bytes(), "application/zip")},
    )
    assert r.status_code == 401
    r = httpx.delete(f"{base}/api/v1/library/files", params={"path": "x.cbz"})
    assert r.status_code == 401
    r = httpx.post(f"{base}/api/v1/library/uploads", json={"name": "x.cbz", "size": 1})
    assert r.status_code == 401
