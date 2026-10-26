"""Per-user persistent folder visibility (hidden top-level roots).

Stdlib ``sqlite3`` only — one row per (user, hidden root); unhide deletes the
row. Each user has a monotonically increasing ``revision`` that bumps only
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
MAX_ROOT_LENGTH = 2048
_BUSY_TIMEOUT_MS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS visibility_users (
  user_key TEXT PRIMARY KEY,
  revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS hidden_roots (
  user_key TEXT NOT NULL,
  root TEXT NOT NULL,
  PRIMARY KEY (user_key, root)
);
"""


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


def validate_root(root: object) -> str:
    """Return *root* unchanged, or raise ``ValueError`` for an invalid value.

    A root is a single non-empty top-level path segment: no separators, NUL,
    or dot names (the scanner skips ``.*`` entries entirely), and bounded in
    length. Leading/trailing whitespace and case are preserved verbatim.
    """
    if not isinstance(root, str) or not root:
        raise ValueError("root must be a non-empty string")
    if len(root) > MAX_ROOT_LENGTH:
        raise ValueError("root too long")
    if "/" in root or "\\" in root or "\x00" in root:
        raise ValueError("root must be a single path segment")
    if root.startswith("."):
        raise ValueError("root must not be a dot name")
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
                        conn.executescript(_SCHEMA)
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
