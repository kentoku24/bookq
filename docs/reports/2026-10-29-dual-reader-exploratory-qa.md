# Dual Reader / Two-Device Spread — Exploratory QA Report

- Date: 2026-10-29
- Base: `main` @ `e1a6a4c` (clean worktree; no production code modified)
- Environment: `BOOKQ_TEST_MODE` + uvicorn @ `127.0.0.1:8765`, `DOWNLOADS_DIR=/tmp/bookq-qa/downloads` (synthetic page-marker PNG tree)
- Drivers: Playwright Chromium, 2–4 isolated browser contexts as devices (mobile landscape 844×390 dsf2, portrait 390×844, desktop 900×600). Real UI input (touch taps/swipes, keyboard, DOM buttons), route-level network fault injection.
- Harness: `/tmp/bookq-qa/qa/*.py` (reuses the `scripts/reader-sync/` marker formula; evidence in `/tmp/bookq-qa/evidence/`)
- Transport observed: `webrtc-dc` data channel established between contexts (not just server-ws) — both paths exercised.

## 1. Summary

Roughly half a day of continuous exploratory testing. The synchronized-reading core is in good shape: **17 works × all spreads verified pixel+DOM correct**, rapid/conflicting input converges deterministically, offline/reload/re-pair lifecycles recover cleanly, ambiguous MATCH picker, QR/deep-link join, and manual code entry all work, and dialog a11y (role/aria-modal/Escape/focus) is present.

**Confirmed defects: 1** (DUAL-UI-001, stale blank cell after SWAP_SIDES — user sees blank page instead of the page image). No P0 issues found. Several noteworthy spec/UX observations recorded below.

## 2. Defects

### [DUAL-UI-001] Stale `.pair-blank` cell masks the real page after SWAP_SIDES

- 分類: 表示 / 状態同期(DOM)
- 重大度: **P1** — primary reading surface shows a blank page where content should be; persists until the spread is re-rendered by distance-eviction; affects either device
- 対象端末: whichever device transitions **blank → page** (reproduced on both leader and follower)
- 前提条件: paired, reading a spread where this device renders a blank side (odd page count trailing page, or offset=1 opening spread)
- 再現手順:
  1. Pair two devices; open `work_odd` (5 pages); navigate to last spread (one device blank, other `p005`)
  2. Invoke 左右交換 (`PairController.swapSides()` / `#pair-swap`)
  3. Internal `pv.ref` flips correctly (`None → p005`), `renderSet` updated — but the device keeps the old `.page-cell.pair-blank` inside the spread element; the newly placed image cell overflows outside the spread box and is clipped → **user still sees a blank page**
- 再現性: 3/3 variants (odd trailing spread, offset1 opening spread, double-swap)
- 発生タイミング: 操作直後・安定後も持続
- ユーザーへの影響: after swapping sides at any blank-containing spread, one reader sees an empty page; a turn away+back does NOT fix it (the stale cell is keyed to the spread, re-shown every visit)
- 証拠: `/tmp/bookq-qa/evidence/s52-*` screenshots; DOM dumps show `.page-cell.pair-blank` + real `.page-cell` coexisting in one `.spread`
- 既存テストとの関係: 未カバー (runner asserts spreadId/ref state, not per-cell DOM occupancy)
- 原因 (confirmed by code inspection): `pairBlankCells` is keyed by **spread number** (`app/static/app.js:766`). `ensureBlankCell` (L2448–2460) removes non-blank siblings when *creating* a blank (page→blank direction is clean), and `purgeNonRenderedCells` (L2613–2620) evicts blank cells only by spread *distance*. **No path removes `pairBlankCells[s]` when the device starts rendering a page for the same spread `s`** (image cells ARE culled on swap via `renderSet` at L2602 — the blank-cell side was missed).
- 修正の方向性: when placing an image cell into spread el `s` (or in the purge loop, keyed on `renderSet` membership per side), remove `.pair-blank` children and `delete pairBlankCells[s]` — symmetric to L2456. One-line-class fix; also clear `el.classList` `pair-blank` marker.

### Observations (spec-consistent, worth product decision)

