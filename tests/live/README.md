# Live-library regression (PR-review time, #31)

Runs the app against a REAL library without recording anything about it:
folders are discovered at runtime via `/api/folders` and addressed only by
sha1 hash ID, so no real title/folder name can leak into code, logs, or
reports. Collected only when `BOOKQ_LIVE_ROOT` is set; plain `pytest tests/`
is unaffected.

```bash
BOOKQ_LIVE_ROOT=/path/to/library ./scripts/live-regress.sh
```

Env knobs: `BOOKQ_LIVE_TOP` (default 6, biggest leaves), `BOOKQ_LIVE_RANDOM`
(default 6), `BOOKQ_LIVE_SEED` (default 7, deterministic sample).

## Safety

- The app never writes into the library (thumb/warmup caches live outside
  `DOWNLOADS_DIR`; audit: `app/thumbs.py::cache_root`,
  `app/warmup_cache.py::BOOKQ_CACHE_DIR`). The suite additionally sandboxes
  both cache dirs to tmp and asserts a file count+bytes census of the live
  root is identical before/after the session.
- `BOOKQ_TEST_MODE=true` is forced (auth bypass, same as the normal suite).

## Anti-leak rules (enforced by test_live_hygiene.py)

- No real folder/file names as literals in this directory or the wrapper
  script (examples use `sample_*` placeholders only).
- Test IDs, log lines, and assertion messages carry hash IDs, counts, and
  statuses — never raw paths.
- No screenshots of live content (copyrighted material must not enter the
  repo, even gitignored).
- Reports go to stdout or `.omo/evidence/` (gitignored), never committed.
