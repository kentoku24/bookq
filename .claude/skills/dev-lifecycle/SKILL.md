---
name: dev-lifecycle
description: Use when verifying a bookq change before committing/PRing — spins up an isolated dev environment via dev.sh, runs the automated test suite plus a live smoke check against it, and tears it down. Trigger on "開発環境で確認して", "dev.sh で立てて", "verify this in dev", or before claiming a fix/feature works.
---

# bookq dev lifecycle

Verify a change against a real running container, not just against the Playwright suite's own throwaway server, then leave no trace behind. This is safe to run from any worktree, in parallel with other agents doing the same — see `docs/deployment.md` for why (environments share nothing: separate compose projects, networks, volumes, and tailnet nodes — no lock, no shared gateway).

**Never substitute this for touching production.** `dev.sh` only ever talks to `docker-compose.dev.yml`; it refuses a project name that would collide with `bookq` (prod). Don't `cd` into the main checkout and run bare `docker compose` commands as a shortcut — a bare `up` loads `docker-compose.yml` (production) under a directory-derived project name and starts a second `bookq` tailnet node that fights prod. That's the exact mistake that has taken prod down before (see `docs/deployment.md` → 過去の障害).

## When to use this

- Before telling the user a bug fix or feature works
- Before opening a PR / before `git commit` on `app/`, `requirements.txt`, or `Dockerfile` changes (the pre-commit hook does a subset of this automatically — this skill is the full version, including automated tests)
- When asked to verify a specific branch, tag, or commit rather than the current working tree

## The flow

Run these from the repo root (any worktree). Each step assumes the previous one succeeded.

### 1. Run the automated suite first

This is independent of Docker/`dev.sh` — it spins its own `uvicorn` on a random port via `tests/conftest.py`. Do this first because it's fast and catches most regressions before spending time on a container.

```bash
uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/ -q
```

Stop here and fix if anything fails — don't spin up a dev container against code you already know is broken.

### 2. Spin up a dev environment

```bash
./dev.sh up
```

No arguments defaults to the current branch name (worktree working tree — includes uncommitted changes). Useful variants:

```bash
./dev.sh up review-42 --ref origin/feature/foo   # deterministic: builds from git archive, ignores the working tree
./dev.sh up my-check                              # explicit name
```

- Omitting `--ref` builds from whatever's on disk right now (including uncommitted changes) — use this to check work-in-progress.
- `--ref <branch|tag|SHA>` builds from `git archive` at that commit — use this to verify an exact commit reproducibly, e.g. before approving a PR, with no dependency on any worktree's state.
- Re-running `up` for a name that already exists rebuilds in place — the URL (`https://dev-<name>.<tailnet>.ts.net`) never changes as long as the name doesn't change.

The command prints one URL when it succeeds:

- `https://dev-<name>.<tailnet>.ts.net` — reachable from any tailnet device (iPad included)

Each environment is its own tailnet node (`dev-<name>`), so no port allocation or gateway is involved. The tailnet domain comes from `DEV_TS_DOMAIN` or the `.env` `BASE_URL`.

If it prints a `❌`, read the message — it already tells you the fix (missing `TS_AUTHKEY`, Docker not running, domain not derivable, etc.). Don't retry blindly.

### 3. Smoke-check the running container

Automated tests exercise the app logic; this step exercises the actual image you just built, over HTTP, the way a user would hit it.

```bash
URL="$(./dev.sh url <name>)"   # omit <name> to use the current branch's environment
curl -sf "$URL/api/health"       # expect {"status":"ok"}
curl -sf "$URL/api/folders" | head -c 200   # confirm the downloads tree is actually readable
```

For anything UI-facing (layout, page-turn behavior, spread sizing — see CLAUDE.md's viewer conventions), open `$URL` in a browser and drive the actual feature. `BOOKQ_TEST_MODE=true` means no login step. Don't claim a UI change works from `curl` output alone.

If you need another agent or the user to eyeball it, hand them the `https://dev-<name>...` URL — it's reachable from their devices too, not just this host.

### 4. Tear down

Always clean up what you spun up, even if verification failed — a leaked environment leaves a running container and a registered tailnet node.

```bash
./dev.sh down <name>
```

`down` keeps volumes (like production's `down`), so a re-`up` always returns to the same name and auth state. Plain `down` never deletes the state volume; for a full teardown use `./dev.sh down <name> --full` (removes volumes, images, and, when `TS_API_KEY` is set, the tailnet node registration). Don't delete the state volume by hand (`docker compose -p bookq-dev-<name> down -v`): it holds the tailnet node identity, and a later `up` with the same name would be renamed to `dev-<name>-1` and fail URL verification.

Check what's still running before ending a session, in case an earlier `up` in this conversation was never torn down:

```bash
./dev.sh list
```

Don't run `./dev.sh down --all` unless you've confirmed via `./dev.sh list` that every listed environment is yours to remove — other agents may have their own running concurrently. Prefer `./dev.sh down <name>` for a single environment you own; use `down --all` only after reviewing the `list` output.

## Failure triage

| Symptom | Cause | Fix |
|---|---|---|
| `up` fails: `TS_AUTHKEY が見つかりません` | No auth key available for the sidecar's first boot | Check `.env` in the main checkout, or export `TS_AUTHKEY` |
| `up` fails: `tailnet ドメインを導出できません` | `DEV_TS_DOMAIN` unset and no `BASE_URL` in `.env` | Set `DEV_TS_DOMAIN=<tailnet-domain>` or check `BASE_URL` in the main checkout's `.env` |
| `https://dev-<name>...` URL doesn't respond | Tailnet ACL blocking the node, or the sidecar failed to join | `./dev.sh list` to confirm the env is running; ACL fix is in `docs/deployment.md` |
| Health check passes but `/api/folders` is empty | Wrong `DOWNLOADS_HOST_DIR` or an unrelated empty fixture tree | Check what `DOWNLOADS_HOST_DIR` resolved to for this environment |
| URL verification fails with a different hostname | Node-name collision (another env holds `dev-<name>`) | `./dev.sh list` to find the holder, `down` the one you don't need, re-`up` |
| Environment won't go away / `list` shows stale entries | A previous crash mid-operation | `./dev.sh down <name>` still removes the containers; `./dev.sh list` shows what remains so you can `down` the stale ones |

## Related

- `docs/deployment.md` — full design rationale (why no state file, why each environment has its own node, the parallel-agent guarantees)
- `CLAUDE.md` — viewer-specific behavior worth knowing before you smoke-test the UI (RTL reading direction, spread pairing, spacer pages)
- `.githooks/pre-commit` — runs a lighter version of steps 2–3 automatically on relevant commits; this skill is what to reach for deliberately, e.g. before a PR or when asked to verify a specific ref