- **[OBS-1] Peer-away pause blocks the remaining reader for up to 5 min.** Closing a partner's tab leaves the survivor in `peer-away`/`paused`; `gate_page_turn` (`state.py:187–199`) rejects all TURN/GOTO with `peer_offline` until RESUME_WINDOW (300s) elapses. Working-as-specified, but a solo reader cannot continue or expedite session end from the reader UI — consider a "continue alone" affordance. Verified: tab close → `peer-away`+`paused` within ~3s; taps rejected.
- **[OBS-2] Resume credentials are tab-scoped (sessionStorage).** Reload in the same tab resumes; opening a new tab does not (goes `idle`). Matches §3.5 spec; on real devices an accidental tab close = session loss + manual re-pair. Acceptable but worth noting for UX copy.
- **[OBS-3] Reading-time pair entry discards book context by design.** Opening the sidebar runs `returnToBrowse()` which clears `currentFolder`, so `_maybeBootstrapWork` never fires from the real UI path; pairing always lands in `selecting`. Consistent, but the bootstrap branch is effectively dead code from available UI.

## 3. Left/right display consistency

Method: deterministic colored PNG pages + corner markers; per device, the *displayed* cell under the viewport was identified via DOM (`elementFromPoint` + cell rect) and compared to `PairedView.ref` — not just counters.

- **17/17 works, every spread, both devices: correct.** Covers 1–4 page works, odd/even counts, archives (cbz), single-child flattened chains, deep nesting, Unicode folder names (夏 の 本), sibling-chapter concatenated works, 60-page book.
- Offset 0 and offset 1 plans correct, including blank-side placement and odd trailing pages.
- Multi-chapter (`lib/ser` v1+v2+v3): taps cross chapter boundaries, blank chapter-end cells correct.
- Failure cases: only DUAL-UI-001 (DOM masking — internal state was still correct).

## 4. Coverage

- **Pairing**: MATCH create/join; ambiguous MATCH (2 waiting leaders via offline-gap) → candidate picker ("Test User — N秒前") → pick → connect; QR canvas rendered+painted; `#/join/<code>` deep link consumed + hash cleaned; manual 6-digit entry incl. leader-join swap path; leave → clean re-pair; leader waiting view.
- **Input**: taps (both zones, both devices), synthetic single-finger swipes (forward/back/edge-clamped, chapter-limited), rapid ×10 bursts leader/follower/alternating, simultaneous conflicting turns, direction flapping, offset toggle during burst, double-tap, keyboard arrows, page slider goto, tiny-viewport taps.
- **Async**: 600ms delayed image responses under 250ms tap bursts → converged, no stale overwrite (index-identity guards hold); image fetch abort on non-displayed page benign.
- **Lifecycle**: one-device offline → `peer-away`/`paused` + `reconnecting` → heal converges; turn during peer reload converges; simultaneous reloads both resume; peer tab-close → `peer-away` (OBS-1); new-tab = no resume (OBS-2); background/foreground (`document.hidden`) + turns during hidden → converges.
- **Visual**: landscape, portrait, rotation mid-read, 320×568 — screenshots taken; layouts sound. Pair overlay: `role=dialog`, `aria-modal`, Escape dismiss, Tab focus inside, gesture isolation.
- **A11y**: dialog semantics verified; inert not set on background but overlay swallows events (verified).

## 5. Unverified / constraints

- Real-device-only: actual iOS Safari/Android Chrome finger physics, bezel palm rejection, WebRTC over real networks, device sleep/wake, browser back/forward buttons mid-pair.
- The 300s resume-window expiry and `resume_in_use` reaping verified by code path only, not timed E2E.
- Image failure on the *currently displayed* page (abort landed on the non-rendered side); offline-cache (SW CacheFirst) interaction with pairing untested.
- `UI_SYNC` browse mirroring observed in code; live scroll-mirroring not E2E-verified.
- Pinch-zoom while paired: API present, no synthetic pinch driver available (untested).
- Harness artifacts (not product bugs): an earlier "swipe advances 2 spreads" was my helper dispatching two gestures — re-verified single gesture = 1 turn (`via: swipe-ready`).

## 6. Fix priorities

1. **DUAL-UI-001 (P1)** — small, well-localized DOM fix; add a swap-then-screenshot assertion to `scripts/reader-sync/` scenario set.
2. OBS-1 product call — "continue alone / end session" during peer-away pause.
3. Consider logging/asserting blank-cell occupancy in the verification runner to prevent regression.

## Evidence

Screenshots: `/tmp/bookq-qa/evidence/` (s01–s60 runs). Key: `s52-v1-post-*.png` (DUAL-UI-001), `s45-amb-picker` (picker), `s60-img-fail-B`, `s50-tiny-A`, `s54-after-peer-close`. Scripts: `/tmp/bookq-qa/qa/`. Nothing in the repo was modified.
