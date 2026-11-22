# User-state DB — internal backend contract

`folder-visibility.sqlite3` on the `user-state` volume (`/var/lib/bookq`)
is **not** an external API contract. It is an internal backend contract
owned by viewer-rs (`rust/viewer-rs/src/state.rs` +
`rust/viewer-rs/src/admin.rs`) and embedded from
`api/user-state-schema.sql` via `include_str!`.

The DB holds three feature families, each keyed by the authenticated
user's normalized email or by a library-global key:

- Folder visibility (`visibility_users`, `hidden_roots`) — added at
  schema v1. Library-global: every row lives under the sentinel
  `user_key = '*'` (`GLOBAL_USER_KEY`).
- Reading positions (`position_users`, `position_folders`) — added at
  schema v2 (#375). Per-user, per-folder last-write-wins records with
  delete tombstones; both the v0 latest-only view and
  `/api/v1/reading-positions` read/write the same tables, so a process
  restart or a route flip no longer loses progress.
- Admin registry (`admin_users`) — added at schema v3 (#608). The
  library-global set of users allowed to reach `/admin` and (in
  follow-ups) privileged operations. Rows are written **only** by the
  offline `viewer-rs admin` CLI; there is no HTTP write path. `email` is
  normalized (trim + lowercase) before every write, and authorization is
  an AND with the normal session/Bearer + `ALLOWED_EMAILS` checks. See
  `docs/deployment.md` "管理者の登録" for the operator commands.

- The DDL lives in exactly one file: `api/user-state-schema.sql`.
  viewer-rs applies it idempotently on first connect.
- `PRAGMA user_version` stamps the schema version;
  `USER_STATE_SCHEMA_VERSION` must match the file's stamp. A binary that
  finds the DB stamped *newer* than its own constant refuses rather than
  silently downgrading — that is the drift detector for rolling back a
  schema. The check runs only at store init (first connect) — a
  long-running process does not notice a mid-flight schema upgrade, so
  the real protection is boot ordering: upgrade the schema with a
  release, then restart everything that holds a connection.
- v2 → v3 is additive (`CREATE TABLE IF NOT EXISTS`); existing
  visibility/position rows survive. A v2 binary refuses a v3 DB, so take
  a `user-state` snapshot before deploying v3 and restore it to roll
  back.
- The DB file is chmod 0600 and its parent 0700 by the store init. The
  file and the DB volume passed to viewer-rs unchanged when the FastAPI
  side retired (#503); the client-facing API
  (`api/openapi.v1.yaml`) was unaffected.

`user-state` is persistent user data, not a cache — cleanup paths must
not delete it. It now also carries privilege grants (`admin_users`), so
back it up and treat a restore as an authorization-state change: run
`viewer-rs admin list` after restoring and remove stale grants.
