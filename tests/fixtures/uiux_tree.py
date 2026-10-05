#!/usr/bin/env python3
"""Build the synthetic downloads tree for dev environments (./dev.sh up --fixture) and the UI/UX audit.
Deterministic, dependency-free (pure stdlib PNG writer). Color-coded solid
pages so vision-model screenshots can distinguish folders, with a per-page
lightness gradient within each folder's hue family so page turns are visible
(no two pages in a folder are byte-identical). `zoom_episodes/` is the one
exception: real-size 720x1080 pages with a grid, huge EP/page labels, START/END
badges and fine print, so pinch-zoom, page turns and episode seams can be judged
by eye in the Simulator or on a device (solid pages look identical when zoomed).
Usage:  uv run python .omo/scripts/uiux_tree.py [target_dir]
Default target: /tmp/bookq-uiux/downloads
"""
import colorsys, struct, sys, zlib
from pathlib import Path

def make_png(path, w=40, h=60, rgb=(200, 60, 60)):
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(rgb) * w
    idat = zlib.compress(row * h)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                     + chunk(b"IDAT", idat) + chunk(b"IEND", b""))

def make_png_rows(path, w, h, rows):
    """Write an RGB PNG from a list of `h` bytearrays, each `3*w` bytes."""
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"".join(b"\x00" + bytes(r) for r in rows), 6)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                     + chunk(b"IDAT", idat) + chunk(b"IEND", b""))

# 5x7 bitmap font for the zoom/navigation fixture (digits, a few letters, '/', '-').
FONT5x7 = {
    "0": ["01110","10001","10011","10101","11001","10001","01110"],
    "1": ["00100","01100","00100","00100","00100","00100","01110"],
    "2": ["01110","10001","00001","00010","00100","01000","11111"],
    "3": ["11110","00001","00001","01110","00001","00001","11110"],
    "4": ["00010","00110","01010","10010","11111","00010","00010"],
    "5": ["11111","10000","11110","00001","00001","10001","01110"],
    "6": ["00110","01000","10000","11110","10001","10001","01110"],
    "7": ["11111","00001","00010","00100","01000","01000","01000"],
    "8": ["01110","10001","10001","01110","10001","10001","01110"],
    "9": ["01110","10001","10001","01111","00001","00010","01100"],
    "E": ["11111","10000","10000","11110","10000","10000","11111"],
    "P": ["11110","10001","10001","11110","10000","10000","10000"],
    "S": ["01111","10000","10000","01110","00001","00001","11110"],
    "T": ["11111","00100","00100","00100","00100","00100","00100"],
    "A": ["01110","10001","10001","11111","10001","10001","10001"],
    "R": ["11110","10001","10001","11110","10100","10010","10001"],
    "N": ["10001","11001","10101","10011","10001","10001","10001"],
    "D": ["11110","10001","10001","10001","10001","10001","11110"],
    "L": ["10000","10000","10000","10000","10000","10000","11111"],
    "/": ["00001","00010","00010","00100","01000","01000","10000"],
    "-": ["00000","00000","00000","11111","00000","00000","00000"],
    " ": ["00000"] * 7,
}

class Canvas:
    """Tiny RGB raster with just enough drawing for the zoom fixture."""
    def __init__(self, w, h, bg=(255, 255, 255)):
        self.w, self.h = w, h
        self.rows = [bytearray(bytes(bg) * w) for _ in range(h)]
    def px(self, x, y, rgb):
        if 0 <= x < self.w and 0 <= y < self.h:
            self.rows[y][3*x:3*x+3] = bytes(rgb)
    def rect(self, x0, y0, x1, y1, rgb):
        for y in range(max(0, y0), min(self.h, y1)):
            for x in range(max(0, x0), min(self.w, x1)):
                self.rows[y][3*x:3*x+3] = bytes(rgb)
    def text(self, s, x, y, scale, rgb):
        for ch in s:
            glyph = FONT5x7.get(ch, FONT5x7[" "])
            for gy, line in enumerate(glyph):
                for gx, bit in enumerate(line):
                    if bit == "1":
                        self.rect(x + gx*scale, y + gy*scale,
                                  x + (gx+1)*scale, y + (gy+1)*scale, rgb)
            x += 6 * scale
    def text_width(self, s, scale):
        return (6 * len(s) - 1) * scale

