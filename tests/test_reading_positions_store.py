"""Reading-position store tests (#375).

Three layers of coverage:

* ``positions-cases.json`` — language-neutral PositionBook conformance
  vectors, also consumed by viewer-rs's Rust tests so py/rs cannot drift.
* ``ReadingPositionStore`` — SQLite persistence: restart survival,
  per-user isolation, tombstones, and the user/folder bounds.
* HTTP multi-user — TestClient apps with ``require_auth`` overridden to
  different emails over one shared BOOKQ_STATE_DIR, proving user A can
  never read or wipe user B's positions (and that a fresh app instance —
  a simulated restart — keeps them).
"""

import json
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth import require_auth
from app.reading_positions import (
    POSITION_MAX_FOLDERS,
    POSITION_MAX_USERS,
    PositionBook,
    ReadingPositionStore,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CASES_PATH = REPO_ROOT / "api" / "vectors" / "positions-cases.json"


# ── conformance vectors ────────────────────────────────────────────────


def _subset_match(expected: dict, actual: dict) -> bool:
    """Every key in *expected* equals the same key in *actual*.

    ``updated_at`` is deliberately not part of the contract — it is an
    internal eviction timestamp, not wire data.
    """
    return all(actual.get(k) == v for k, v in expected.items())


def _run_case(case: dict) -> None:
    book = PositionBook()
    for i, op in enumerate(case["ops"]):
        kind = op["op"]
        now = op.get("now", 0.0)
        where = f"{case['name']} op[{i}] {kind}"
        if kind in ("put_v0", "put_v1"):
            rec = dict(op["rec"])
            result = (
                book.put_v0(rec, now) if kind == "put_v0" else book.put_v1(rec, now)
            )
            assert result == op["expect"], (
                f"{where}: expected {op['expect']!r}, got {result!r}"
            )
        elif kind == "delete_folder":
            book.delete_folder(op["folder"], now)
        elif kind == "delete_all":
            book.delete_all(now)
        elif kind == "list":
            recs = book.list_positions(op.get("folder"))
            expected = op["expect"]
            assert len(recs) == len(expected), (
                f"{where}: expected {len(expected)} recs, got {len(recs)}: {recs}"
            )
            for exp, act in zip(expected, recs):
                assert _subset_match(exp, act), (
                    f"{where}: expected subset {exp}, got {act}"
                )
        elif kind == "latest":
            latest = book.latest()
            expected = op["expect"]
            if expected is None:
                assert latest is None, f"{where}: expected null, got {latest}"
            else:
                assert latest is not None and _subset_match(expected, latest), (
                    f"{where}: expected subset {expected}, got {latest}"
                )
        elif kind == "check":
            recs = book.list_positions()
            if "len" in op:
                assert len(recs) == op["len"], (
                    f"{where}: expected len {op['len']}, got {len(recs)}"
                )
            if "first" in op:
                assert recs and _subset_match(op["first"], recs[0]), (
                    f"{where}: first {recs[0] if recs else None} "
                    f"does not match {op['first']}"
                )
            for folder in op.get("absent", []):
                assert all(r["folder"] != folder for r in recs), (
                    f"{where}: {folder!r} should have been evicted"
                )
        else:
            raise AssertionError(f"{where}: unknown op {kind!r}")


@pytest.mark.parametrize(
    "case",
    json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"],
    ids=lambda c: c["name"],
)
def test_position_book_conformance(case):
    _run_case(case)


# ── SQLite store ───────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    return ReadingPositionStore(tmp_path / "user-state" / "folder-visibility.sqlite3")


def _put(store, user, folder, page, ts):
    return store.put_v1(user, {"folder": folder, "page": page, "total": 10, "ts": ts})


def test_store_roundtrip_and_latest(store):
    assert store.latest("a@x.com") is None
    assert store.list_positions("a@x.com") == []
    assert _put(store, "a@x.com", "manga/vol1", 3, 100) == "stored"
    rec = store.latest("a@x.com")
    assert rec["folder"] == "manga/vol1"
    assert rec["page"] == 3
    assert rec["ts"] == 100
    assert rec["updated_at"] > 0
    assert store.list_positions("a@x.com", folder="manga/vol1")[0]["page"] == 3
    assert store.list_positions("a@x.com", folder="other") == []


def test_store_survives_new_instance(store, tmp_path):
    """A fresh store object on the same DB file = container restart."""
    _put(store, "a@x.com", "vol1", 4, 100)
    _put(store, "a@x.com", "vol2", 9, 200)
    store.delete_folder("a@x.com", "vol1")

    restarted = ReadingPositionStore(store._path)
    recs = restarted.list_positions("a@x.com")
    assert [r["folder"] for r in recs] == ["vol2"]
    # The folder tombstone also persisted — an old in-flight write can't
    # resurrect vol1.
    assert _put(restarted, "a@x.com", "vol1", 1, 100) == "stale"
    assert _put(restarted, "a@x.com", "vol1", 8, 300) == "stored"


def test_store_delete_all_tombstone_persists(store, tmp_path):
    _put(store, "a@x.com", "vol1", 4, 100)
    _put(store, "a@x.com", "vol2", 9, 200)
    assert store.delete_all("a@x.com") == "stored"
    assert store.list_positions("a@x.com") == []
    assert store.latest("a@x.com") is None

    restarted = ReadingPositionStore(store._path)
    # tombstone_ts = 200 (newest wiped) survives the restart.
    assert _put(restarted, "a@x.com", "vol1", 1, 200) == "stale"
    assert _put(restarted, "a@x.com", "vol1", 1, 201) == "stored"


def test_store_users_are_isolated(store):
    _put(store, "alice@x.com", "shared", 1, 100)
    _put(store, "bob@x.com", "shared", 7, 200)
    assert store.latest("alice@x.com")["page"] == 1
    assert store.latest("bob@x.com")["page"] == 7
    # Deletes are per-user too.
    store.delete_all("alice@x.com")
    assert store.latest("alice@x.com") is None
    assert store.latest("bob@x.com")["page"] == 7


def test_store_delete_user_leaves_no_tombstone(store):
    _put(store, "a@x.com", "vol1", 4, 100)
    store.delete_user("a@x.com")
    assert store.latest("a@x.com") is None
    # ?hard=1 semantics: the next fresh write is NOT rejected by a tombstone.
    assert _put(store, "a@x.com", "vol1", 1, 100) == "stored"


def test_store_max_users_eviction(store):
    for i in range(POSITION_MAX_USERS + 1):
        user = f"user{i:03d}@x.com"
        assert _put(store, user, "vol", i, 1000 + i) == "stored"
        time.sleep(0.001)  # distinct updated_at ordering
    # The stalest (first-written) user was evicted; count stays bounded.
    assert store.latest("user000@x.com") is None
    assert store.latest(f"user{POSITION_MAX_USERS:03d}@x.com") is not None


def test_store_max_folders_trim(store):
    for i in range(POSITION_MAX_FOLDERS + 1):
        assert _put(store, "u@x.com", f"f{i:04d}", i, 1000 + i) == "stored"
    recs = store.list_positions("u@x.com")
    assert len(recs) == POSITION_MAX_FOLDERS
    folders = {r["folder"] for r in recs}
    assert "f0000" not in folders
    assert f"f{POSITION_MAX_FOLDERS:04d}" in folders


def test_store_put_v0_semantics(store):
    rec = {"folder": "a", "page": 1, "total": 10, "ts": 100}
    assert store.put_v0("u@x.com", dict(rec)) == "stored"
    # v0 is global-newest: an older ts for a DIFFERENT folder is stale.
    assert store.put_v0("u@x.com", {"folder": "b", "page": 1, "total": 10, "ts": 50}) == "stale"
    # ...while v1 accepts it (per-folder LWW).
    assert store.put_v1("u@x.com", {"folder": "b", "page": 1, "total": 10, "ts": 50}) == "stored"
    latest = store.latest("u@x.com")
    assert latest["folder"] == "a" and latest["ts"] == 100


def test_store_future_ts_rejected(store):
    future = int(time.time() * 1000) + 10 * 60 * 1000  # 10min ahead > 5min slack
    assert store.put_v1("u@x.com", {"folder": "a", "page": 1, "total": 10, "ts": future}) == "rejected"
    assert store.latest("u@x.com") is None


# ── HTTP multi-user isolation ──────────────────────────────────────────

SECRET = "test-secret-key-for-reading-positions"


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    """TestClient apps over one shared state dir; each call = a different
    authenticated email — or a 'restarted' app when fresh=True."""
    state_dir = tmp_path / "state"
    monkeypatch.setenv("BOOKQ_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SECRET_KEY", SECRET)
    monkeypatch.setenv("DOWNLOADS_DIR", str(tmp_path / "downloads"))
    (tmp_path / "downloads").mkdir(exist_ok=True)

    def _make(email: str = "alice@example.com") -> TestClient:
        from app.main import create_app

        app = create_app()
        app.dependency_overrides[require_auth] = lambda: {"email": email}
        return TestClient(app)

    return _make


def _v1_put(client, folder, page, ts, total=10):
    return client.put(
        "/api/v1/reading-positions",
        params={"folder": folder},
        json={"page": page, "total": total, "ts": ts},
    )


def _v1_list(client, folder=None):
    params = {"folder": folder} if folder else {}
    return client.get("/api/v1/reading-positions", params=params)


def test_http_users_are_isolated(make_client):
    alice = make_client("alice@example.com")
    bob = make_client("bob@example.com")

    assert _v1_put(alice, "vol1", 3, 100).json() == {"status": "ok"}
    assert _v1_put(bob, "vol1", 8, 200).json() == {"status": "ok"}

    alice_recs = _v1_list(alice).json()["positions"]
    bob_recs = _v1_list(bob).json()["positions"]
    assert [r["page"] for r in alice_recs] == [3]
    assert [r["page"] for r in bob_recs] == [8]

    # Bob's delete-all cannot touch Alice's records.
    assert alice.delete("/api/v1/reading-positions").json() == {"status": "ok"}
    assert _v1_list(alice).json()["positions"] == []
    assert [r["page"] for r in _v1_list(bob).json()["positions"]] == [8]


def test_http_email_normalization(make_client):
    """Case/whitespace variants of one email share the same slot."""
    alice = make_client("Alice@Example.com")
    alice2 = make_client("  alice@example.COM  ")
    _v1_put(alice, "vol1", 3, 100)
    assert [r["page"] for r in _v1_list(alice2).json()["positions"]] == [3]


def test_http_positions_survive_app_restart(make_client):
    """A new app instance on the same state dir keeps positions — the
    container-restart case the in-memory store used to lose."""
    alice = make_client("alice@example.com")
    _v1_put(alice, "vol1", 3, 100)
    _v1_put(alice, "vol2", 7, 200)

    restarted = make_client("alice@example.com")
    recs = _v1_list(restarted).json()["positions"]
    assert [(r["folder"], r["page"]) for r in recs] == [("vol2", 7), ("vol1", 3)]

    # v0 view agrees across the restart too.
    pos = restarted.get("/api/reading-position").json()["position"]
    assert pos["folder"] == "vol2" and pos["page"] == 7


def test_http_v0_and_v1_share_the_store(make_client):
    """v0 PUT then v1 GET (and vice versa) see the same per-user data —
    both endpoints adapt the one SQLite slot."""
    client = make_client("alice@example.com")
    assert client.put(
        "/api/reading-position",
        json={"folder": "vol1", "page": 4, "total": 10, "ts": 100},
    ).json() == {"status": "ok"}
    v1 = _v1_list(client).json()["positions"]
    assert [(r["folder"], r["page"]) for r in v1] == [("vol1", 4)]

    assert _v1_put(client, "vol2", 2, 50).json() == {"status": "ok"}
    # v0 newest = vol1 (ts 100 > 50).
    pos = client.get("/api/reading-position").json()["position"]
    assert pos["folder"] == "vol1"


def test_http_session_without_email_gets_403(make_client):
    from app.main import create_app

    app = create_app()
    app.dependency_overrides[require_auth] = lambda: {}
    client = TestClient(app)
    assert client.get("/api/v1/reading-positions").status_code == 403
    assert client.get("/api/reading-position").status_code == 403
    assert _v1_put(client, "vol1", 1, 1).status_code == 403


def test_http_hard_delete_clears_slot(make_client):
    """``?hard=1`` (test mode) removes the slot with NO tombstone — the
    conftest reset relies on a truly clean slate."""
    client = make_client("alice@example.com")
    _v1_put(client, "vol1", 3, 100)
    assert client.delete("/api/reading-position?hard=1").json() == {"status": "ok"}
    assert _v1_list(client).json()["positions"] == []
    # No tombstone: the same ts writes cleanly again.
    assert _v1_put(client, "vol1", 3, 100).json() == {"status": "ok"}


def test_http_plain_delete_leaves_tombstone(make_client):
    client = make_client("alice@example.com")
    _v1_put(client, "vol1", 3, 100)
    assert client.delete("/api/v1/reading-positions").json() == {"status": "ok"}
    assert _v1_list(client).json()["positions"] == []
    # A stale in-flight write at the wiped ts cannot resurrect the record.
    assert _v1_put(client, "vol1", 3, 100).json() == {"status": "stale"}
    assert _v1_put(client, "vol1", 3, 101).json() == {"status": "ok"}
