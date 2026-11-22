# User-state DB — internal backend contract

`folder-visibility.sqlite3` on the `user-state` volume (`/var/lib/bookq`)
is **not** an external API contract. It is an internal backend contract
owned by viewer-rs (`rust/viewer-rs/src/state.rs` +
`rust/viewer-rs/src/users.rs`) and embedded from
`api/user-state-schema.sql` via `include_str!`.

The DB holds the following feature families:

- Folder visibility (`visibility_users`, `hidden_roots`) — added at
  schema v1. Library-global: every row lives under the sentinel
  `user_key = '*'` (`GLOBAL_USER_KEY`).
- Reading positions (`position_users`, `position_folders`) — added at
  schema v2 (#375). Per-user, per-folder last-write-wins records with
  delete tombstones.
- User directory (`users`) — added at schema v3 (#608). The
  authoritative membership + role store (`role='admin'|'member'`).
  Rows are written **only** by the offline `viewer-rs users` CLI; there
  is no HTTP write path. `email` is normalized (trim + lowercase)
  before every write. A removed user is soft-disabled via
  `disabled_at`, not deleted, so the row survives as a minimal audit
  trail.
- Settings (`settings`) — added at schema v3. Internal metadata; the
  `users_seeded=1` marker records that the one-time `ALLOWED_EMAILS`
  seed has run. It prevents a restart from re-adding users after an
  operator intentionally removed them. Losing the DB loses the marker
  too, so an empty DB + env seed is the recovery path.

## Authorization

- `auth::resolve` / Bearer validation / the shell / the reader-sync
  WebSocket handshake read an in-memory `UserDirectory` of active
  members; a 1-second background refresh bounds member changes without
  a SQLite read on every request.
- `/admin` checks `users.role='admin' AND disabled_at IS NULL` against
  the DB per request, so admin revocation is immediate.
- The OAuth callback checks membership against the DB (a just-added
  member can log in on the first attempt) and refreshes the directory
  before redirecting to `/`.
- `ALLOWED_EMAILS` is a **one-time seed**, not an OR allowlist. After
  `settings.users_seeded`, changing the env has no authorization effect;
  use `viewer-rs users` instead.
- On a v2 → v3 upgrade the seed inserts the existing `ALLOWED_EMAILS` as
  `member`, preserving access. The first admin is assigned explicitly
  via the CLI (no automatic promotion).
- This branch's earlier v3 schema used `admin_users`; a one-time Rust
  migration copies only admins that are present in the seed allowlist
  (dormant grants are skipped rather than activated), then drops
  `admin_users`.

## Versioning

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
- The DB file is chmod 0600 and its parent 0700 by the store init.

`user-state` is persistent user data, not a cache — cleanup paths must
not delete it. It now carries the login allowlist and roles, so a restore
is an authorization-state change: run `viewer-rs users list` after
restoring and disable stale users. A lost volume with `ALLOWED_EMAILS`
set re-seeds those members on first boot (then assign an admin via the
CLI); without it, bootstrap with `viewer-rs users add ... --role admin`.
