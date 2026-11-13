"""Reading positions — per-user, per-folder last-writer-wins store (#375).

Two layers:

* ``PositionBook`` — the pure slot rules, extracted verbatim from the old
  ``app.routes`` private helpers so v0/v1 handlers are thin adapters and
  the same semantics can be ported to viewer-rs (conformance vectors in
  ``api/vectors/positions-cases.json`` pin the behaviour in both
  languages). Slot shape is unchanged:
  ``{"folders": {folder: rec|tombstone}, "tombstone_ts": int,
  "updated_at": float}``.

* ``ReadingPositionStore`` — SQLite persistence on the shared user-state
  DB (``api/user-state-schema.sql`` v2, same file as folder visibility).
  Every operation is one ``BEGIN IMMEDIATE`` transaction: load slot →
  apply book op → rewrite the user's rows. Folder rows are re-inserted in
  dict order so ``rowid`` reproduces the old dict's insertion order and
  ts ties sort identically.

User key: ``user_key_for_email`` (``strip().lower()``) — same derivation
as folder visibility, so py and viewer-rs agree on the row's user_key.
"""

import os
import sqlite3
import threading
import time
from pathlib import Path

from app.folder_visibility import DB_FILENAME, _apply_schema, user_key_for_email

POSITION_MAX_USERS = 100
POSITION_MAX_FOLDERS = 512
# How far into the future a client ts may reach before being rejected.
# 5 minutes tolerates ordinary clock drift while bounding a wrong-clock
# write's damage to the same window.
POSITION_TS_FUTURE_SLACK_MS = 300_000
_BUSY_TIMEOUT_MS = 5000

STORED = "stored"
STALE = "stale"
REJECTED = "rejected"


def position_user_key(user: dict) -> str | None:
    """The caller's position-store user key, or None when the session
    carries no usable email (callers answer 403 — never a shared bucket)."""
    email = user.get("email") if isinstance(user, dict) else None
    return user_key_for_email(email)


# ── pure slot rules ────────────────────────────────────────────────────


def _slot_max_ts(slot: dict | None) -> int:
    """Newest ts over every folder record, tombstones included."""
    ts = (slot or {}).get("tombstone_ts", 0)
    for rec in (slot or {}).get("folders", {}).values():
        if rec.get("ts", 0) > ts:
            ts = rec["ts"]
    return ts


def _live_max_ts(slot: dict | None) -> int:
    """Newest ts over non-tombstone folder records only."""
    ts = 0
    for rec in (slot or {}).get("folders", {}).values():
        if not rec.get("tombstone") and rec.get("ts", 0) > ts:
            ts = rec["ts"]
    return ts


def _latest_position(slot: dict | None) -> dict | None:
    """The newest non-tombstone record across the slot's folders (v0 view)."""
    latest = None
    for rec in (slot or {}).get("folders", {}).values():
        if rec.get("tombstone"):
            continue
        if latest is None or rec["ts"] > latest["ts"]:
            latest = rec
    return latest


