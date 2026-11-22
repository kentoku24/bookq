"""Library edit API tests — /api/v1/library/* (upload, delete, chunked).

These tests MUTATE the library tree, so they spawn a dedicated viewer-rs
against a private tmp root (same pattern as the folder-visibility write
tests) instead of the shared session `server` fixture.
"""

import concurrent.futures
import io
import json
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

from rs_spawn import viewer_rs_binary  # noqa: E402

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
    # Archive whose only member is 0 bytes — the archive Range path.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("empty.png", b"")
    (root / "series" / "zero-member.cbz").write_bytes(buf.getvalue())
    (root / "plain.txt").write_bytes(b"not an archive")
    (root / "zero.jpg").write_bytes(b"")  # 0-byte file for the Range path
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
            "SECRET_KEY": "ci-not-a-secret",
            "BOOKQ_WEB_DIR": str(REPO_ROOT / "web"),
            "BOOKQ_THUMB_CACHE_DIR": str(state / "thumbs"),
        }
    )
    # #139: watchdog spawn — pytest death can't orphan the backend.
    import _server_proc

    proc = _server_proc.spawn(
        [str(viewer_rs_binary())],
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
        (".BOOKQ-UPLOADS", 400),  # case-variant can't slip past the guard
        (".hidden", 400),         # dot components are never listable
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
    # Non-multipart Content-Type is an extractor rejection → 400 text/plain.
    # (415 is unreachable — axum's MultipartRejection has no 415 variant.)
    assert r.status_code == 400


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


def test_staging_unaddressable_on_every_route(lib_server, lib_root):
    # Dot components are rejected on every request path — listing filters
    # already hide `.bookq-uploads`, these keep it unreachable directly too
    # (session ids + in-flight blobs must not leak; Shannon scan repros).
    uid = _init(lib_server, "hidden.cbz", 10)
    assert (lib_root / UPLOAD_DIR_NAME / uid / "meta.json").exists()

    # Read paths → 404 (invisible, not 403/400 — the dir shouldn't exist).
    for url in (
        f"{lib_server}/image/{UPLOAD_DIR_NAME}/{uid}/meta.json",
        f"{lib_server}/image/{UPLOAD_DIR_NAME}/{uid}/blob",
        f"{lib_server}/thumb/{UPLOAD_DIR_NAME}/{uid}/meta.json",
        f"{lib_server}/view/{UPLOAD_DIR_NAME}/{uid}/meta.json",
    ):
        r = httpx.get(url)
        assert r.status_code == 404, (url, r.status_code)
    r = httpx.get(
        f"{lib_server}/api/v1/tree",
        params={"path": UPLOAD_DIR_NAME, "depth": 1},
    )
    assert r.status_code == 404, r.status_code
    r = httpx.get(
        f"{lib_server}/api/v1/images",
        params={"folder": UPLOAD_DIR_NAME},
    )
    assert r.status_code == 404, r.status_code

    # Write paths → 400 — incl. case variants that slip the raw string
    # compare on case-insensitive filesystems (APFS). The dot-component
    # check runs before any realpath, so the table is FS-portable.
    for bad in (UPLOAD_DIR_NAME, ".BOOKQ-UPLOADS", f"{UPLOAD_DIR_NAME}/{uid}"):
        r = httpx.delete(
            f"{lib_server}/api/v1/library/files", params={"path": bad}
        )
        assert r.status_code == 400, (bad, r.status_code)

    httpx.delete(f"{lib_server}/api/v1/library/uploads/{uid}")


def test_range_on_empty_targets(lib_server):
    # `size - 1` underflowed to u64::MAX on empty targets — bogus 206 on
    # plain files, a 500 slice panic on archive members (Shannon repro).
    r = httpx.get(f"{lib_server}/image/zero.jpg", headers={"Range": "bytes=-5"})
    assert r.status_code == 416, (r.status_code, r.headers.get("content-range"))
    assert r.headers.get("content-range") == "bytes */0"

    # An explicit range on the same file was already 416 — suffix was the hole.
    r = httpx.get(f"{lib_server}/image/zero.jpg", headers={"Range": "bytes=0-5"})
    assert r.status_code == 416

    # Archive path: unusable range on a 0-byte member → 200 with the empty
    # body (that parser's contract), never a 500.
    r = httpx.get(
        f"{lib_server}/image/series/zero-member.cbz/empty.png",
        headers={"Range": "bytes=-5"},
    )
    assert r.status_code == 200, r.status_code
    assert r.content == b""


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


# ── review hardening: body limit, races, reconcile ──────────────────────────


def test_multipart_body_over_default_limit(lib_server, lib_root):
    # DefaultBodyLimit::disable() is scoped to the library routes — a >2MiB
    # multipart body must not 413 (axum's Multipart extractor applies the
    # 2MiB default even for manual extraction).
    payload = _zip_bytes() + b"\0" * (3 * 1024 * 1024)
    r = httpx.post(
        f"{lib_server}/api/v1/library/files",
        params={"dir": "series"},
        files={"file": ("big.cbz", payload, "application/zip")},
        timeout=30,
    )
    assert r.status_code == 200, r.text
    assert (lib_root / "series" / "big.cbz").stat().st_size == len(payload)


def test_concurrent_same_name_uploads_unique(lib_server, lib_root):
    # Racing same-name uploads each get a distinct slot — dedupe+rename runs
    # under the placement lock so no rename clobbers another upload.
    def post(_):
        return httpx.post(
            f"{lib_server}/api/v1/library/files",
            params={"dir": "series"},
            files={"file": ("race.cbz", _zip_bytes(), "application/zip")},
            timeout=30,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        rs = list(ex.map(post, range(8)))
    assert all(r.status_code == 200 for r in rs), [r.status_code for r in rs]
    names = [r.json()["files"][0]["name"] for r in rs]
    assert len(set(names)) == len(names), names
    for n in names:
        assert (lib_root / "series" / n).exists()


def test_concurrent_chunk_puts_serialize(lib_server, lib_root):
    # Two PUTs at the same offset serialize on the session lock — the loser
    # sees the advanced offset and gets a 409, never a double-append.
    payload = _zip_bytes()
    half = len(payload) // 2
    uid = _init(lib_server, "race-chunk.cbz", len(payload))
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        rs = list(ex.map(lambda _: _put(lib_server, uid, payload[:half], 0), range(2)))
    assert sorted(r.status_code for r in rs) == [200, 409], [
        r.status_code for r in rs
    ]
    # the blob holds the prefix exactly once — resume + finalize proves it
    r = _put(lib_server, uid, payload[half:], half)
    assert r.status_code == 200
    r = httpx.post(f"{lib_server}/api/v1/library/uploads/{uid}/finalize")
    assert r.status_code == 200, r.text
    assert (lib_root / "series" / "race-chunk.cbz").read_bytes() == payload


def test_session_load_reconciles_unconfirmed_tail(lib_server, lib_root):
    # The append landed but the offset persist died: meta says N, blob is
    # longer. The next load truncates the unconfirmed tail so a resent chunk
    # appends at the true position instead of double-writing.
    uid = _init(lib_server, "reconcile.cbz", 10)
    sdir = lib_root / UPLOAD_DIR_NAME / uid
    (sdir / "blob").write_bytes(b"0123456789")
    meta = sdir / "meta.json"
    m = json.loads(meta.read_text())
    m["offset"] = 6
    meta.write_text(json.dumps(m))

    g = httpx.get(f"{lib_server}/api/v1/library/uploads/{uid}")
    assert g.status_code == 200
    assert g.json()["offset"] == 6
    assert (sdir / "blob").stat().st_size == 6

    r = _put(lib_server, uid, b"abcd", 6)
    assert r.status_code == 200
    r = httpx.post(f"{lib_server}/api/v1/library/uploads/{uid}/finalize")
    assert r.status_code == 200, r.text
    assert (lib_root / "series" / "reconcile.cbz").read_bytes() == b"012345abcd"

def test_upload_contract_rejections(lib_server, lib_root):
    # Schema-invalid bodies must 4xx, not 200 — schemathesis' negative_data
    # check exercises exactly these shapes against the live contract.
    r = httpx.post(
        f"{lib_server}/api/v1/library/uploads",
        json={"name": "x.cbz", "size": 10, "extra": 1},
    )
    assert r.status_code == 400

    # A `file` part with no filename is a form field, not an upload.
    r = httpx.post(
        f"{lib_server}/api/v1/library/files",
        files={"file": (None, b"{}")},
    )
    assert r.status_code == 400

    # Unparseable multipart is a bad request, not a partial-success report.
    # Pin the JSON detail so we know it's the handler's saw_malformed 400,
    # not the extractor's text/plain 400 (missing boundary etc.).
    r = httpx.post(
        f"{lib_server}/api/v1/library/files",
        content=b"--b\r\nContent-Disposition: form-data; name=\"\x0f\xc3\xa4\"\r\n\r\nx\r\n--b--\r\n",
        headers={"Content-Type": "multipart/form-data; boundary=b"},
    )
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["detail"] == "malformed multipart body"

    # A filename present in the disposition but undecodable as UTF-8 is an
    # upload attempt with a bad name — reported per-file at 200, not
    # silently skipped like a form field.
    r = httpx.post(
        f"{lib_server}/api/v1/library/files",
        content=b"--b\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\xff.cbz\"\r\nContent-Type: application/octet-stream\r\n\r\nx\r\n--b--\r\n",
        headers={"Content-Type": "multipart/form-data; boundary=b"},
    )
    assert r.status_code == 200
    assert r.json()["errors"] == [{"name": "?", "code": "invalid_name"}]

    # ...but a schema-valid batch whose entries all fail per-file checks
    # still reports them under `errors` at 200.
    r = httpx.post(
        f"{lib_server}/api/v1/library/files",
        files={"file": ("b.txt", b"hello")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["files"] == [] and body["errors"][0]["code"] == "unsupported_type"
