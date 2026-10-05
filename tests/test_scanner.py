"""Unit tests for app/scanner.py folder ordering.

Covers the natural-sort key (ch-2 < ch-10) used for DIRECTORY names, the
composite (key, raw-name) tie-break that keeps digitless dirs deterministic,
and the regression guards: image files keep the numeric-only key (cover.png
first) and single-child flattening is untouched.

Run:
    uv run --with pytest --with-requirements requirements-dev.txt pytest tests/test_scanner.py -q
"""

import struct
import sys
import zlib
from pathlib import Path

# Ensure the repo root is on sys.path so "app" package is importable
# (same convention as tests/test_metadata.py)
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from app.scanner import (
    _kanji_run_to_int,
    list_images,
    natural_key,
    scan_tree,
)


def _make_png(path: Path) -> None:
    """Write a minimal solid-color PNG (mirror of tests/conftest.py:_make_png)."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 40, 60, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes((200, 60, 60)) * 40
    idat = zlib.compress(row * 60)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
    )


def _make_subdirs(root: Path, names: list[str], image: str = "p1.png") -> None:
    for name in names:
        d = root / name
        d.mkdir()
        _make_png(d / image)


# ── natural_key ───────────────────────────────────────────────────────────────


def test_natural_key_orders_digits_naturally():
    assert natural_key("ch-2") < natural_key("ch-10") < natural_key("ch-11")
    assert sorted(["ch-10", "ch-2", "ch-11"], key=natural_key) == ["ch-2", "ch-10", "ch-11"]


# ── scan_tree directory order ─────────────────────────────────────────────────


def test_scan_tree_orders_directories_naturally(tmp_path):
    _make_subdirs(tmp_path, ["ch-1", "ch-2", "ch-10", "ch-11"])
    names = [n["name"] for n in scan_tree(tmp_path)]
    assert names == ["ch-1", "ch-2", "ch-10", "ch-11"]


def test_scan_tree_digitless_dirs_lexicographic(tmp_path):
    _make_subdirs(tmp_path, ["aaa", "bbb", "ccc"])
    names = [n["name"] for n in scan_tree(tmp_path)]
    assert names == ["aaa", "bbb", "ccc"]


def test_scan_tree_leading_zero_tie_break(tmp_path):
    _make_subdirs(tmp_path, ["ch-7", "ch-007"])
    names = [n["name"] for n in scan_tree(tmp_path)]
    assert names == ["ch-007", "ch-7"]


# ── file-order regression guards ──────────────────────────────────────────────


def test_scan_tree_file_order_cover_first(tmp_path):
    _make_subdirs(tmp_path, [])
    for name in ["cover.png", "1.png", "2.png", "10.png"]:
        _make_png(tmp_path / name)
    names = [n["name"] for n in scan_tree(tmp_path)]
    assert names == ["cover.png", "1.png", "2.png", "10.png"]


def test_list_images_mixed_prefix_order(tmp_path):
    """Lock the _sort_key head-order: digitless files sort first, then numeric.

    Mixed prefixes ["a1.png","b0.png","cover.png","01.png","10.png"] keep the
    current head-order ["cover.png","b0.png","01.png","a1.png","10.png"] in
    both list_images and scan_tree file children (cover-first preserved;
    no tail migration to natural_key).
    """
    for name in ["a1.png", "b0.png", "cover.png", "01.png", "10.png"]:
        _make_png(tmp_path / name)
    expected = ["cover.png", "b0.png", "01.png", "a1.png", "10.png"]
    assert list_images(tmp_path) == expected
    assert [n["name"] for n in scan_tree(tmp_path)] == expected


def test_scan_tree_flattening_preserved(tmp_path):
    (tmp_path / "a" / "only" / "leaf").mkdir(parents=True)
    _make_png(tmp_path / "a" / "only" / "leaf" / "1.png")
    nodes = scan_tree(tmp_path)
    assert [n["name"] for n in nodes] == ["a/only/leaf/1.png"]
    assert nodes[0]["type"] == "image"


def test_scan_tree_flattening_keeps_thumb_folder_relative(tmp_path):
    # Flatten rewrites name ONLY; thumb/thumbs stay folder-relative for
    # resolveCover.js (front resolves /image/<_fullPath>/<thumb>).
    # Single-child level `a` hoists b's children with the "b/" prefix, while
    # each hoisted node keeps its own relative thumb (never "b/c1/1.png").
    (tmp_path / "a" / "b" / "c1").mkdir(parents=True)
    (tmp_path / "a" / "b" / "c2").mkdir(parents=True)
    _make_png(tmp_path / "a" / "b" / "c1" / "1.png")
    _make_png(tmp_path / "a" / "b" / "c2" / "1.png")
    _make_subdirs(tmp_path, ["other"])
    _make_png(tmp_path / "other" / "1.png")
    by_name = {n["name"]: n for n in scan_tree(tmp_path)}
    assert set(by_name) == {"a", "other"}
    kids = {n["name"]: n for n in by_name["a"]["children"]}
    assert set(kids) == {"b/c1", "b/c2"}
    for name in ("b/c1", "b/c2"):
        assert kids[name]["type"] == "folder"
        assert kids[name]["thumb"] == "1.png", f"{name} thumb must stay folder-relative, got {kids[name]!r}"
        assert "thumbs" not in kids[name], f"{name} must not gain thumbs from flattening"
    assert by_name["a"]["thumbs"] == ["b/c1/1.png", "b/c2/1.png"]


# ── full-width digit safety ───────────────────────────────────────────────────


def test_natural_key_fullwidth_digits_no_crash(tmp_path):
    # Python's \d would match the full-width ２ and crash the sort with a
    # str-vs-int TypeError; the ASCII-only ([0-9]+) key must not.
    assert natural_key("第２話") == ("第２話",)  # single text part, like the JS key
    _make_subdirs(tmp_path, ["第10話", "第２話"])
    names = [n["name"] for n in scan_tree(tmp_path)]
    assert names == ["第10話", "第２話"]


# ── kanji numeral natural sort ────────────────────────────────────────────────


def test_natural_key_kanji_numeric_slots():
    """Kanji numerals in numeric slots (第X部 / 第X話 / 第X章) convert to digits."""
    cases = {
        "第一部": ("第", 1, "部"),
        "第三部": ("第", 3, "部"),
        "第十話": ("第", 10, "話"),
        "第十一話": ("第", 11, "話"),
        "第二十三章": ("第", 23, "章"),
        "百二": ("", 102, ""),
        "二千五": ("", 2005, ""),
        "三百二十一": ("", 321, ""),
        "九千九百九十九": ("", 9999, ""),
        "十": ("", 10, ""),
        "十二": ("", 12, ""),
        "二十": ("", 20, ""),
        "一十": ("", 10, ""),
        "百十": ("", 110, ""),
        "十十": ("", 20, ""),  # greedy unit-consumption semantics locked
        "一十一": ("", 11, ""),
        "十月": ("", 10, "月"),  # 月 ∈ _KANJI_POST (context allows; 月 stays a text part)
        "九十九": ("", 99, ""),  # standalone run at string edges converts
    }
    for name, expected in cases.items():
        assert natural_key(name) == expected, f"natural_key({name!r})"


def test_natural_key_kanji_noncanonical_greedy_semantics():
    # Non-canonical forms follow the greedy algorithm exactly as implemented:
    # consecutive digits OVERWRITE the pending digit (only the digit immediately
    # before a unit is multiplied). 一〇一 is therefore 1, not 101, and
    # 二十百 is 2×10 + 1×100 = 120, not 2000. Locked behavior (JS mirror is
    # byte-identical, so parity holds).
    assert natural_key("一〇一") == ("", 1, "")
    assert natural_key("二十百") == ("", 120, "")
    assert natural_key("〇一") == ("", 1, "")


def test_natural_key_kanji_word_protection():
    """Proper nouns keep their kanji numerals as text (no conversion)."""
    for name in [
        "六本木", "五反田", "四谷", "八王子", "九条",
        "千尋", "千代", "千代田", "千と千尋の神隠し",
        "百鬼夜行", "三日月", "十兵衛", "十返舎", "一之介", "五郎",
        "一人", "三千人", "三千年",
    ]:
        assert natural_key(name) == (name,), f"natural_key({name!r})"


def test_natural_key_kanji_big_units_bail():
    """万/億/兆 never numericize — the whole run stays text (no partial conversion)."""
    for name in ["三万字", "一万", "一億", "二十万五千", "九千九百九十九万"]:
        assert natural_key(name) == (name,), f"natural_key({name!r})"


def test_natural_key_kanji_mixed_with_big_unit():
    # The 一 inside 【第一部】 converts; 一万 stays text (万 bails its run).
    assert natural_key("本好きの下剋上【第一部】 一万年の眠り") == (
        "本好きの下剋上【第", 1, "部】 一万年の眠り",
    )


def test_natural_key_kanji_regressions():
    # Full-width digit stays text (existing guarantee); 第二話 sorts first.
    assert natural_key("第２話") == ("第２話",)
    assert sorted(["第２話", "第二話"], key=natural_key) == ["第二話", "第２話"]
    # 第010話 / 第10話 / 第十話 all key ("第", 10, "話"); 第〇一話 = 第01話 → 1.
    assert natural_key("第010話") == ("第", 10, "話")
    assert natural_key("第10話") == ("第", 10, "話")
    assert natural_key("第十話") == ("第", 10, "話")
    assert natural_key("第〇一話") == ("第", 1, "話")
    assert sorted(["第010話", "第10話", "第十話", "第〇一話"], key=natural_key) == [
        "第〇一話", "第010話", "第10話", "第十話",
    ]
    # 第一部 vs 第1部: keys are EQUAL; the composite (key, raw-name) tie-break
    # that scan_tree uses keeps 第1部 first (no flip vs today).
    assert natural_key("第一部") == natural_key("第1部")
    assert sorted(["第一部", "第1部"], key=lambda d: (natural_key(d), d)) == ["第1部", "第一部"]
    # 第二部 vs 第10部: 2 < 10.
    assert sorted(["第二部", "第10部"], key=natural_key) == ["第二部", "第10部"]
    # ASCII-digit names unchanged; 第11話 idempotent; trivial inputs.
    assert natural_key("ch-2") == ("ch-", 2, "")
    assert natural_key("ch-02") == ("ch-", 2, "")
    assert natural_key("ch-10") == ("ch-", 10, "")
    assert natural_key("ch-2.1") == ("ch-", 2, ".", 1, "")
    assert natural_key("第11話") == ("第", 11, "話")
    assert natural_key("") == ("",)
    assert natural_key("123") == ("", 123, "")
    assert natural_key("abc") == ("abc",)


def test_scan_tree_kanji_parts_natural_order(tmp_path):
    """The user's real case: 本好きの下剋上【第一部】〜【第五部】 sort 1,2,3,4,5."""
    _make_subdirs(tmp_path, [
        "本好きの下剋上【第一部】", "本好きの下剋上【第二部】",
        "本好きの下剋上【第三部】", "本好きの下剋上【第四部】",
        "本好きの下剋上【第五部】",
    ])
    expected = [
        "本好きの下剋上【第一部】", "本好きの下剋上【第二部】",
        "本好きの下剋上【第三部】", "本好きの下剋上【第四部】",
        "本好きの下剋上【第五部】",
    ]
    names = [n["name"] for n in scan_tree(tmp_path)]
    assert names == expected