class PositionBook:
    """Per-user position rules over one slot — pure Python, no I/O.

    ``slot`` is the ``{"folders", "tombstone_ts", "updated_at"}`` dict the
    old in-memory store kept; ``folders`` maps folder → rec where a live
    rec is ``{folder,page,total,ts,updated_at}`` and a tombstone is
    ``{tombstone:True,ts,updated_at}``. Folder order inside ``folders``
    is insertion order (first INSERT wins on overwrite), which the SQLite
    store reproduces via ``rowid``.
    """

    def __init__(self, slot: dict | None = None):
        slot = slot if isinstance(slot, dict) else {}
        folders = slot.get("folders")
        self.folders = folders if isinstance(folders, dict) else {}
        self.tombstone_ts = slot.get("tombstone_ts") or 0
        self.updated_at = slot.get("updated_at") or 0.0

    def to_slot(self) -> dict:
        return {
            "folders": self.folders,
            "tombstone_ts": self.tombstone_ts,
            "updated_at": self.updated_at,
        }

    # ── reads ──

    def latest(self) -> dict | None:
        return _latest_position(self.to_slot())

    def max_ts(self) -> int:
        return _slot_max_ts(self.to_slot())

    def list_positions(self, folder: str | None = None) -> list[dict]:
        """Non-tombstone records, newest ts first (stable on insertion
        order for ties) — the v1 GET shape minus updated_at handling,
        which the caller strips."""
        if folder is not None:
            rec = self.folders.get(folder)
            recs = [rec] if rec is not None and not rec.get("tombstone") else []
        else:
            recs = [r for r in self.folders.values() if not r.get("tombstone")]
        return sorted(recs, key=lambda r: r["ts"], reverse=True)

    # ── writes ──

    def _future_ts(self, rec: dict, now: float) -> bool:
        return rec["ts"] > int(now * 1000) + POSITION_TS_FUTURE_SLACK_MS

    def put_v0(self, rec: dict, now: float) -> str:
        """v0 single-slot semantics: accept only a global newest write.

        Rejects at-or-below a delete-all tombstone, at-or-below this
        folder's own delete tombstone, and strictly below the live newest
        — the same "newest ts wins, tombstone ties lose" contract the
        single-slot store had, extended over per-folder records.
        """
        if self._future_ts(rec, now):
            return REJECTED
        if rec["ts"] <= self.tombstone_ts:
            return STALE
        existing = self.folders.get(rec["folder"])
        if existing is not None and existing.get("tombstone") and rec["ts"] <= existing["ts"]:
            return STALE
        if rec["ts"] < _live_max_ts(self.to_slot()):
            return STALE
        self._write(rec, now)
        return STORED

    def put_v1(self, rec: dict, now: float) -> str:
        """v1 per-folder last-writer-wins: reject only an older/equal ts
        for the SAME folder, or at-or-below a delete-all tombstone."""
        if self._future_ts(rec, now):
            return REJECTED
        if rec["ts"] <= self.tombstone_ts:
            return STALE
        existing = self.folders.get(rec["folder"])
        if existing is not None:
            if existing.get("tombstone"):
                if rec["ts"] <= existing["ts"]:
                    return STALE
            elif rec["ts"] < existing["ts"]:
                return STALE
        self._write(rec, now)
        return STORED

    def _write(self, rec: dict, now: float) -> None:
        rec["updated_at"] = now
        self.folders[rec["folder"]] = rec
        self.updated_at = now
        self._trim()

    def delete_folder(self, folder: str, now: float) -> str:
        """Per-folder tombstone: blocks resurrection by an in-flight or
        boot-heal PUT at-or-below the wiped ts; a genuinely newer read
        always lands."""
        self.updated_at = now
        prev = self.folders.get(folder)
        self.folders[folder] = {
            "tombstone": True,
            "ts": (prev or {}).get("ts", 0),
            "updated_at": now,
        }
        return STORED

    def delete_all(self, now: float) -> str:
        """Delete-all tombstone: wipes every folder record and records the
        newest wiped ts so stale writes can't resurrect them."""
        self.tombstone_ts = self.max_ts()
        self.folders = {}
        self.updated_at = now
        return STORED

    def _trim(self) -> None:
        """Bound the per-folder map; evict the least-recently-written
        entry (first minimum in insertion order, like dict min()).

        ``while`` — not the old in-memory ``if`` — because delete_folder
        can grow the map past the bound without trimming, and viewer-rs's
        port evicts down to the bound on the next write; a single evict
        would leave the two backends holding different folder sets."""
        while len(self.folders) > POSITION_MAX_FOLDERS:
            stalest = min(
                self.folders,
                key=lambda f: self.folders[f].get("updated_at", 0),
            )
            del self.folders[stalest]


# ── SQLite store ───────────────────────────────────────────────────────


