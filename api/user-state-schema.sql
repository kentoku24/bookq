-- User-state SQLite schema — single source of truth (#200).
--
-- Applied by viewer-rs (rust/viewer-rs/src/state.rs) on the `user-state`
-- volume (/var/lib/bookq/folder-visibility.sqlite3). This is an internal
-- backend contract, NOT part of the external API contract
-- (api/openapi.v1.yaml). The FastAPI side retired in #503; the file and
-- the DB volume survived unchanged.
--
-- The hidden set is library-global: every hidden_roots row lives under
-- the sentinel user_key '*' (GLOBAL_USER_KEY in viewer-rs). The column
-- name stays `user_key` for schema compatibility.
--
-- Applied idempotently on first connect; the trailing PRAGMA stamps the
-- schema version. When changing this file, bump the version here AND in
-- USER_STATE_SCHEMA_VERSION (rust/viewer-rs/src/state.rs) — a binary that
-- finds the DB stamped newer than its own constant refuses rather than
-- silently downgrading.

CREATE TABLE IF NOT EXISTS visibility_users (
  user_key TEXT PRIMARY KEY,
  revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS hidden_roots (
  user_key TEXT NOT NULL,
  root TEXT NOT NULL,
  PRIMARY KEY (user_key, root)
);

-- Reading positions (#375): per-user slot of per-folder LWW records.
-- position_users holds the slot-level tombstone_ts/updated_at; each live
-- record is a position_folders row (tombstone=0) and each folder delete a
-- tombstone=1 row carrying the wiped ts — the same shape the in-memory
-- {folders, tombstone_ts, updated_at} slot had. rowid preserves first-
-- insertion order so ts ties sort stably exactly like the old dict did.
CREATE TABLE IF NOT EXISTS position_users (
  user_key TEXT PRIMARY KEY,
  tombstone_ts INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS position_folders (
  user_key TEXT NOT NULL,
  folder TEXT NOT NULL,
  page INTEGER,
  total INTEGER,
  ts INTEGER NOT NULL DEFAULT 0,
  tombstone INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (user_key, folder)
);

-- User directory (#608): the authoritative membership + role store.
-- Rows are written only by the offline CLI (`viewer-rs users`); there is no
-- HTTP write path. `email` is normalized (trim+lowercase) before every
-- write; COLLATE NOCASE is belt-and-braces for manual SQL.
--
-- `ALLOWED_EMAILS` is a one-time seed on first init (guarded by the
-- `settings.users_seeded` marker), then the DB is authoritative. A disabled
-- user keeps the row (`disabled_at`) for audit instead of being deleted.
CREATE TABLE IF NOT EXISTS users (
  email       TEXT NOT NULL COLLATE NOCASE PRIMARY KEY,
  role        TEXT NOT NULL DEFAULT 'member'
              CHECK (role IN ('admin', 'member')),
  disabled_at REAL,                       -- NULL = active
  created_at  REAL NOT NULL,              -- Unix epoch seconds
  created_by  TEXT NOT NULL,              -- 'seed' / 'cli' / a ticket ref
  note        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

PRAGMA user_version = 3;