def test_scan_tree_kanji_chapters_natural_order(tmp_path):
    _make_subdirs(tmp_path, ["第十話", "第十一話", "第二話"])
    expected = ["第二話", "第十話", "第十一話"]
    names = [n["name"] for n in scan_tree(tmp_path)]
    assert names == expected


# ── thumbs cap-at-9 order ─────────────────────────────────────────────────────


def test_scan_tree_thumbs_cap_selects_natural_first_nine(tmp_path):
    """A folder with 10+ chapter subfolders must preview the natural-order
    first 9 (vol1..vol9), not the alphabetically-first 9 (vol1, vol10, vol11,
    vol12, vol2..vol6) -- the sub_thumbs loop breaks at 9 over the same
    `dirs` list natural_key now sorts."""
    (tmp_path / "series").mkdir()
    _make_subdirs(tmp_path / "series", [f"vol{i}" for i in range(1, 13)])
    _make_subdirs(tmp_path, ["other"])  # 2nd root child so "series" isn't flattened away

    series = next(n for n in scan_tree(tmp_path) if n["name"] == "series")
    assert series["thumbs"] == [f"vol{i}/p1.png" for i in range(1, 10)]


# ── Todo 4 parity tests: fullwidth-as-text / 万億兆 bail / Py/JS constants ──


