"""Top-page display-complete measurement harness.

Plan: `.omo/plans/top-page-display-speed.md` Todo 1 (計測ハーネス固定).

Measures the time from navigation to `/` until every top-page tile image
(`#tile-grid .tile[data-path] img` ONLY) has settled (`img.complete`,
which covers both `load` and `error`), then prints numeric logs for
`pytest -s` capture:

- display-complete ms (from `window.__cc_markTopStart`)
- tile image count
- `/api/folders` duration ms
- `/image/*` request count + transfer bytes

Visibility checks use `getBoundingClientRect` (no `IntersectionObserver`).

Later plan todos (4/5) will ADD asserts to this file — the `_wait_*` /
`_collect_*` helpers are kept small and reusable for that.

BOOKQ_TEST_MODE handling: none needed here — the shared `server`
fixture (tests/conftest.py) already launches uvicorn with
`BOOKQ_TEST_MODE=true` + a synthetic DOWNLOADS_DIR tree.

Run:
    uv run --with pytest --with playwright --with-requirements requirements-dev.txt \\
        pytest tests/test_top_display_perf.py -q -s
"""

# Scope is EXACTLY this selector — do not broaden (e.g. no bare `.tile`).
TILE_IMG_SELECTOR = "#tile-grid .tile[data-path] img"

TIMEOUT_MS = 15_000


def _mark_top_start(page) -> None:
    """Stamp navigation time immediately after `goto` (display-complete base)."""
    page.evaluate("window.__cc_markTopStart = performance.now()")


def _wait_for_tiles(page, timeout_ms: int = TIMEOUT_MS) -> None:
    page.wait_for_selector("#tile-grid .tile", timeout=timeout_ms)


def _wait_for_all_tile_images(page, timeout_ms: int = TIMEOUT_MS) -> None:
    """Wait until every scoped img is settled.

    `img.complete` becomes True on both `load` and `error`, so polling it
    covers the `complete || load/error` requirement without listeners.
    """
    page.wait_for_function(
        "(sel) => {"
        " const imgs = Array.from(document.querySelectorAll(sel));"
        " return imgs.length > 0 && imgs.every((img) => img.complete);"
        " }",
        arg=TILE_IMG_SELECTOR,
        timeout=timeout_ms,
    )


def _visible_tile_image_count(page) -> int:
    """Count scoped imgs intersecting the viewport via getBoundingClientRect."""
    return page.evaluate(
        "(sel) => {"
        " const vh = window.innerHeight || document.documentElement.clientHeight;"
        " const vw = window.innerWidth || document.documentElement.clientWidth;"
        " return Array.from(document.querySelectorAll(sel)).filter((img) => {"
        " const r = img.getBoundingClientRect();"
        " return r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw;"
        " }).length;"
        " }",
        TILE_IMG_SELECTOR,
    )


def _collect_perf_stats(page) -> dict:
    """Gather display-complete timing + resource stats in one evaluate."""
    return page.evaluate(
        """(sel) => {
            const entries = performance.getEntriesByType("resource");
            const folders = entries.filter((e) => e.name.includes("/api/v1/tree"));
            const images = entries.filter((e) => e.name.includes("/image/"));
            const imgBytes = images.reduce((s, e) => s + (e.transferSize || 0), 0);
            const markStart = window.__cc_markTopStart || null;
            const now = performance.now();
            return {
                display_complete_ms: markStart !== null ? now - markStart : null,
                image_count: document.querySelectorAll(sel).length,
                api_folders_duration_ms: folders.length > 0 ? folders[0].duration : null,
                api_folders_count: folders.length,
                image_requests: images.length,
                image_transfer_bytes: imgBytes,
            };
        }""",
        TILE_IMG_SELECTOR,
    )


def _log_perf_stats(stats: dict, visible_count: int) -> None:
    print(f"[top-display-perf] display_complete_ms={stats['display_complete_ms']:.1f}")
    print(f"[top-display-perf] tile_img_count={stats['image_count']}")
    print(f"[top-display-perf] tile_img_visible_count={visible_count}")
    print(
        "[top-display-perf] api_folders_duration_ms="
        f"{stats['api_folders_duration_ms']:.1f}"
        f" (count={stats['api_folders_count']})"
    )
    print(
        "[top-display-perf] image_requests="
        f"{stats['image_requests']} image_transfer_bytes={stats['image_transfer_bytes']}"
    )


def test_top_display_complete(server, browser):
    """Harness: open `/`, wait for all tile imgs, print numeric logs."""
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    try:
        page.goto(server + "/")
        _mark_top_start(page)
        _wait_for_tiles(page)
        _wait_for_all_tile_images(page)
        stats = _collect_perf_stats(page)
        visible = _visible_tile_image_count(page)
        _log_perf_stats(stats, visible)
        assert stats["display_complete_ms"] is not None
        assert stats["image_count"] > 0
    finally:
        context.close()


# Todo 4 (フロント切替): every browse-list cover must load via
# `/thumb/...?w=` with `loading="lazy"`. Same scope as the harness
# (TILE_IMG_SELECTOR); the zero-eager check spans `#tile-grid` per spec.
def _tile_img_srcs(page) -> list:
    """As-authored `src` of every scoped tile img (getAttribute, not .src)."""
    return page.evaluate(
        "(sel) => Array.from(document.querySelectorAll(sel)).map("
        "(img) => img.getAttribute('src') || '')",
        TILE_IMG_SELECTOR,
    )


def _non_lazy_tile_img_count(page) -> int:
    """Count of `#tile-grid` imgs without a `loading` attribute (eager)."""
    return page.evaluate(
        "document.querySelectorAll(\"#tile-grid img:not([loading])\").length"
    )


# Todo 5 (初回フェッチ順): first paint must fetch shallow folders
# (`/api/folders?...depth=1`) BEFORE the first `/thumb` request, so visible
# covers start from the cheap shallow snapshot. Order comes from
# `performance.getEntriesByType("resource")` start times.
def _resource_start_times(page) -> list:
    """(name, startTime) of every performed resource request."""
    return page.evaluate(
        "() => performance.getEntriesByType('resource').map("
        "(e) => ({name: e.name, start: e.startTime}))"
    )


def test_top_covers_use_thumbnails(server, browser):
    """Todo 4: all tile covers via `/thumb/`, zero eager imgs."""
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    try:
        page.goto(server + "/")
        _mark_top_start(page)
        _wait_for_tiles(page)
        _wait_for_all_tile_images(page)
        srcs = _tile_img_srcs(page)
        print(f"[top-display-perf] thumb_src_sample={srcs[:3]}")
        assert len(srcs) > 0
        # /api/thumb-sprite is the composed variant of the same thumbnail
        # pipeline (multi-thumb tiles render one sheet img, not 9 /thumb/ imgs).
        non_thumb = [
            s
            for s in srcs
            if not (s.startswith("/thumb/") or s.startswith("/api/v1/thumb-sprite"))
        ]
        print(f"[top-display-perf] non_thumb_src_count={len(non_thumb)}")
        assert non_thumb == []
        non_lazy = _non_lazy_tile_img_count(page)
        print(f"[top-display-perf] non_lazy_img_count={non_lazy}")
        assert non_lazy == 0
    finally:
        context.close()
