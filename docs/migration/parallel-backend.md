# Parallel-backend decision — viewer-rs (ADR, issue #163)

Status: adopted (supersedes the in-process PyO3 model of the W-series)

## Context

W1–W4 (`bookq_rs` PyO3 extension, PRs #172/#176/#177) proved the pure-Rust
ports correct — byte-identical sort keys, archive listings, fs walks, tree
signatures and metadata records on the live corpus, with measured speedups
(sort walk ~3x, signature ~12x, metadata rebuild ~3x) — but required touching
`app/*.py` to wire per-function dispatch (`_py_*` + `BOOKQ_RUST`).

The policy that governs this migration was refined: **the existing backend is
never touched; the Rust rewrite proceeds as a second backend that can be
selected by switching the connection target.** Old and new backends run side
by side and are compared at the HTTP API surface.

## Decision

- **`viewer-rs`**: a standalone Rust server (axum + hyper) deployed in the
  same dev/prod slot pattern as `viewer`, reachable on its own hostname
  (`dev-XX.<tailnet>.ts.net` via `./dev.sh up <name> --rs`).
- **Proxy fall-through**: viewer-rs natively implements migrated endpoints
  only; every other request is forwarded to `viewer:8080` byte-transparently
  (headers, bodies, and WebSocket upgrade tunnels included). The backend is
  usable from day one; endpoints migrate one at a time in ROI order; stopping
  partway is a coherent end state (low-ROI paths can stay proxied forever).
- **`bookq-core`**: the verified pure-Rust logic harvested from the closed
  PyO3 PRs (sort keys, archive readers, fs walks, metadata parsing), kept in
  `rust/bookq-core` as a library for viewer-rs.
- **Switching**: hostname/slot-level — compare `dev-XX` (viewer-rs) against
  another slot or `bookq` (Python). The frontend uses same-origin relative
  URLs so it needs no changes; session cookies and OAuth flow through the
  proxy unchanged.
- **app/ untouched**: the Python backend keeps running unchanged; no
  dispatch code, no flags. Rollback = route the hostname at `viewer` again.

## Migration plan (R-series, replaces W-series)

| Wave | Scope | Gate |
|---|---|---|
| R0 | workspace + viewer-rs catch-all proxy (HTTP + WS upgrade) + `Dockerfile.viewer-rs` + `dev.sh --rs` + `docker-compose.dev-rs.yml` + API diff harness | rs slot serves all endpoints via proxy; responses identical to viewer |
| R1 | read-heavy endpoints in ROI order (`/api/images`, `/image`, `/api/folders`, `/api/search`) on bookq-core | per-endpoint byte diff vs viewer on live data + bench |
| R2 | `/thumb/*` / sprites — acceptance switches to pixel-approx + HTTP contract (JPEG bytes differ across encoders) | same |
| R3 | stateful/control paths (folder_visibility SQLite, reader_sync WS, OAuth, statics) — keep proxied or port, decided per measurement | decision recorded here |

## Consequences

- Closed: #172 (W1 sort keys), #176 (W2 archive readers), #177 (W3+W4 fs
  walks + metadata). Branches retained; the pure-Rust modules moved to
  `bookq-core` essentially verbatim (the PyO3 layer was a thin shell).
- CI remains blocked by account billing — verification is local suite +
  live-corpus parity + dev-env smoke, same as before.
- `docker-compose.dev-rs.yml` / `docker-compose.local-rs.yml` are additive
  overlays; the base dev compose path is unchanged for non-rs envs.