def test_natural_key_fullwidth_is_text():
    """Full-width ２ is text, not a number: 第10話 (numeric 10) sorts before 第２話 (text)."""
    # 第２話 is a single text token — the ASCII-only split keeps full-width digits as text,
    # matching the JS /(\d+)/ behaviour and avoiding the TypeError str-vs-int sort crash.
    assert natural_key("第２話") == ("第２話",)
    assert natural_key("第10話") == ("第", 10, "話")
    # numeric tuple ("第", 10, "話") < text tuple ("第２話",) in Python's tuple compare
    assert natural_key("第10話") < natural_key("第２話")
    assert sorted(["第２話", "第10話"], key=natural_key) == ["第10話", "第２話"]
    # Also fixed expectation: ASCII-only numeric "10" is numeric, full-width "２" is not
    assert natural_key("第２話") != natural_key("第2話")


def test_kanji_bail_on_big_units():
    """万/億/兆 bail: any run containing one stays fully text — no partial conversion."""
    # Direct _kanji_run_to_int bail (single-source kernel)
    assert _kanji_run_to_int("一万") is None
    assert _kanji_run_to_int("一万億") is None
    assert _kanji_run_to_int("二十万") is None
    assert _kanji_run_to_int("万") is None
    assert _kanji_run_to_int("億") is None
    assert _kanji_run_to_int("兆") is None
    # Via natural_key: whole name stays a single text token
    assert natural_key("一万") == ("一万",)
    assert natural_key("一万億") == ("一万億",)
    assert natural_key("二十万五千") == ("二十万五千",)
    assert natural_key("三万字") == ("三万字",)
    # Mixed: only slots without 万/億/兆 convert; 一万 stays text while 【第一部】 converts
    assert natural_key("本好きの下剋上【第一部】 一万年の眠り") == (
        "本好きの下剋上【第", 1, "部】 一万年の眠り",
    )
    # Bail keeps text ordering (byte-lexicographic among themselves), not numeric
    assert natural_key("一万") < natural_key("一万億")  # "一万" prefix < "一万億"


# ── symlink cycle guard (#108) ───────────────────────────────────────────────


def test_scan_tree_breaks_symlink_cycles(tmp_path):
    # dl/a/b/c points back at dl: the walk must terminate instead of
    # recursing forever, and the reachable content stays intact.
    dl = tmp_path / "dl"
    dl.mkdir()
    (dl / "a" / "b").mkdir(parents=True)
    (dl / "a" / "b" / "c").symlink_to(dl, target_is_directory=True)
    _make_png(dl / "a" / "b" / "1.png")
    nodes = scan_tree(dl)
    assert [n["name"] for n in nodes] == ["a/b/1.png"]
    assert nodes[0]["type"] == "image"