def make_zoom_page(path, ep, page, total, w=720, h=1080, tint=(60, 120, 220)):
    """A page whose content is legible at every zoom level:
    - 1px grid every 40px, 3px grid every 200px (fine detail only visible zoomed in)
    - tinted top band + huge 'EP<n>' / 'P<i>/<n>' (readable at fit-to-screen)
    - START / END badges on the first / last page of an episode (episode seams)
    - L / R edge labels (RTL sanity), colored corner brackets (crop / orientation)
    - a small-scale caption row that only resolves when pinched in"""
    c = Canvas(w, h)
    grid, bold = (200, 200, 200), (120, 120, 120)
    for x in range(0, w, 40):
        c.rect(x, 0, x+1, h, bold if x % 200 == 0 else grid)
        if x % 200 == 0: c.rect(x, 0, x+3, h, bold)
    for y in range(0, h, 40):
        c.rect(0, y, w, y+1, bold if y % 200 == 0 else grid)
        if y % 200 == 0: c.rect(0, y, w, y+3, bold)
    # diagonal so a crop or offset is obvious
    for i in range(max(w, h)):
        c.px(int(i * w / max(w, h)), int(i * h / max(w, h)), (0, 0, 0))
    # corner brackets: TL red, TR green, BL blue, BR orange
    L, T = 90, 14
    for (cx, cy, sx, sy, col) in [(0, 0, 1, 1, (220, 40, 40)), (w, 0, -1, 1, (40, 170, 40)),
                                  (0, h, 1, -1, (40, 80, 220)), (w, h, -1, -1, (230, 140, 20))]:
        x0, x1 = sorted((cx, cx + sx * L)); y0, y1 = sorted((cy, cy + sy * T))
        c.rect(x0, y0, x1, y1, col)
        x0, x1 = sorted((cx, cx + sx * T)); y0, y1 = sorted((cy, cy + sy * L))
        c.rect(x0, y0, x1, y1, col)
    # episode tint band with the episode label
    band_h = 150
    c.rect(0, 60, w, 60 + band_h, tint)
    label = f"EP{ep}"
    c.text(label, (w - c.text_width(label, 16)) // 2, 60 + (band_h - 7*16) // 2, 16, (255, 255, 255))
    # page counter, big and centered
    counter = f"P{page}/{total}"
    sc = 14 if len(counter) <= 5 else 11
    c.text(counter, (w - c.text_width(counter, sc)) // 2, h // 2 - 7*sc // 2, sc, (20, 20, 20))
    # START / END badge on episode edges
    badge = "START" if page == 1 else ("END" if page == total else "")
    if badge:
        bw = c.text_width(badge, 8) + 40
        bx = (w - bw) // 2; by = h // 2 + 7*14 // 2 + 40
        c.rect(bx, by, bx + bw, by + 7*8 + 30, (30, 30, 30))
        c.text(badge, bx + 20, by + 15, 8, (255, 230, 80))
    # L / R edge labels
    c.text("L", 30, h // 2 - 7*6 // 2, 6, (90, 90, 90))
    c.text("R", w - 30 - c.text_width("R", 6), h // 2 - 7*6 // 2, 6, (90, 90, 90))
    # fine caption row: legible only when zoomed
    cap = f"EP{ep}-P{page}/{total} " * 6
    c.text(cap.strip(), 20, h - 40, 1, (0, 0, 0))
    c.text(f"EP{ep} P{page}", 20, h - 70, 2, (0, 0, 0))
    make_png_rows(path, w, h, c.rows)

def page_color(base_rgb, page_num, total):
    """Same-hue gradient: keep the folder's hue, go bright -> dark so pages
    stay in one color family but are clearly distinguishable."""
    h, l, s = colorsys.rgb_to_hls(*(c / 255.0 for c in base_rgb))
    t = page_num / max(total - 1, 1) if total > 1 else 0.0
    l = 0.78 - t * 0.42              # first page bright, last page deep (never black: >= 0.36)
    s = min(1.0, s + 0.12)           # slight saturation boost for clarity
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return (int(round(r * 255)), int(round(g * 255)), int(round(b * 255)))

COLORS = {
    "simple_manga": (200, 60, 60), "long": (60, 200, 60),
    "series": (60, 60, 200), "mixed_ar": (200, 120, 60),
    "wide_spread": (120, 60, 200), "夏 の 本": (60, 200, 200),
    "group": (200, 60, 120), "chain": (120, 200, 60),
    "tight_fit": (200, 160, 60), "kanji": (180, 60, 180),
    "episodes": (60, 180, 120), "fullwidth": (120, 160, 200),
    "wrap": (160, 120, 60), "odd_wrap": (120, 60, 160),
    "mixed": (100, 140, 200),
}

THUMB_GRID_PALETTE = [
    (230,  50,  50),
    (230, 130,  50),
    (210, 200,  50),
    ( 80, 200,  80),
    ( 50, 200, 180),
    ( 50, 120, 230),
    (120,  60, 230),
    (200,  50, 180),
    (230,  70, 110),
]

METADATA_TMPL = """[source]
name = "test"

[comic]
title_jp = "{title}"
total_pages = 0

[tags]
artist = ["テスト作者"]
male = ["glasses"]
female = ["ponytail"]
"""

def build(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    simple = root / "simple_manga"; simple.mkdir(exist_ok=True)
    names = ["1.png", "2.png", "3.png", "10.png", "11.png"]
    for i, name in enumerate(names):
        make_png(simple / name, rgb=page_color(COLORS["simple_manga"], i, len(names)))
    long_dir = root / "long"; long_dir.mkdir(exist_ok=True)
    for i in range(1, 31):
        make_png(long_dir / f"p{i:03d}.png", rgb=page_color(COLORS["long"], i - 1, 30))
    for (vol, pages), base in [(("vol1", 2), THUMB_GRID_PALETTE[0]), (("vol2", 1), THUMB_GRID_PALETTE[5])]:
        d = root / "series" / vol; d.mkdir(parents=True, exist_ok=True)
        for i in range(1, pages + 1):
            make_png(d / f"p{i}.png", rgb=page_color(base, i - 1, pages))
    mixed_ar = root / "mixed_ar"; mixed_ar.mkdir(exist_ok=True)
    for i in range(1, 7):
        tall = i % 2 == 1
        make_png(mixed_ar / f"p{i}.png", w=40 if tall else 90, h=60,
                 rgb=page_color(COLORS["mixed_ar"], i - 1, 6))
    wide = root / "wide_spread"; wide.mkdir(exist_ok=True)
    for i, (w, h) in enumerate([(40, 60), (40, 60), (160, 60), (40, 60)], start=1):
        make_png(wide / f"p{i}.png", w=w, h=h,
                 rgb=page_color(COLORS["wide_spread"], i - 1, 4))
    jp = root / "夏 の 本"; jp.mkdir(exist_ok=True)
    make_png(jp / "1.png", rgb=page_color(COLORS["夏 の 本"], 0, 1))
    for vol, pages in [("vol1", 2), ("vol2", 1)]:
        d = root / "group" / "deep_series" / vol; d.mkdir(parents=True, exist_ok=True)
        for i in range(1, pages + 1):
            make_png(d / f"p{i}.png", rgb=page_color(COLORS["group"], i - 1, pages))
    other = root / "group" / "other_work"; other.mkdir(parents=True, exist_ok=True)
    make_png(other / "1.png", rgb=page_color(COLORS["group"], 0, 1))
    leaf = root / "chain" / "only" / "leaf"; leaf.mkdir(parents=True, exist_ok=True)
    make_png(leaf / "1.png", rgb=page_color(COLORS["chain"], 0, 2))
    make_png(leaf / "2.png", rgb=page_color(COLORS["chain"], 1, 2))
    (root / "empty_folder").mkdir(exist_ok=True)

    for ch in ["ch-1", "ch-2", "ch-10", "ch-11"]:
        d = root / "group" / "episodes" / ch; d.mkdir(parents=True, exist_ok=True)
        make_png(d / "p1.png", rgb=page_color(COLORS["episodes"], 0, 1))
    for ch in ["第10話", "第２話"]:
        d = root / "group" / "fullwidth_episodes" / ch; d.mkdir(parents=True, exist_ok=True)
        make_png(d / "p1.png", rgb=page_color(COLORS["fullwidth"], 0, 1))
    for vol, pages in [("vol1", 2), ("vol2", 1)]:
        d = root / "wrap" / "inner" / vol; d.mkdir(parents=True, exist_ok=True)
        for i in range(1, pages + 1):
            make_png(d / f"p{i}.png", rgb=page_color(COLORS["wrap"], i - 1, pages))
    for vol, pages in [("vol1", 1), ("vol2", 2)]:
        d = root / "odd_wrap" / "inner" / vol; d.mkdir(parents=True, exist_ok=True)
        for i in range(1, pages + 1):
            make_png(d / f"p{i}.png", rgb=page_color(COLORS["odd_wrap"], i - 1, pages))
    mixed = root / "mixed"; (mixed / "extra").mkdir(parents=True, exist_ok=True)
    make_png(mixed / "cover.png", rgb=page_color(COLORS["mixed"], 0, 2))
    make_png(mixed / "extra" / "1.png", rgb=page_color(COLORS["mixed"], 1, 2))
    tight = root / "tight_fit"; tight.mkdir(exist_ok=True)
    for i in range(1, 9):
        make_png(tight / f"p{i}.png", w=120, h=170, rgb=page_color(COLORS["tight_fit"], i - 1, 8))
    for part in ["【第一部】", "【第二部】", "【第三部】", "【第四部】", "【第五部】"]:
        d = root / "kanji" / "series1" / part; d.mkdir(parents=True, exist_ok=True)
        make_png(d / "p1.png", rgb=page_color(COLORS["kanji"], 0, 1))
    for ch in ["第二話", "第十話", "第十一話"]:
        d = root / "kanji" / "series2" / ch; d.mkdir(parents=True, exist_ok=True)
        make_png(d / "p1.png", rgb=page_color(COLORS["kanji"], 0, 1))

    # zoom_episodes: real-size (720x1080) pages with grid, big EP/page labels,
    # START/END badges and fine print — for pinch-zoom, page-turn and
    # episode-seam QA in the Simulator / on device. Odd page counts so the
    # spread pairing and the __spacer__ at each seam are exercised.
    # Chapters sit at depth 2 (zoom_episodes/series/ep-N) on purpose: sibling
    # concatenation only kicks in at SIBLING_AUTOLOAD_MIN_DEPTH = 2 (app.js), so
    # a depth-1 layout would open each episode alone and tapping past the last
    # page would do nothing. Deep link: #/view/zoom_episodes/series/ep-1?p=0
    for ep, (name, pages, tint) in enumerate(
            [("ep-1", 5, (60, 120, 220)), ("ep-2", 4, (40, 160, 90)), ("ep-3", 7, (200, 80, 60))], start=1):
        d = root / "zoom_episodes" / "series" / name; d.mkdir(parents=True, exist_ok=True)
        for i in range(1, pages + 1):
            make_zoom_page(d / f"p{i:02d}.png", ep, i, pages, tint=tint)

    for n in range(1, 10):
        parent = root / f"thumb_grid_{n:02d}"
        parent.mkdir(parents=True, exist_ok=True)
        for i in range(1, n + 1):
            wdir = parent / f"work_{i:02d}"
            wdir.mkdir(parents=True, exist_ok=True)
            base = THUMB_GRID_PALETTE[i - 1]
            rgb = page_color(base, 0, 1)
            make_png(wdir / "1.png", rgb=rgb)

    # Metadata fixtures: make /api/search + /api/metadata/facets return data.
    for folder, title in [("simple_manga", "シンプル漫画"), ("series", "シリーズ"),
                          ("mixed_ar", "混在アスペクト")]:
        mdir = root / folder
        if mdir.is_dir():
            (mdir / "metadata.toml").write_text(
                METADATA_TMPL.format(title=title), encoding="utf-8")

if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/bookq-uiux/downloads")
    build(target)
    pngs = list(target.rglob("*.png"))
    print(f"tree built at {target} ({len(pngs)} pngs)")
