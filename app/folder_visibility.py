"""Persistent folder visibility (hidden top-level roots), library-global.

Stdlib ``sqlite3`` only — one row per (scope, hidden root); unhide deletes
the row. The hidden set is shared by every authenticated user: all rows
live under the single ``GLOBAL_USER_KEY`` sentinel (``*`` — never a valid
email-derived user key). A monotonically increasing ``revision`` bumps only
when the hidden set actually changes (same-value writes are idempotent).
"""

import hashlib
import hmac
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

DB_FILENAME = "folder-visibility.sqlite3"
OWNER_PURPOSE = "folder-visibility:v1:"
# The hidden set is library-global: every row lives under this sentinel
# ``user_key``. ``*`` can never collide with an email-derived user key.
# viewer-rs keeps the same constant (rust/viewer-rs/src/state.rs).
GLOBAL_USER_KEY = "*"
MAX_ROOT_LENGTH = 2048
_BUSY_TIMEOUT_MS = 5000

# The DDL lives in one shared file (#200) — api/user-state-schema.sql is
# the single source FastAPI and viewer-rs both apply. This constant must
# equal the file's `PRAGMA user_version` stamp; a DB stamped newer than
# it is refused rather than silently downgraded. v2 added the reading-
# position tables (#375) — the constant versions the whole user-state DB,
# not just the visibility tables.
SCHEMA_VERSION = 2
_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "api" / "user-state-schema.sql"
)


def _apply_schema(conn: sqlite3.Connection) -> None:
    """Idempotently apply the shared user-state DDL and verify the schema
    version — refuses a DB stamped newer than this build understands."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"user-state schema v{current} is newer than supported "
            f"v{SCHEMA_VERSION} — refusing to downgrade"
        )
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    stamped = conn.execute("PRAGMA user_version").fetchone()[0]
    if stamped != SCHEMA_VERSION:
        raise RuntimeError(
            f"user-state schema file stamped v{stamped}; this build "
            f"expects v{SCHEMA_VERSION} — bump SCHEMA_VERSION with the DDL"
        )


@dataclass(frozen=True)
class VisibilitySnapshot:
    revision: int
    hidden_roots: tuple[str, ...]


def user_key_for_email(email: object) -> str | None:
    """Internal user key: ``strip().lower()`` of the authenticated email.

    ``None`` when the session carries no usable email — a malformed session
    must never fall back to a shared "unknown" account.
    """
    if not isinstance(email, str):
        return None
    key = email.strip().lower()
    return key or None


def visibility_owner(user_key: str, secret_key: str) -> str:
    """Opaque per-user token: purpose-scoped HMAC-SHA256 of the user key.

    Anti account-confusion only — the API never selects a DB user from it,
    and the DB user key does not depend on SECRET_KEY (rotation keeps rows).
    """
    return hmac.new(
        secret_key.encode("utf-8"),
        (OWNER_PURPOSE + user_key).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def covered_by_roots(path: object, roots) -> bool:
    """``path == K`` or ``path`` under ``K + "/"`` for any non-empty string K
    in *roots* — the same covering rule the client's hiddenMatch uses, so a
    flattened key like ``wrap/work`` covers its whole subtree. Used by the
    metadata endpoints to keep hidden works out of facets/search server-side.
    """
    if not isinstance(path, str) or not path:
        return False
    for k in roots or ():
        if isinstance(k, str) and k and (path == k or path.startswith(k + "/")):
            return True
    return False


def validate_root(root: object) -> str:
    """Return *root* unchanged, or raise ``ValueError`` for an invalid value.

    A root is the normalized path of a top-level tree node — one or more
    segments, and a flattened scanner node's name legitimately contains
    ``/`` (``wrap/work``). No NUL or backslashes, no empty / ``.`` / ``..``
    segments (so no leading/trailing/doubled ``/``), no dot-leading segment
    (the scanner skips ``.*`` entries), and bounded in length. The value
    must already be normalized; whitespace and case are preserved verbatim.
    """
    if not isinstance(root, str) or not root:
        raise ValueError("root must be a non-empty string")
    if len(root) > MAX_ROOT_LENGTH:
        raise ValueError("root too long")
    if "\\" in root or "\x00" in root:
        raise ValueError("root must not contain backslash or NUL")
    for seg in root.split("/"):
        if not seg or seg in (".", ".."):
            raise ValueError("root must be a normalized path")
        if seg.startswith("."):
            raise ValueError("root must not contain a dot name")
    return root


class FolderVisibilityStore:
    """SQLite-backed hidden-root store. The constructor performs no I/O."""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._init_lock = threading.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        """Open a fresh connection; initialize schema under a lock on first use."""
        if not self._initialized:
            with self._init_lock:
                if not self._initialized:
                    self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    conn = sqlite3.connect(
                        str(self._path),
                        timeout=_BUSY_TIMEOUT_MS / 1000,
                        isolation_level=None,
                    )
                    try:
                        conn.execute("PRAGMA journal_mode=WAL")
                        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
                        _apply_schema(conn)
                    finally:
                        conn.close()
                    try:
                        os.chmod(self._path, 0o600)
                    except OSError:
                        pass
                    self._initialized = True
        conn = sqlite3.connect(
            str(self._path),
            timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        return conn

    def read(self, user_key: str) -> VisibilitySnapshot:
        """Return ``(revision, hidden_roots)`` read inside one transaction."""
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            try:
                row = conn.execute(
                    "SELECT revision FROM visibility_users WHERE user_key = ?",
                    (user_key,),
                ).fetchone()
                roots = conn.execute(
                    "SELECT root FROM hidden_roots WHERE user_key = ? ORDER BY root",
                    (user_key,),
                ).fetchall()
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            return VisibilitySnapshot(
                revision=row[0] if row else 0,
                hidden_roots=tuple(r[0] for r in roots),
            )
        finally:
            conn.close()

    def set_hidden(self, user_key: str, root: str, hidden: bool) -> VisibilitySnapshot:
        """Add/remove one hidden row; bump revision only on an actual change."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = (
                    conn.execute(
                        "SELECT 1 FROM hidden_roots WHERE user_key = ? AND root = ?",
                        (user_key, root),
                    ).fetchone()
                    is not None
                )
                if existing != hidden:
                    if hidden:
                        conn.execute(
                            "INSERT INTO hidden_roots (user_key, root) VALUES (?, ?)",
                            (user_key, root),
                        )
                    else:
                        conn.execute(
                            "DELETE FROM hidden_roots WHERE user_key = ? AND root = ?",
                            (user_key, root),
                        )
                    conn.execute(
                        "INSERT INTO visibility_users (user_key, revision)"
                        " VALUES (?, 0) ON CONFLICT(user_key) DO NOTHING",
                        (user_key,),
                    )
                    conn.execute(
                        "UPDATE visibility_users SET revision = revision + 1"
                        " WHERE user_key = ?",
                        (user_key,),
                    )
                row = conn.execute(
                    "SELECT revision FROM visibility_users WHERE user_key = ?",
                    (user_key,),
                ).fetchone()
                roots = conn.execute(
                    "SELECT root FROM hidden_roots WHERE user_key = ? ORDER BY root",
                    (user_key,),
                ).fetchall()
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            return VisibilitySnapshot(
                revision=row[0] if row else 0,
                hidden_roots=tuple(r[0] for r in roots),
            )
        finally:
            conn.close()
