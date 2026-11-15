# pytest Phase 2b triage: quarantined tests (port vs retire with app/) — #472

Date: 2026-11-14. Phase 2a (#471) retargeted the live-server `server` fixture to
viewer-rs (`BOOKQ_TEST_SERVER=rs`) and quarantined files that import `app.*` or
hardcode v0 `/api/*`. This report triages that pool so nothing is silently lost
when `app/` is deleted in Phase 3.

Method: evaluated the conftest collection hook (`tests/conftest.py:111-130`)
statically against all `tests/test_*.py` (120 files). `app_import` =
`^\s*(from|import)\s+app[.\s]`; `v0_url` = quoted `/api/` not followed by v1.
For each hit, read the file head + `/api/` call sites and grepped
`rust/viewer-rs/src/**/*.rs` (`#[test]`/`#[tokio::test]` fns) + `api/vectors/*`
for the covering contract test. Rule: **retire** when Python-internal or v0
wire that intentionally dies with the app; **port** when a contract the Rust
backend must keep has no equivalent coverage.

## Code change in this PR (1 line + comment)

`tests/conftest.py`: `v0_url` `(?!v1/)` → `(?!v1\b)`. The old lookahead also
matched bare "`/api/v1`" prose (no trailing slash, e.g.
`tests/test_pwa_api_client.py` docstrings/comments), wrongly quarantining a
server-agnostic static lint. Verified:

- `` `/api/v1` prose``: old=quarantine, new=run
- `"/api/v1/tree"`: run in both
- `"/api/folders"`, `"/api/health"`: quarantine in both
- `"/api/v1"` (bare): old=quarantine, new=run (correct — not a v0 path)

Effect: `test_pwa_api_client.py` (no server fixture, checks
`app/static/api/` literals + `api/openapi.v1.yaml` freshness) now **runs under
`BOOKQ_TEST_SERVER=rs`**. All other quarantine decisions unchanged.

Counts (after fix): 120 `test_*.py` → **53 run under rs**, **67 quarantined**.
Pre-fix: 52 run / 68 quarantined (only delta is the false positive).

## Runs under rs after fix (1 file)

| file | why it runs | coverage |
|---|---|---|
| `test_pwa_api_client.py` | static JS lint, no server/`app.*`; only v0 hits were bare `` `/api/v1` `` prose | self-contained (gen check vs `api/openapi.v1.yaml`) |

## Retires with app/ (Python-internal or v0 wire — 37 files)

Each has a named Rust-side or contract-side equivalent, or is
justified as Python-internal with no portable contract.

| file | bucket | justification + Rust/contract equivalent |
|---|---|---|
| `test_background_fill.py` | `app.*` unit (`routes._background_fill`) | startup shallow+fill internals; rs builds natively. Nearest: `folders.rs` ensure path (different design). Retire. |
| `test_bearer_auth.py` | `app.auth` unit | connection-auth order already in rs: `auth.rs::{bearer_claims,validate_bearer,session_wins_over_bearer,session_cookie_identifies_caller}` |
| `test_cover_warmup.py` | `app.*` warmup threads | py warmup concurrency; rs is on-demand: `thumbs.rs::thumb_generate_caches_and_304s` |
| `test_disable_index.py` | `BOOKQ_DISABLE_INDEX` flag | transition flag dies with app; no rs equivalent. Retire. |
| `test_ensure_index.py` | `_ensure_index`/lock internals | TTL/single-flight internals; rs: `folders.rs` ensure path (different). Retire. |
| `test_index_building_202.py` | `202+Retry-After` internals | cold-miss budget internals; rs: `folders.rs::ensure_tree_and_index_wait` (different). Retire. |
| `test_metadata.py` | `app/metadata` models | `ComicMetadata`/`_calculate_relevance` internals; rs: `search.rs` index path (no 1:1). Retire. |
| `test_offline_thumb_prefetch.py` | `scanner` + Playwright | JS `offline-core`/prefetch + Cache/quota, not backend. Retire. |
| `test_paired_view.py` | `reader_sync.model` + Playwright | S06 JS `projectSpread`/DOM; rs model only `reader_sync/model.rs::{spread_plan_offsets,turn_goto_index}`. Retire file. |
| `test_reader_sync_client.py` | `reader_sync.*` + Playwright | S07 browser JS client/backoff, no backend. Retire. |
| `test_reader_sync_model.py` | `reader_sync.model/protocol` unit | frozen spread logic; `reader_sync/model.rs::{spread_plan_offsets,turn_goto_index}` + `protocol.rs` (2 tests). Retire. |
| `test_reader_transport.py` | in-process ASGI | frozen ASGI hello/ping/pong; rs transport in `reader_sync/mod.rs`. Retire. |
| `test_reading_positions_store.py` | `app.reading_positions` + v0 | frozen store; `state.rs::{position_store_roundtrip_and_tombstone,position_book_conformance_cases}` + `api/vectors/positions-cases.json`. Retire. |
| `test_scan_phase_timings.py` | lifespan log line | `walk/archive/pdf_ms` log, no rs contract. Retire. |
| `test_scanner.py` | `app.scanner` unit | ordering/flatten internals; `bookq-core::{natural_cmp_orders_like_python_tuples,sort_file_cmp_cover_first}` + `archive.rs` (16 tests), `tree.rs::{archive_node,archive_node_count}`. Retire. |
| `test_scanner_perf_todo6.py` | perf pins | shape/perf pins; archive shape in `bookq-core/archive.rs`. Retire. |
| `test_shallow_warmup.py` | shallow internals | frozen shallow path; rs `folders.rs::shallow_live` + `parse_i64_saturates_overflow`. Retire as-written. |
| `test_stale_serve.py` | TTL/stale internals | frozen TTL; rs `folders.rs` `Ensure::Stale` path. Retire. |
| `test_thumb_cache_hit_no_open.py` | archive-open counting | frozen counting; `thumbs.rs::{thumb_generate_caches_and_304s,store_and_prune}`. Retire. |
| `test_thumb_sprite.py` | frozen endpoint | `thumbs.rs::{sprite_strip_dims_match_python,sprite_grid_dims_follow_w_class,sprite_archive_native_and_fail_open,sprite_grid_all_bad_404s_and_bad_rows_falls_back}`. Retire. |
| `test_thumbs.py` | frozen endpoint | `thumbs.rs` (25 tests): `thumb_shrinks_to_width`, `thumb_never_upscales`, `thumb_generate_caches_and_304s`, `decode_gate_truth_table`, etc. Retire. |
| `test_traversal.py` | `_is_within` unit | frozen guard; rs normalizes in `serve.rs`/`tree.rs` (`serve.rs::image_entry_filter` + range/etag tests). Retire unit. |
| `test_tree_signature.py` | `app.tree_signature` | frozen digest; rs `bookq-core/scans.rs::tree_signature` + `lib.rs` natural_cmp tests. Retire. |
| `test_view_endpoint.py` | frozen normalization | `thumbs.rs::{view_caps_and_flattens,view_generate_caches_jpeg,view_w_normalizes_like_python,view_w_forces_normalized_jpeg,view_invalid_w_stays_passthrough,view_w_keys_cache_and_etag,make_view_renders_requested_width}`. Retire. |
| `test_warmup_baseline.py` | perf timing (`slow`) | fixed-scale baseline on frozen lifespan; no contract. Retire. |
| `test_warmup_breakdown.py` | caplog fields | `scan/rebuild/import_ms` log fields; no rs contract. Retire. |
| `test_warmup_cache_mirror.py` | `app.warmup_cache` | py-only disk mirror; rs uses in-memory `Arc<CacheData>` + snapshot. Retire. |
| `test_warmup_cache_race.py` | `app.warmup_cache` | py-only race; no rs counterpart. Retire. |
| `test_warmup_lifespan.py` | asyncio lifespan | frozen hand-off; rs startup is `folders.rs` ensure path. Retire. |
| `test_warmup_persistent_cache.py` | non-existent module (RED) | references missing `app.warmup_cache`; frozen. Retire. |
| `test_compose_config.py` | static py runtime | pins hypercorn/`WEB_CONCURRENCY`/py healthcheck; rs deploys `viewer-rs` binary. Retire. |
| `test_helpers.py` | harness helper | tests `tests/helpers.py` 202-poll/retry, not product. Retire. |
| `test_top_display_perf.py` | perf harness | display-complete timing; thumb-cover overlapped by `thumbs.rs`. Retire. |
| `test_v0_deprecation.py` | v0 headers | asserts v0 `Deprecation` headers; under rs v0 answers 501 (policy #212/#407). Retire (or keep as rs-501 pin, not a port). |
| `test_wipe_all.py` | py-only `/api/wipe|keep` | rs has no wipe/keep (only `DELETE /api/v1/test-positions-reset`, `main.rs:1539`). Retire as-written. |
| `test_refresh_endpoint.py` | `POST /api/refresh` (v0) | v0 wire dies; v1 `POST /api/v1/index/refresh` contract lives in `main.rs::api_refresh` (port backlog, not this PR). Retire as-written. |
| `test_sw_offline_contract.py` (partial) | Dockerfile pins | `Dockerfile` copies `app` pins die (rs uses `Dockerfile.viewer-rs`); static-asset precache port is backlog. Retire Dockerfile part. |

## Port backlog (contractual — v1 rewrite needed, NOT in this PR, recorded so Phase 3 does not silently drop them — 30 files)

These stay quarantined under rs in this PR. Each needs an rs-spawned
(HTTP/WS/Playwright, v1 URLs, no `app.*`) rewrite as follow-up. No coverage
regression today because the behavior is still exercised in the default
(python-server) lane; the Rust-side unit coverage cited is the bridge until
the e2e ports land.

| file | contract to preserve | nearest Rust/unit cover today |
|---|---|---|
| `test_api_v1.py` | v1 envelope `{kind,nodes,generation}`, LWW | `folders.rs::api_tree`, `search.rs::api_search`, `main.rs::auth_token` |
| `test_archive.py` (HTTP) | `/api/v1/*` + `/image/<archive>/<entry>` | `serve.rs::{resolve_archive,list_archive_entries,archive_range_ignores_invalid}` |
| `test_archive_nested.py` (HTTP) | recursive `vol01(+sub)` tree + prefix guards | `tree.rs::archive_node`, `serve.rs::list_archive_prefix` |
| `test_archive_split.py` | multi-top-dir split vs single/EPUB-flat | `tree.rs::{archive_node,archive_node_count}` |
| `test_auth_token.py` | PKCE-S256/expiry/rotation/JWT + native redirect | `oauth.rs::{token_urlsafe_shape,code_exchange_roundtrip_and_one_time,refresh_rotation_kills_old_token,token_endpoint_response_shape,minted_access_passes_bearer_validation}` |
| `test_conformance_vectors.py` | shared `api/vectors/*.json` | `conformance_vectors.rs::{work_chapters_vectors,path_lookup_vectors,encode_path_vectors}` |
| `test_filetype_matrix.py` | 14-type images/thumb matrix | `thumbs.rs::{decode_gate_truth_table,thumb_view_python_only_and_unsupported_proxy,thumb_undecodable_proxies}`, `serve.rs::is_archive_file` |
| `test_folder_visibility_api.py` | v1 visibility 401/HMAC/validation/share | `state.rs::{visibility_owner,covered_by_roots,get_folder_visibility,put_folder_visibility}`, `main.rs::folder_visibility` |
| `test_folder_visibility_store.py` (logic) | GLOBAL_USER_KEY persistence semantics | `state.rs::visibility_owner` (no 1:1 store test) |
| `test_folders_shallow.py` | `?path=&depth=1` top-level-only | `tree.rs::shallow_live`, `folders.rs::api_tree` |
| `test_index_gate.py` | ETag/`?depth=1` bypass | `folders.rs::api_tree`, `tree.rs::shallow_live` |
| `test_media_decode_contract.py` | native vs Proxy gate (#406 §7) | `thumbs.rs::{decode_gate_truth_table,thumb_view_python_only_and_unsupported_proxy}` |
| `test_reader_manifest.py` | S04 `resolve_work` | `reader_sync/catalog.rs::{build_manifest_core,manifest_digest,split_manifest}` |
| `test_reader_pairing.py` | S03 registry/match/heartbeat | `reader_sync/mod.rs::{paired,session,end_all_resets_every_live_session}` |
| `test_reader_state.py` | S05 sequencer/LWW | `reader_sync/state.rs::{gate_page_turn,check_epoch,apply_turn,apply_goto}` |
| `test_reader_sync_golden.py` | #210 dual-harness lockstep | `reader_sync/golden.rs::golden_cases_conform`, `catalog.rs::manifest_digest` |
| `test_reader_ws.py` | WS hello/ping/join | `reader_sync/mod.rs` (no WS e2e yet) |
| `test_reader_ws_state.py` | WS resume/replay | `reader_sync/catalog.rs`, `state.rs` |
| `test_thumbnail_simulator_mode.py` | `?thumb_perf=1` probe | `main.rs:685`, `shell.rs::PROBE_EXPR` (no dedicated test) |
| `test_compression.py` | gzip contract | `serve.rs::maybe_gzip*` (no HTTP-gzip test) |
| `test_filters_e2e.py` | facets/search UI → v1 | `search.rs` (0 e2e) |
| `test_harness_prod.py` | version/health/harness | `main.rs::{api_version,api_health}` |
| `test_mixed_folder.py` | images-tile flow → v1 | `serve.rs::image_entry_filter` |
| `test_offline_full_browse.py` | offline browse/viewer → v1 | none (frontend + v1) |
| `test_offline_harness.py` | `?offline=1` modes | none |
| `test_reading_position.py` | resume sync → v1 | `state.rs::{position_store_roundtrip_and_tombstone,position_book_conformance_cases}`, `api/vectors/positions-cases.json` |
| `test_search_e2e.py` | search UI → v1 | `search.rs` (0 e2e) |
| `test_task7_harness_baseline.py` | harness/thumb shell | `shell.rs` viewer-branch |
| `test_ui_logic.py` | viewer/counter/decode → v1 | none single |
| `test_viewer_slider_per_book.py` | chapter slider (server-agnostic) | `reader_sync/model.rs::spread_plan_offsets` |

## Acceptance mapping

- Every quarantined file is either running under rs (53 files incl. the
  un-quarantined lint above) or listed here as retires-with-app (37) / port
  backlog (30). 53 + 37 + 30 = 120 `test_*.py` (full file list, no silent loss).
- No coverage regression: each retired file cites a Rust-side equivalent
  above (or is justified Python-internal: warmup/index internals, ASGI/TestClient
  harness, py-only flags/modules, v0 wire).
- `BOOKQ_TEST_SERVER=rs pytest -q` stays 0 failures: quarantine set only
  shrinks by one server-agnostic static test; all other skips unchanged.
  (Full pytest not run in this env — no `uv`/pytest; verified by regex unit
  check + `cargo test -p viewer-rs` where feasible. See PR body.)
