# User-state DB — internal backend contract

`folder-visibility.sqlite3` on the `user-state` volume (`/var/lib/bookq`)
is **not** an external API contract. It is an internal backend contract
shared between FastAPI (`app/folder_visibility.py` +
`app/reading_positions.py`) and viewer-rs
(`rust/viewer-rs/src/state.rs`) while the strangler migration (#163)
routes some endpoints natively and proxies the rest: one writer's rows
are the other's reads, which is what makes per-endpoint cutover/rollback
free.

The DB holds two feature families, each keyed by the authenticated
user's normalized email:

- Folder visibility (`visibility_users`, `hidden_roots`) — added at
  schema v1.
- Reading positions (`position_users`, `position_folders`) — added at
  schema v2 (#375). Per-user, per-folder last-write-wins records with
  delete tombstones; both `/api/reading-position` (v0 latest-only view)
  and `/api/v1/reading-positions` read/write the same tables, so a
  process restart or a py↔rs route flip no longer loses progress.

- The DDL lives in exactly one file: `api/user-state-schema.sql`.
  viewer-rs applies it idempotently on first connect, embedded via
  `include_str!`.
- `PRAGMA user_version` stamps the schema version; `VIS_SCHEMA_VERSION`
  must match the file's stamp. A
  side that finds the DB stamped *newer* than its own constant refuses
  rather than silently downgrading — that is the drift detector for
  mixed-version deployments. The check runs only at store init (first
  connect) — a long-running process does not notice a mid-flight schema
  upgrade, so the real protection is boot ordering: upgrade the schema
  with a release, then restart everything that holds a connection.
- The file and the DB volume passed to viewer-rs unchanged when the
  FastAPI side retired (#503); the client-facing API
  (`api/openapi.v1.yaml`) was unaffected.

`user-state` is persistent user data, not a cache — cleanup paths must
not delete it.