class ReadingPositionStore:
    """SQLite-backed position store on the shared user-state DB file.

    Same lifecycle as ``FolderVisibilityStore``: the constructor performs
    no I/O, the schema is applied lazily under a lock on first connect,
    and every operation opens a fresh connection (WAL + busy_timeout make
    concurrent py/viewer-rs access safe).
    """

    def __init__(self, path: Path):
        self._path = Path(path)
        self._init_lock = threading.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
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

    # ── slot (de)serialization ──

    @staticmethod
    def _load_slot(conn: sqlite3.Connection, user_key: str) -> dict | None:
        """Rebuild the ``{folders, tombstone_ts, updated_at}`` slot.

        Folder rows come back in ``rowid`` order = first-insertion order,
        matching the dict order the in-memory store had (a folder's first
        INSERT fixes its position; later PUTs UPDATE in place).
        """
        user_row = conn.execute(
            "SELECT tombstone_ts, updated_at FROM position_users WHERE user_key = ?",
            (user_key,),
        ).fetchone()
        rows = conn.execute(
            "SELECT folder, page, total, ts, tombstone, updated_at"
            " FROM position_folders WHERE user_key = ? ORDER BY rowid",
            (user_key,),
        ).fetchall()
        if user_row is None and not rows:
            return None
        folders = {}
        for folder, page, total, ts, tombstone, updated_at in rows:
            if tombstone:
                folders[folder] = {
                    "tombstone": True,
                    "ts": ts,
                    "updated_at": updated_at,
                }
            else:
                folders[folder] = {
                    "folder": folder,
                    "page": page,
                    "total": total,
                    "ts": ts,
                    "updated_at": updated_at,
                }
        tomb_ts, upd = user_row if user_row is not None else (0, 0.0)
        return {"folders": folders, "tombstone_ts": tomb_ts, "updated_at": upd}

    @staticmethod
    def _save_slot(conn: sqlite3.Connection, user_key: str, slot: dict) -> None:
        conn.execute(
            "INSERT INTO position_users (user_key, tombstone_ts, updated_at)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(user_key) DO UPDATE SET"
            " tombstone_ts = excluded.tombstone_ts,"
            " updated_at = excluded.updated_at",
            (user_key, slot["tombstone_ts"], slot["updated_at"]),
        )
        conn.execute(
            "DELETE FROM position_folders WHERE user_key = ?", (user_key,)
        )
        for folder, rec in slot["folders"].items():
            if rec.get("tombstone"):
                conn.execute(
                    "INSERT INTO position_folders"
                    " (user_key, folder, page, total, ts, tombstone, updated_at)"
                    " VALUES (?, ?, NULL, NULL, ?, 1, ?)",
                    (user_key, folder, rec["ts"], rec["updated_at"]),
                )
            else:
                conn.execute(
                    "INSERT INTO position_folders"
                    " (user_key, folder, page, total, ts, tombstone, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, 0, ?)",
                    (
                        user_key,
                        folder,
                        rec["page"],
                        rec["total"],
                        rec["ts"],
                        rec["updated_at"],
                    ),
                )

    @staticmethod
    def _evict_stalest_user(conn: sqlite3.Connection) -> None:
        """MAX_USERS bound: drop the least-recently-updated user slot."""
        row = conn.execute(
            "SELECT user_key FROM position_users"
            " ORDER BY updated_at ASC, rowid ASC LIMIT 1"
        ).fetchone()
        if row is None:
            return
        key = row[0]
        conn.execute("DELETE FROM position_folders WHERE user_key = ?", (key,))
        conn.execute("DELETE FROM position_users WHERE user_key = ?", (key,))

    # ── reads ──

    def latest(self, user_key: str) -> dict | None:
        conn = self._connect()
        try:
            slot = self._load_slot(conn, user_key)
            return PositionBook(slot).latest()
        finally:
            conn.close()

    def list_positions(self, user_key: str, folder: str | None = None) -> list[dict]:
        conn = self._connect()
        try:
            slot = self._load_slot(conn, user_key)
            return PositionBook(slot).list_positions(folder)
        finally:
            conn.close()

    # ── writes ──

    def _write(self, user_key: str, op) -> str:
        """One transaction: load slot, apply the book op, persist on
        STORED. MAX_USERS eviction runs only for a successful write by a
        user with no slot yet — a rejected PUT must not evict anyone."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                slot = self._load_slot(conn, user_key)
                book = PositionBook(slot)
                result = op(book)
                if result == STORED:
                    if slot is None:
                        n = conn.execute(
                            "SELECT COUNT(*) FROM position_users"
                        ).fetchone()[0]
                        if n >= POSITION_MAX_USERS:
                            self._evict_stalest_user(conn)
                    self._save_slot(conn, user_key, book.to_slot())
                conn.commit()
                return result
            except BaseException:
                conn.rollback()
                raise
        finally:
            conn.close()

    def put_v0(self, user_key: str, rec: dict) -> str:
        return self._write(user_key, lambda book: book.put_v0(rec, time.time()))

    def put_v1(self, user_key: str, rec: dict) -> str:
        return self._write(user_key, lambda book: book.put_v1(rec, time.time()))

    def delete_folder(self, user_key: str, folder: str) -> str:
        return self._write(
            user_key, lambda book: book.delete_folder(folder, time.time())
        )

    def delete_all(self, user_key: str) -> str:
        return self._write(user_key, lambda book: book.delete_all(time.time()))

    def delete_user(self, user_key: str) -> None:
        """Remove the user's whole slot (``?hard=1`` test reset) — unlike
        delete_all this leaves no tombstone."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "DELETE FROM position_folders WHERE user_key = ?", (user_key,)
                )
                conn.execute(
                    "DELETE FROM position_users WHERE user_key = ?", (user_key,)
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        finally:
            conn.close()
