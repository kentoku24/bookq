"""Shared fixtures: a synthetic downloads/ tree, a live FastAPI server (test mode),
and Playwright browser contexts (portrait / landscape).

Run:
    uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/ -q
(First time: uv run --with playwright playwright install chromium)
"""

import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request
import zlib
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(Path(__file__).parent))


def pytest_addoption(parser):
    try:
        parser.addoption("--browser", action="store", default=None, help="browser: chromium or webkit (alias for BROWSER env)")
    except Exception:
        pass


def pytest_configure(config):
    try:
        b = config.getoption("--browser", default=None)
        if b == "webkit":
            os.environ["BROWSER"] = "webkit"
    except Exception:
        pass


def _make_png(path: Path, w: int = 40, h: int = 60, rgb: tuple = (200, 60, 60)) -> None:
    """Write a minimal solid-color RGB PNG without external deps."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(rgb) * w
    idat = zlib.compress(row * h)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
    )


@pytest.fixture(scope="session")
def downloads_tree(tmp_path_factory) -> Path:
    """Build the fixture folder layout the tests rely on.

    Note: thumb_grid_* (9 folders for multi-thumb grid 2..9) are NOT generated
    here — they live in tests/fixtures/uiux_tree.py and dev_fixture/ (on-demand
    via `dev.sh up --fixture`). Keeping conftest at 14 folders avoids bumping
    every `expect(...).to_have_text("14 folders")` to 23. The scanner thumbs
    cap-at-9 is covered by tests/test_scanner.py::test_scan_tree_thumbs_cap_*
    instead.

    root/
      simple_manga/          1.png 2.png 3.png 10.png 11.png   (natural sort)
      long/                  p001.png .. p030.png              (prefetch/evict, spread)
      series/vol1/           p1.png p2.png                     (drill-down, multi-thumb)
      series/vol2/           p1.png
      group/deep_series/vol1/p1.png p2.png                     (depth-2 chapter concat)
      group/deep_series/vol2/p1.png
      group/other_work/       1.png                             (prevents group flattening)
      group/episodes/ch-1..11/p1.png                            (natural-sort episodes: ch-2 < ch-10)
      group/fullwidth_episodes/第10話,第２話/p1.png              (server/client natural-sort parity)
      wrap/inner/vol1/       p1.png p2.png                     (flattened wrapper, still depth-2 raw path)
      wrap/inner/vol2/       p1.png
      odd_wrap/inner/vol1/   p1.png                            (odd count → vol2 needs a pad)
      odd_wrap/inner/vol2/   p1.png p2.png
      chain/only/leaf/       1.png 2.png                       (single-child flattening)
      mixed/                 cover.png + extra/1.png           (images + subfolder)
      empty_folder/                                            (no children)
      夏 の 本/               1.png                             (unicode + spaces)
      mixed_ar/              6 pages alternating 40x60 / 90x60 (per-page spread sizing)
      wide_spread/           4 pages incl. a 160x60 double-page (width-limited spread)
      tight_fit/             8 pages 120x170 — near-exact fit at 1180x820 (iPad Air)
      kanji/series1/【第一部】..【第五部】/p1.png      (kanji-numeral natural sort)
      kanji/series2/第二話 第十話 第十一話/p1.png     (kanji chapter order; series2 sibling
                            prevents kanji single-child flattening)
      zoom_series/series/ep-1..3/  5/4/7 pages                (zoom + overview, depth-2 chapters)
      huge/book/part1,part2/       350 + 351 pages            (overview ±200 img window)
      nest/keep/1.png                                          (prevents nest flattening)
      nest/wrap/inner/{aa,bb}/{x,y}/p1.png                     (browseable flattened node
                            "inner/aa" under nest/wrap — its _fullPath
                            "nest/wrap/inner/aa" has no live textual parent)
    """
    root = tmp_path_factory.mktemp("downloads")

    simple = root / "simple_manga"
    simple.mkdir()
    for name in ["1.png", "2.png", "3.png", "10.png", "11.png"]:
        _make_png(simple / name)

    long_dir = root / "long"
    long_dir.mkdir()
    for i in range(1, 31):
        _make_png(long_dir / f"p{i:03d}.png")

    for vol, pages in [("vol1", 2), ("vol2", 1)]:
        d = root / "series" / vol
        d.mkdir(parents=True)
        for i in range(1, pages + 1):
            _make_png(d / f"p{i}.png")

    # Chapters nested under a folder dedicated to one series (depth 2), unlike
    # "series" above which sits directly under root (depth 1) and is used only
    # for navigation/thumbnail tests. "other_work" is a second child of "group"
    # so it has 2+ subdirectories and "deep_series" survives scanner.py's
    # single-child flattening as its own node.
    for vol, pages in [("vol1", 2), ("vol2", 1)]:
        d = root / "group" / "deep_series" / vol
        d.mkdir(parents=True)
        for i in range(1, pages + 1):
            _make_png(d / f"p{i}.png")
    other_work = root / "group" / "other_work"
    other_work.mkdir(parents=True)
    _make_png(other_work / "1.png")

    # group/episodes — digit-count natural-sort case (ch-2 < ch-10), raw depth 2 so
    # cross-chapter concat also applies; "episodes" keeps group at 3 children so no
    # single-child flattening anywhere on this path.
    for ch in ["ch-1", "ch-2", "ch-10", "ch-11"]:
        d = root / "group" / "episodes" / ch
        d.mkdir(parents=True)
        _make_png(d / "p1.png")

    # group/fullwidth_episodes — cross-implementation regression fixture: the
    # server's natural_key() and the client's JS _naturalSortKey() both treat
    # full-width digits as opaque text (ASCII-only regex), so "第２話" ties
    # after "第10話" on raw text. Same case as test_natural_key_fullwidth_
    # digits_no_crash (server-side); exercised client-side via cross-chapter
    # concat in test_cross_chapter_concat_fullwidth_digit_order.
    for ch in ["第10話", "第２話"]:
        d = root / "group" / "fullwidth_episodes" / ch
        d.mkdir(parents=True)
        _make_png(d / "p1.png")

    # "wrap" has a single child ("inner"), so scanner.py flattens it away and
    # vol1/vol2 land at post-flatten tree depth 1 -- but their raw path is
    # still 2 levels deep. Regression coverage for depth being judged from the
    # raw path, not the (possibly flattened) tree.
    for vol, pages in [("vol1", 2), ("vol2", 1)]:
        d = root / "wrap" / "inner" / vol
        d.mkdir(parents=True)
        for i in range(1, pages + 1):
            _make_png(d / f"p{i}.png")

    # Same flattened-wrapper shape as "wrap" above (so it still concatenates
    # per SIBLING_AUTOLOAD_MIN_DEPTH), but vol1 has an odd page count -- the
    # concatenated cross-chapter sequence then needs a leading spacer before
    # vol2 to keep it starting on the correct parity (see CLAUDE.md "Chapters
    # are sibling leaf folders...").
    for vol, pages in [("vol1", 1), ("vol2", 2)]:
        d = root / "odd_wrap" / "inner" / vol
        d.mkdir(parents=True)
        for i in range(1, pages + 1):
            _make_png(d / f"p{i}.png")

    leaf = root / "chain" / "only" / "leaf"
    leaf.mkdir(parents=True)
    _make_png(leaf / "1.png")
    _make_png(leaf / "2.png")

    mixed = root / "mixed"
    (mixed / "extra").mkdir(parents=True)
    _make_png(mixed / "cover.png")
    _make_png(mixed / "extra" / "1.png")

    (root / "empty_folder").mkdir()

    jp = root / "夏 の 本"
    jp.mkdir()
    _make_png(jp / "1.png")

    # Alternating aspect ratios: each spread pairs a tall page with a wide one,
    # so a single global cell width cannot fit both without letterboxing.
    mixed_ar = root / "mixed_ar"
    mixed_ar.mkdir()
    for i in range(1, 7):
        tall = i % 2 == 1
        _make_png(mixed_ar / f"p{i}.png", w=40 if tall else 90, h=60)

    # 120x170 is the common manga scan ratio (0.7059). Two of them side by side
    # nearly exactly fill an iPad Air landscape viewport (1180x820), leaving
    # ~8.7px per side — the tight fit where a fixed outer gap would hide the
    # neighbouring pages.
    tight = root / "tight_fit"
    tight.mkdir()
    for i in range(1, 9):
        _make_png(tight / f"p{i}.png", w=120, h=170)

    # p3 is a double-page scan wider than two normal pages — forces the
    # width constraint to bind instead of the height constraint.
    wide = root / "wide_spread"
    wide.mkdir()
    for i, (w, h) in enumerate([(40, 60), (40, 60), (160, 60), (40, 60)], start=1):
        _make_png(wide / f"p{i}.png", w=w, h=h)

    # Zoom / overview fixtures (tests/test_viewer_zoom.py, test_viewer_overview.py).
    # Chapters sit at depth 2 (zoom_series/series/ep-N) so sibling concatenation
    # kicks in (SIBLING_AUTOLOAD_MIN_DEPTH = 2). Page counts 5/4/7 exercise odd
    # trailing pages and the __spacer__ at each seam.
    for ep, pages in [("ep-1", 5), ("ep-2", 4), ("ep-3", 7)]:
        d = root / "zoom_series" / "series" / ep
        d.mkdir(parents=True)
        for i in range(1, pages + 1):
            _make_png(d / f"p{i:02d}.png")
    # 701 pages across two chapters: exercises the ±200 <img> window of the overview.
    for part, pages in [("part1", 350), ("part2", 351)]:
        d = root / "huge" / "book" / part
        d.mkdir(parents=True)
        for i in range(1, pages + 1):
            _make_png(d / f"p{i:03d}.png")

    # Browseable flattened node: wrap→inner is a single-child chain, so inner's
    # children hoist into "wrap" with names "inner/aa"/"inner/bb" — each still a
    # folder with subfolders, so the tile navigates (not straight to viewer).
    # Its _fullPath "nest/wrap/inner/aa" has no live textual parent: splitting on
    # "/" yields the dead "nest/wrap/inner"; the real tree parent is "nest/wrap".
    # Regression fixture for swipe-back / breadcrumb parent resolution.
    keep = root / "nest" / "keep"
    keep.mkdir(parents=True)
    _make_png(keep / "1.png")
    for grp in ["aa", "bb"]:
        for vol in ["x", "y"]:
            d = root / "nest" / "wrap" / "inner" / grp / vol
            d.mkdir(parents=True)
            _make_png(d / "p1.png")

    # kanji — kanji-numeral natural-sort case (第一部 < 第二部 < … < 第五部;
    # 第二話 < 第十話 < 第十一話). "series2" is a second child of "kanji" so
    # kanji survives scanner.py's single-child flattening and series1/series2
    # stay reachable as their own tiles.
    for part in ["【第一部】", "【第二部】", "【第三部】", "【第四部】", "【第五部】"]:
        d = root / "kanji" / "series1" / part
        d.mkdir(parents=True)
        _make_png(d / "p1.png")
    for ch in ["第二話", "第十話", "第十一話"]:
        d = root / "kanji" / "series2" / ch
        d.mkdir(parents=True)
        _make_png(d / "p1.png")

    return root


@pytest.fixture(scope="session")
def folder_count_text(downloads_tree: Path) -> str:
    # Expected "#folder-count" text at the browse root, derived from the same
    # fixture tree the server serves (not a hardcoded count), so extending the
    # fixture updates every assertion automatically.
    from app.scanner import scan_tree

    return f"{len(scan_tree(downloads_tree))} 件"


@pytest.fixture(scope="session")
def root_folder_names(downloads_tree: Path) -> list[str]:
    # Expected root-level tile order/names, derived from the same fixture tree
    # and the same natural-sort scan_tree uses server-side (not a hardcoded
    # list), so extending the fixture updates every assertion automatically.
    from app.scanner import scan_tree

    return [node["name"] for node in scan_tree(downloads_tree)]


@pytest.fixture(scope="session")
def server(downloads_tree: Path) -> str:
    """Start FastAPI app in test mode against the fixture tree; yield its base URL."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    env = os.environ.copy()
    env["BOOKQ_TEST_MODE"] = "true"
    env["DOWNLOADS_DIR"] = str(downloads_tree)

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Server exited early: {proc.stderr.read().decode()}")
        try:
            with urllib.request.urlopen(base_url + "/api/health", timeout=1):
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.terminate()
        raise RuntimeError("Server did not become healthy within 10s")

    yield base_url

    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture(autouse=True)
