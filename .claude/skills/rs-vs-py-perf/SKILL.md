# rs-vs-py-perf — Python vs viewer-rs side-by-side performance comparison

Compare the FastAPI backend and the viewer-rs gateway on the **same commit and same live data**, across indexing, tile view, and viewer read paths. Produces a `py | rs` table of p50 latencies plus a `measure.py` display-complete baseline for each backend.

**Never substitute this for touching production.** Both environments are `--local` dev envs (`http://127.0.0.1:<port>`). Never point any measurement at the production URL, never run `./prd.sh`, never `docker compose up` the production project. Prod is only ever *inspected read-only* to discover the live-data mount.

## When to use this

- When asked to compare py vs rs backend performance / validate that native endpoints beat Python (「pyとrsで計測して」「native が速いか見て」)
- After changing a native hot path in `rust/viewer-rs` or `rust/bookq-core`, before/after comparison
- For everything else use `perf-measurement` (single-env display-complete)

## The flow

Run everything **serially** — the py dev server is single-worker, and parallel runs contaminate timings. All commands from any worktree root.

### 1. Discover the live-data source (read-only)

```bash
docker inspect bookq-prd-viewer --format '{{range .Mounts}}{{if eq .Destination "/app/downloads"}}{{.Source}}{{end}}{{end}}'
```

ps/inspect only — never exec/restart/stop prod, never read `.env`.

### 2. Boot both environments at the same ref

```bash
DOWNLOADS_HOST_DIR=<source> ./dev.sh up perf-py --ref <REF> --local --port 8081
DOWNLOADS_HOST_DIR=<source> ./dev.sh up perf-rs --ref <REF> --rs --local --port 8099
```

- `<REF>`: `origin/main` for a mainline comparison, or the branch under test.
- **Check ports first**: `docker ps --format '{{.Names}} {{.Ports}}'` — dev-01 usually holds 8080. Pick free ones.
- The `--rs` env runs viewer-rs in front with proxy fall-through to a co-located Python viewer. `BOOKQ_TEST_MODE=true` (dev default) makes `Native::from_env` enable the **entire** native surface — `BOOKQ_RS_NATIVE` need not be set.
- Slot pool is 10; check `./dev.sh list` first and never evict others' envs.

### 3. Verify data + which side answers (STOP if empty)

```bash
for p in 8081 8099; do curl -s "http://127.0.0.1:$p/api/folders?depth=1" | python3 -c "import sys,json;print(len(json.load(sys.stdin)))"; done
docker inspect bookq-dev-viewer-rs-dev-NN --format '{{range .Mounts}}{{if eq .Destination "/app/downloads"}}{{.Source}} {{.Mode}}{{end}}{{end}}'
```

- Both counts must match and be > 0; mount Mode must be `ro`.
- **Native vs proxy discrimination**: `curl -s -o /dev/null -D- "$URL/image/<file>"` — native axum sends **no `server:` header**; proxied responses carry `server: hypercorn-h11`. HEAD always proxies (method gate) — use GET only.
- **Wait for the index before measuring**: poll `GET /api/v1/tree` until `200` — `202` + 36-byte body means the index is still building (215k-image library: ~40–60 s on both backends). `?since=` while building returns `422`.

### 4. Measure

Use the throwaway driver (never under `scripts/`):

```bash
python3 .claude/skills/rs-vs-py-perf/references/perf_compare.py http://127.0.0.1:8081 [container-to-restart]
python3 .claude/skills/rs-vs-py-perf/references/perf_compare.py http://127.0.0.1:8099 [container-to-restart]
```

- Optional 2nd arg `docker restart`s the viewer container first — **this is what produces the cold index-build number** (`bookq-dev-viewer-dev-NN` for py env, `bookq-dev-viewer-rs-dev-NN` for rs env). The `== indexing ==` section then reports healthy→200 wall time; without it, the env's index is already warm and only a reminder prints.
- The script auto-discovers a large image folder and an archive from `/api/v1/tree`, then measures: cold first-hits, warm API p50s (`v1/tree`, v0 `folders?depth=1`, `?since=<cur-gen>`, `v1/images` dir + archive), sequential `/image` reads (loose + archive entries), and a 16-parallel wall-clock probe.
- Then `measure.py` for end-to-end tile view, 3 runs each, same as `perf-measurement`:

```bash
ONLY=A uv run --with playwright --with-requirements requirements-dev.txt \
  python scripts/perf/measure.py http://127.0.0.1:<port> --runs 3 --out .omo/evidence/<run>/<env>
```

### 5. Report + tear down

Report a `metric | py | rs` table (cold, warm p50s, sequential reads, parallel wall, measure.py medians), plus *native-vs-proxy confirmation*. Note proxy-hop tax on non-native routes (~2–8 ms, e.g. v0 `/api/folders`, `/`) as a structural constant, not a regression.

Tear down only the envs you created: `./dev.sh down perf-py && ./dev.sh down perf-rs` — or leave running and tell the user, if they may want to browse. Never `--all`.

## Pitfalls (learned the hard way)

- **`?since=` needs the CURRENT generation** — fetch `generation` from a `200` tree response first; a stale gen returns the full 12 MB tree, not the stale-poll envelope. `urllib` raises `HTTPError` on `304` — catch it and count as success.
- **Never print live folder/archive names** — tests/live rule: names stay in the script's internals; report only counts, sizes, ms.
- `urllib` sends `Accept-Encoding: identity` — gzip cost is **not** measured unless you add the header explicitly; real browsers always accept gzip, so per-request `12 MB → gzip` is a separate (larger) number.
- Fairness: both envs have fresh `thumb-cache` volumes → equally cold; restart the container for cold-index numbers rather than trusting boot order.
- rs `dev` image is `--release` — slowness you measure is real, not a debug-build artifact.
- `BOOKQ_RS_NATIVE` unset in test mode is fine (all-native); to pin a subset export `DEV_RS_NATIVE=<csv>` at `up` time.

## Known structural results (2026-11, library ≈215k imgs / external volume)

Hot paths after #258 hot-path fixes: rs native `/image` 1.4 ms, dir-1599 listing 1.4 ms (cached), `?since` 0.7 ms — all beat py. Remaining gaps: 12 MB `v1/tree` body send (~2×), proxy hop on v0/non-native routes, rar entry reads (proxied by design — the library's majority archive type).
