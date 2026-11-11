"""FolderVisibilityStore unit tests — sqlite persistence, revisions, idempotency.

Pure store-level tests (no HTTP): constructor performs no I/O, schema is
created lazily on first use, read/write return consistent snapshots, revisions
only bump on real state changes. Production rows all live under
``GLOBAL_USER_KEY`` — the store itself stays a generic keyed KV.

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt \\
        pytest tests/test_folder_visibility_store.py -q
"""

import sqlite3
import threading
from pathlib import Path

import pytest

from app.folder_visibility import (
    GLOBAL_USER_KEY,
    FolderVisibilityStore,
    user_key_for_email,
    validate_root,
    visibility_owner,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "folder-visibility.sqlite3"


def test_constructor_performs_no_io(db_path: Path):
    FolderVisibilityStore(db_path)
    assert not db_path.exists()


def test_read_empty_snapshot(db_path: Path):
    snap = FolderVisibilityStore(db_path).read("alice@example.com")
    assert snap.revision == 0
    assert snap.hidden_roots == ()
    assert db_path.exists()


def test_set_hidden_persists_across_instances(db_path: Path):
    # GLOBAL_USER_KEY is the production key — rows persist across store
    # instances over the same DB file.
    snap = FolderVisibilityStore(db_path).set_hidden(GLOBAL_USER_KEY, "作品A", True)
    assert snap.revision == 1
    assert snap.hidden_roots == ("作品A",)
    # A brand-new store object over the same DB file sees the same state.
    snap2 = FolderVisibilityStore(db_path).read(GLOBAL_USER_KEY)
    assert snap2.revision == 1
    assert snap2.hidden_roots == ("作品A",)


def test_unhide_removes_row_and_bumps_revision(db_path: Path):
    store = FolderVisibilityStore(db_path)
    store.set_hidden("alice@example.com", "作品A", True)
    snap = store.set_hidden("alice@example.com", "作品A", False)
    assert snap.revision == 2
    assert snap.hidden_roots == ()
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT COUNT(*) FROM hidden_roots").fetchone()
    finally:
        conn.close()
    assert rows[0] == 0


def test_same_value_writes_are_idempotent(db_path: Path):
    store = FolderVisibilityStore(db_path)
    store.set_hidden("alice@example.com", "作品A", True)
    snap = store.set_hidden("alice@example.com", "作品A", True)
    assert snap.revision == 1
    # Unhide of a never-hidden root is also a no-op.
    snap = store.set_hidden("alice@example.com", "missing", False)
    assert snap.revision == 1
    assert snap.hidden_roots == ("作品A",)


def test_generic_keys_isolated_within_one_instance(db_path: Path):
    """The store stays a generic keyed KV: arbitrary keys coexist
    (production never writes them — routes pin GLOBAL_USER_KEY)."""
    store = FolderVisibilityStore(db_path)
    store.set_hidden("alice@example.com", "作品A", True)
    assert store.read("bob@example.com").hidden_roots == ()
    assert store.read("bob@example.com").revision == 0
    assert store.read("alice@example.com").hidden_roots == ("作品A",)


def test_multiple_roots_and_revision_monotonic(db_path: Path):
    store = FolderVisibilityStore(db_path)
    s1 = store.set_hidden("alice@example.com", "b-root", True)
    s2 = store.set_hidden("alice@example.com", "a-root", True)
    assert s1.revision < s2.revision == 2
    assert s2.hidden_roots == ("a-root", "b-root")
    s3 = store.set_hidden("alice@example.com", "a-root", False)
    assert s3.revision == 3
    assert s3.hidden_roots == ("b-root",)


def test_unhide_of_gone_root_still_possible(db_path: Path):
    # Removing the underlying library folder must not affect the stored row;
    # unhide on a missing row stays a no-op.
    store = FolderVisibilityStore(db_path)
    store.set_hidden("alice@example.com", "作品A", True)
    snap = store.set_hidden("alice@example.com", "作品A", False)
    assert snap.hidden_roots == ()


def test_concurrent_writers_keep_all_roots(db_path: Path):
    store = FolderVisibilityStore(db_path)
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            for j in range(5):
                store.set_hidden("alice@example.com", f"root-{i}-{j}", True)
        except BaseException as exc:  # noqa: BLE001 — collected and asserted
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    snap = store.read("alice@example.com")
    assert len(snap.hidden_roots) == 20
    assert snap.revision == 20


def test_store_surfaces_db_errors(tmp_path: Path):
    # state dir is an existing regular file -> mkdir/connect must fail, not
    # silently fall back to memory.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    store = FolderVisibilityStore(blocker / "folder-visibility.sqlite3")
    with pytest.raises(Exception):
        store.read("alice@example.com")


# ── input helpers ─────────────────────────────────────────────────────


def test_user_key_normalizes_email():
    assert user_key_for_email("  Alice@Example.COM ") == "alice@example.com"
    assert user_key_for_email("") is None
    assert user_key_for_email("   ") is None
    assert user_key_for_email(None) is None
    assert user_key_for_email(123) is None


def test_visibility_owner_is_opaque_deterministic_and_scoped():
    o1 = visibility_owner("alice@example.com", "secret-1")
    assert o1 == visibility_owner("alice@example.com", "secret-1")
    assert o1 != visibility_owner("alice@example.com", "secret-2")
    assert o1 != visibility_owner("bob@example.com", "secret-1")
    assert "alice" not in o1
    assert "@" not in o1


def test_validate_root_accepts_top_segment_names():
    assert validate_root("作品A") == "作品A"
    assert validate_root("単行本.zip") == "単行本.zip"
    assert validate_root("A B") == "A B"
    assert validate_root("  spaced  ") == "  spaced  "


def test_validate_root_accepts_flattened_multi_segment_names():
    """Flattened scanner roots are the top-level node's own name — a '/'
    inside it is structural, not a separator to reject."""
    assert validate_root("wrap/work") == "wrap/work"
    assert validate_root("wrap/inner/vol1") == "wrap/inner/vol1"
    assert validate_root("wrap/arc.zip") == "wrap/arc.zip"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "a\\b",
        "/abs",
        "/a",
        "a/",
        "a//b",
        "a/./b",
        "a/../b",
        "a\x00b",
        ".",
        "..",
        ".hidden",
        "a/.b",
        ".h/x",
        "x" * 2049,
        None,
        123,
        True,
        ["a"],
    ],
)
def test_validate_root_rejects(bad):
    with pytest.raises(ValueError):
        validate_root(bad)


def test_validate_root_accepts_max_length():
    assert validate_root("x" * 2048) == "x" * 2048


# ── shared schema file / version pinning (#200) ──────────────────────

_SCHEMA_SQL = (
    Path(__file__).resolve().parent.parent / "api" / "user-state-schema.sql"
)


def test_schema_version_matches_file_stamp():
    import re

    from app.folder_visibility import SCHEMA_VERSION

    sql = _SCHEMA_SQL.read_text(encoding="utf-8")
    stamp = re.search(r"PRAGMA\s+user_version\s*=\s*(\d+)", sql)
    assert stamp is not None, "schema file must stamp PRAGMA user_version"
    assert int(stamp.group(1)) == SCHEMA_VERSION


def test_store_init_stamps_schema_version(db_path: Path):
    from app.folder_visibility import SCHEMA_VERSION

    FolderVisibilityStore(db_path).read("alice@example.com")
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        conn.close()


def test_store_refuses_newer_schema(db_path: Path):
    from app.folder_visibility import SCHEMA_VERSION

    FolderVisibilityStore(db_path).read("alice@example.com")
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    finally:
        conn.close()
    with pytest.raises(RuntimeError, match="newer than supported"):
        FolderVisibilityStore(db_path).read("alice@example.com")