def _reset_reading_position(request):
    """Clear the session server's in-memory resume point before each test.

    `server` is session-scoped, so app.state.reading_positions survives across
    tests: a page turn's debounced PUT in one test would leak into the NEXT
    test's boot pull, merge into its fresh localStorage, and steer the opened
    page off "1 / N". Worse, the previous test's teardown (context.close →
    pagehide → keepalive PUT) can land AFTER the reset, so this polls until
    the slot reads stably empty rather than deleting once. ``?hard=1`` also
    drops the wipe tombstone, which would otherwise reject this test's
    fresh record (the tombstone legitimately outlives plain PUTs).

    Every UI fixture funnels through `server` (it's a transitive
    fixturename), so the check covers all browser tests while pure unit
    tests skip the HTTP call entirely. Autouse at function scope runs
    before the `page` fixture's goto — the reset always lands first.
    """
    if "server" not in request.fixturenames:
        return
    try:
        base_url = request.getfixturevalue("server")

        def _delete():
            req = urllib.request.Request(
                base_url + "/api/reading-position?hard=1", method="DELETE"
            )
            urllib.request.urlopen(req, timeout=5).read()

        def _empty():
            with urllib.request.urlopen(
                base_url + "/api/reading-position", timeout=5
            ) as resp:
                return json.loads(resp.read()).get("position") is None

        _delete()
        deadline = time.time() + 3.0
        empty_since = None
        while time.time() < deadline:
            if _empty():
                # Stably empty for ~300ms — a teardown keepalive PUT from
                # the previous test would have landed by now (it typically
                # travels well under 100ms), so returning here is safe.
                if empty_since is not None and time.time() - empty_since >= 0.3:
                    break
                if empty_since is None:
                    empty_since = time.time()
            else:
                empty_since = None
                _delete()
            time.sleep(0.05)
    except Exception:
        pass  # best-effort — a dead server fails the test properly later


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture(scope="session")
def webkit_browser():
    with sync_playwright() as p:
        browser = p.webkit.launch(headless=True)
        yield browser
        browser.close()


IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
IPAD_UA = (
    "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)


def _new_page(
    browser,
    server,
    width: int,
    height: int,
    *,
    is_mobile: bool = False,
    has_touch: bool = False,
    device_scale_factor: int = 1,
    user_agent: str | None = None,
):
    record_video = os.environ.get("RECORD_VIDEO") == "1"
    video_dir = os.environ.get("RECORD_VIDEO_DIR", str(REPO_ROOT / ".omo" / "evidence"))
    context_kwargs: dict = {
        "viewport": {"width": width, "height": height},
        "is_mobile": is_mobile,
        "has_touch": has_touch,
        "device_scale_factor": device_scale_factor,
    }
    if user_agent is not None:
        context_kwargs["user_agent"] = user_agent
    if record_video:
        Path(video_dir).mkdir(parents=True, exist_ok=True)
        context_kwargs["record_video_dir"] = video_dir
        context_kwargs["record_video_size"] = {"width": width, "height": height}
    context = browser.new_context(**context_kwargs)
    page = context.new_page()
    page.goto(server)
    page.wait_for_selector(".tile")  # folder tree fetched and rendered
    return context, page


@pytest.fixture
def page(request, server):
    """Portrait viewport (single-page mode). Respect BROWSER/--browser switch."""
    b = request.getfixturevalue("webkit_browser") if _is_webkit_request(request) else request.getfixturevalue("browser")
    context, page = _new_page(b, server, 390, 844)
    yield page
    context.close()


@pytest.fixture
def landscape_page(request, server):
    """Landscape viewport (2-page spread mode by default). Respect BROWSER/--browser."""
    b = request.getfixturevalue("webkit_browser") if _is_webkit_request(request) else request.getfixturevalue("browser")
    context, page = _new_page(b, server, 900, 600)
    yield page
    context.close()


def _is_webkit_request(request):
    env = os.environ.get("BROWSER")
    if env == "webkit":
        return True
    try:
        opt = request.config.getoption("--browser", default=None)
        if opt == "webkit":
            return True
    except Exception:
        pass
    return False


@pytest.fixture
def ios_page(request, server):
    """iPhone 390x844 with isMobile/hasTouch, deviceScale 3, iPhone UA. BROWSER env or --browser webkit switches browser/webkit_browser."""
    b = request.getfixturevalue("webkit_browser") if _is_webkit_request(request) else request.getfixturevalue("browser")
    context, page = _new_page(
        b,
        server,
        390,
        844,
        is_mobile=True,
        has_touch=True,
        device_scale_factor=3,
        user_agent=IPHONE_UA,
    )
    yield page
    context.close()


@pytest.fixture
def ipad_page(request, server):
    """iPad portrait 820x1180 with isMobile/hasTouch, deviceScale 2, iPad UA."""
    b = request.getfixturevalue("webkit_browser") if _is_webkit_request(request) else request.getfixturevalue("browser")
    context, page = _new_page(
        b,
        server,
        820,
        1180,
        is_mobile=True,
        has_touch=True,
        device_scale_factor=2,
        user_agent=IPAD_UA,
    )
    yield page
    context.close()


@pytest.fixture
def ipad_landscape_page(request, server):
    """iPad landscape 1180x820 with isMobile/hasTouch, deviceScale 2, iPad UA."""
    b = request.getfixturevalue("webkit_browser") if _is_webkit_request(request) else request.getfixturevalue("browser")
    context, page = _new_page(
        b,
        server,
        1180,
        820,
        is_mobile=True,
        has_touch=True,
        device_scale_factor=2,
        user_agent=IPAD_UA,
    )
    yield page
    context.close()
