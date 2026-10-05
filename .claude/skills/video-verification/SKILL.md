---
name: video-verification
description: Use when verifying browser UI behavior via video — page turns, spreads, animations, black-screen gaps, or any timing-dependent glitch. Trigger on "動画で確認", "動画でデバッグ", "video verification", "ブラウザ操作を録画", "UIの動きを見る", or when a bug is invisible in static screenshots.
---

# Video Verification

Capture browser operations as video, extract frames, and let a vision-capable model describe what happened — for bugs static screenshots cannot catch.

## Overview

Playwright records `*.webm` → `ffmpeg fps=2` extracts PNGs → any multi-image vision model (`-f frame_*.png`) reasons over the sequence. Default is `opencode-go/muse-spark-1.2-contributor`; any model with image attachment works (free tier `-free` also valid). ADR 0001 explains why native `.mp4` ingestion is not used.

## When to Use

- Page turn / spread animation, gap flicker, toolbar slide, offline toast
- Black-screen → image latency, loading skeletons, cache transitions
- Any bug where "before/after screenshots look fine but motion is wrong"

Not for: pure layout/markup checks (use single screenshot), API-only changes.

## Quick Reference

| Task | Command |
|---|---|
| Capture bookq op | `uv run python scripts/video_debug.py capture --url <URL> --scenario browse_then_open --out /tmp/cap.webm` |
| Frames only | `uv run python scripts/video_debug.py frames /tmp/cap.webm --fps 2 --out /tmp/frames` |
| Diagnose (default model) | `uv run python scripts/video_debug.py diagnose /tmp/cap.webm --fps 2 --prompt "ページめくりで隙間が出るか"` |
| Diagnose (free / alt model) | `uv run python scripts/video_debug.py diagnose /tmp/cap.webm --model opencode/muse-spark-1.2-contributor-free --fps 2` |
| Diagnose (ox-alpha free) | `uv run python scripts/video_debug.py diagnose /tmp/cap.webm --model opencode-go/ox-alpha-free --fps 2` |
| Record all tests | `RECORD_VIDEO=1 uv run --with pytest --with playwright --with-requirements requirements.txt pytest tests/ -q` → `/tmp/bookq-videos/*.webm` |

Scenarios: `browse_then_open` (open first tile + 2 page turns), `scroll_only`.

## Implementation

### 1. Capture

`scripts/video_debug.py capture` uses `playwright.sync_api` with `recordVideo` (390×844 portrait). For dev envs use `https://dev-<name>.<your-tailnet>.ts.net` (see `docs/deployment.md`); for local use `http://127.0.0.1:<port>` from `BOOKQ_TEST_MODE=true` server.

### 2. Extract

`ffmpeg -i video.webm -vf fps=2 frame_%03d.png` — 2fps is default (8s → 16 frames). Raise to 5fps only for sub-second glitches. `fps` is capped by source fps (25-30 for Playwright). Warning at >12 frames.

### 3. Diagnose

`scripts/video_debug.py diagnose` calls `opencode run -m <model> -f frame_*.png --format json` (default: `opencode-go/muse-spark-1.2-contributor`). Any vision model with image attachment works — Spark, Sonnet, Gemini, ox-alpha, etc. Override via skill argument or CLI:

```bash
# via CLI
uv run python scripts/video_debug.py diagnose /tmp/cap.webm --model opencode-go/ox-alpha-free
uv run python scripts/video_debug.py diagnose /tmp/cap.webm --model opencode/muse-spark-1.2-contributor-free

# via skill: "video-verificationでモデルはox-alpha-freeで"
```

Model context is typically 200k-1M tokens; ~2-3k per image. Direct `.mp4` via `-f` is blocked (`Cannot read binary file`) — always use frames.

Alternative manual path: `look_at(file_paths=[...])` with extracted PNGs.

## Common Mistakes

- Passing `.mp4` directly to `opencode run -f` → fails; extract frames first.
- `fps=30` on a 10s video → 300 frames, context overflow and slow. Keep 2-3fps, max 5fps for short clips.
- Forgetting `RECORD_VIDEO_DIR` override when parallel agents collide on `/tmp/bookq-videos` → set per-agent dir.
- Using `command-code` provider via raw curl → Go plan blocks; use `opencode` provider via `opencode run`.

## References

- `scripts/video_debug.py` — harness
- `tests/conftest.py` — `RECORD_VIDEO` toggle
- `docs/adr/0001-video-debugging-via-frame-extraction.md` — Phase 0 evidence
- `CLAUDE.md` viewer conventions (spreadMetrics, anchorSpread, rebaseAnchor) — primary debug targets
