# bookq Threat Model

Drafted from the code and confirmed with the owner (2026-10-29). Used to prioritize security-scan findings; keep it in sync when the security posture changes.

## System overview

bookq is a self-hosted PWA for browsing/caching/reading a local library of image folders and archives (CBZ/ZIP/RAR/EPUB/PDF). One FastAPI worker (`app/`), plain-script frontend (`app/static/`), no build step, no database (in-memory indexes + one SQLite file for folder visibility + in-memory reader-sync registry). Deployment is Docker Compose: `viewer` (hypercorn :8080, no host port) plus a `tailscale` sidecar that is the only route in — `tailscale serve` terminates HTTPS :443 and proxies plain HTTP to `viewer:8080`. Tailnet ACLs, not the app, decide who can open the URL at all.

## Trust boundaries

1. Internet → tailnet: enforced entirely by Tailscale. The app assumes every request it sees is already inside the tailnet.
2. Tailnet → authenticated user: `tailscale serve` → viewer → Google OAuth (`/auth/login`, `/auth/callback`) → `ALLOWED_EMAILS` allowlist → signed-cookie session (starlette `SessionMiddleware`, `https_only`, 7-day). `require_auth` gates every API route; `/` renders login vs. viewer branches; `/api/health` and `/api/version` are the only intentionally open endpoints.
3. Viewer → host filesystem: `DOWNLOADS_HOST_DIR` bind-mount into the container; request paths contained by `_is_within()` in `app/routes.py` (resolve + `is_relative_to`, unresolvable targets = outside = 403). `user-state` volume holds per-user settings; warmup/thumb caches on separate volumes.
4. Viewer → untrusted file content: `app/archive.py` (zip/rar/epub), `app/archive_pdf.py` (pypdfium2), Pillow. Compressed RAR entries are extracted by spawning a `7zip` subprocess per page.

## Components

- **web-client** — `app/templates/`, `app/static/` (PWA, service worker, IndexedDB offline store).
- **api-server** — `app/routes.py`, `app/main.py`, `app/scanner.py`, `app/metadata/`, `app/thumbs.py`, `app/folder_visibility*.py`, `app/compression.py`, `app/static_files.py`.
- **auth** — `app/auth.py` (Google OAuth, allowlist, session cookies), `SessionMiddleware` config, `SECRET_KEY`/`ALLOWED_EMAILS` handling in `app/config.py`.
- **reader-sync** — `app/reader_sync/` (WebSocket `/ws/reader-sync`, 6-digit pairing codes, leader/follower session registry).
- **archive-parsers** — `app/archive.py`, `app/archive_pdf.py`, and the archive-processing code paths inside `app/scanner.py`/`app/thumbs.py` (zip/rar/epub/pdf parsing, `7zip` subprocess, Pillow).
- **deployment** — `docker-compose*.yml`, `Dockerfile`, `tailscale/` (sidecar entrypoint, `tailscale serve`), `dev.sh`, `prd.sh`, `rebuild.sh`, `.githooks/`.
- **dev-env** — `docker-compose.dev.yml`, `dev.sh` slot pool (dev nodes run `BOOKQ_TEST_MODE=true`).

## Entry points

- `/` HTML (login vs viewer branch on session), `/api/health`, `/api/version` — unauthenticated.
- `/auth/login`, `/auth/callback`, `/auth/logout` — OAuth flow.
- `/api/folders`, `/api/search`, `/api/metadata/facets`, `/api/refresh`, `/api/images`, `/image/{path}`, `/thumb/{path}`, `/view/{path}`, `/api/thumb-sprite`, `/api/reading-position` (GET/PUT/DELETE), `/api/folder-visibility` (GET/PUT) — all `require_auth`.
- `/ws/reader-sync` — WebSocket, same signed-cookie session (`user_from_connection`), 6-digit code join.
- Archive/epub/pdf content — parsed from library files; reachable by anyone who can write into `downloads/`.
- `tailscale serve` config and the tailnet node itself — the perimeter.

## Assets (owner-ranked)

1. **Privacy of library contents** — image/folder contents and structure in `downloads/`; the data the app exists to serve, only to allowlisted users. Includes the tree listing and folder names.
2. **Host filesystem outside the downloads dir** — path traversal is the worst single bug class (read/write outside the mount). `_is_within()` is the control.
3. **Auth material** — `SECRET_KEY` (session-cookie forgery), Google OAuth client secret, `TS_AUTHKEY`, `.env`.
4. **Availability** — slow/solid archives, per-page 7zip spawns, rescan-on-request. Lower value: the owner accepts DoS as risk.
5. **Per-user state** (hidden roots, reading positions) — convenience data, not a confidentiality boundary.

## Attacker profiles (from the owner)

- **Internet attacker, no tailnet access**: matters — should be stopped by Tailscale + OAuth; findings that make the app reachable or exploitable without tailnet are real.
- **Tailnet member / device not on the allowlist**: matters — reaches the login page and every endpoint unauthenticated; findings that bypass or weaken the allowlist gate are real.
- **Malicious archive/PDF/EPUB in the library**: matters — parser vulns (pypdfium2, rarfile, Pillow, 7zip invocation, zip-traversal in `/image/{archive}/{inner}`) are real threats inside the container.
- **Logged-in allowlisted user crossing into another user's state**: explicitly NOT a threat — allowed users are trusted; per-user separation is convenience only.
- **DoS / resource exhaustion**: accepted risk.

## Existing controls

- OAuth + email allowlist; `BOOKQ_TEST_MODE` refuses to start under `BOOKQ_ENV=production` (fail-closed).
- `_is_within()` path containment + `tests/test_traversal.py`.
- Signed session cookie, `https_only`, `same_site=lax`; `SECRET_KEY` must be set in prod.
- reader-sync: codes never logged, single-use, TTL'd, leader approval; unauth WS closed 4401.
- Archive reads fail-closed per entry (bad archives → `[]`, bad entries → 404/501, never 500).
- No host port on `viewer`; only compose-internal `viewer:8080` reachable from the sidecar.

## Out of scope / accepted risk (owner-declared)

- Dev pool environments (`dev-XX` nodes, `BOOKQ_TEST_MODE=true`, auth bypassed) — accepted; findings that only apply to dev/test mode are low/none.
- DoS/resource exhaustion (slow archives, big files, rescan load).
- Cross-user privacy among allowlisted users (hidden roots, reading positions).

## Open questions

- None blocking. If per-user privacy is ever meant to be enforced, reading-position/folder-visibility findings need re-evaluation.
