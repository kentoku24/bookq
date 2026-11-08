-- User-state SQLite schema — single source of truth (#200).
--
-- Shared by FastAPI (app/folder_visibility.py) and viewer-rs
-- (rust/viewer-rs/src/state.rs) on the same `user-state` volume
-- (/var/lib/bookq/folder-visibility.sqlite3) during the strangler
-- migration (#163): an internal backend contract, NOT part of the
-- external API contract (api/openapi.v1.yaml), and provisional — when
-- FastAPI retires, viewer-rs inherits this file as-is.
--
-- The hidden set is library-global: every hidden_roots row lives under
-- the sentinel user_key '*' (GLOBAL_USER_KEY in both implementations).
-- The column name stays `user_key` for schema compatibility; first
-- connect folds any legacy per-user rows into '*' as a union.
--
-- Applied idempotently on first connect; the trailing PRAGMA stamps the
-- schema version. When changing this file, bump the version here AND in
-- SCHEMA_VERSION (app/folder_visibility.py) / VIS_SCHEMA_VERSION
-- (rust/viewer-rs/src/state.rs) — a side that finds the DB stamped newer
-- than its own constant refuses rather than silently downgrading.

CREATE TABLE IF NOT EXISTS visibility_users (
  user_key TEXT PRIMARY KEY,
  revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS hidden_roots (
  user_key TEXT NOT NULL,
  root TEXT NOT NULL,
  PRIMARY KEY (user_key, root)
);

PRAGMA user_version = 1;
