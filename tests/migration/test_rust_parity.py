"""Parity harness: bookq_rs (Rust) vs the Python reference impls.

Phase 1 W1 pilot (issue #163) — byte-for-byte verification of the sort-key
core. ``bookq_rs`` is optional: without a built wheel the whole module is
skipped, so the suite stays green on hosts that only carry the Python path.

Corpus layers:
  * EDGE_CASES   — committed tricky inputs (kanji slots, guards, big units,
                   full-width digits, leading zeros, empty segments, bignum)
  * randomized   — seeded names drawn from a kanji/digit-heavy pool
  * live names   — when BOOKQ_LIVE_ROOT is set, every file/dir name in the
                   real library. Names are never asserted/printed raw; a
                   mismatch is reported by index + sha256 prefix only
                   (tests/live/README.md secrecy rule).
"""

import hashlib
import os
import random
import string
import sys
from pathlib import Path

import pytest

_repo_root = Path(__file__).resolve().parent.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

bookq_rs = pytest.importorskip("bookq_rs")

from app import scanner


EDGE_CASES = [
    # kanji numeric slots / guards
    "第十話", "第1話", "第010話", "第一部", "第二十三章", "第0話", "第十回",
    "一〇一", "二十百", "〇一", "千千千", "十百", "一十一", "三百二十",
    # protected proper nouns / bail on 万億兆
    "千尋", "六本木", "千と千尋の神隠し", "一万年の眠り", "三千世界", "億千万",
    "本好きの下剋上【第一部】 一万年の眠り", "第万話", "千万",
    # PRE/POST boundary chars
    "第ー話", "第・話", "第 話", "之十", "の十", "-十", "【十】", "第十a",
    "a第十", "第十話x", "x第十話", "第", "話", "第話",
    # ASCII digits: empty segments, leading zeros, bignum runs
    "", "1", "a", "1a", "a1", "0", "00", "01.png", "10.png", "a1.png",
    "b0.png", "cover.png", "ch-2", "ch-10", "ch-7", "ch-007",
    "x" + "9" * 40, "9" * 40 + "x", "0" * 50, "1" + "0" * 30 + "話",
    # full-width digits stay text (ASCII-only split)
    "第２話", "２", "１０", "vol０２", "²",
    # mixed shapes that keep Python's int/str tuple structure
    "12abc34", "abc", "９話", "第 二 話", "v01 sub", "_2", "2_", "2a2b2",
]


def _random_corpus(n: int = 5000, seed: int = 42) -> list[str]:
    pool = (
        string.ascii_letters + string.digits
        + "第話部章巻回集編幕期頁号節月【】之の・ー- 一二三四五六七八九十百千万億兆〇千尋本木神隠"
        + "０１２３４５６７８９._~()[]abcXYZあいうえおｱｲｳ"
    )
    rng = random.Random(seed)
    return ["".join(rng.choice(pool) for _ in range(rng.randint(0, 30))) for _ in range(n)]


def _assert_parity(names: list[str], label: str) -> None:
    for i, name in enumerate(names):
        digest = hashlib.sha256(name.encode("utf-8", "surrogatepass")).hexdigest()[:10]
        assert bookq_rs.natural_key(name) == scanner._py_natural_key(name), (
            f"natural_key mismatch [{label} #{i} sha:{digest}]"
        )
        assert bookq_rs._sort_key(name) == scanner._py_sort_key(name), (
            f"_sort_key mismatch [{label} #{i} sha:{digest}]"
        )
        assert bookq_rs._normalize_kanji(name) == scanner._py_normalize_kanji(name), (
            f"_normalize_kanji mismatch [{label} #{i} sha:{digest}]"
        )
        assert bookq_rs._kanji_run_to_int(name) == scanner._py_kanji_run_to_int(name), (
            f"_kanji_run_to_int mismatch [{label} #{i} sha:{digest}]"
        )


def test_parity_edge_cases():
    _assert_parity(EDGE_CASES, "edge")


def test_parity_randomized():
    _assert_parity(_random_corpus(), "rand")


def test_parity_batch_key_generation():
    names = EDGE_CASES + _random_corpus(500, seed=7)
    assert bookq_rs.natural_keys(names) == [scanner._py_natural_key(n) for n in names]
    assert bookq_rs.sort_keys(names) == [scanner._py_sort_key(n) for n in names]


def test_sort_helpers_match_reference_sort():
    """_sort_natural/_sort_file batch ordering == key-lambda reference ordering."""
    names = list(dict.fromkeys(EDGE_CASES + _random_corpus(500, seed=11)))
    got_n, got_f = names[:], names[:]
    scanner._sort_natural(got_n)
    scanner._sort_file(got_f)
    assert got_n == sorted(names, key=lambda n: (scanner._py_natural_key(n), n))
    assert got_f == sorted(names, key=lambda n: (scanner._py_sort_key(n), n))


def test_dispatch_wrapper_matches_reference():
    """The public wrappers delegate to Rust when loaded (or BOOKQ_RUST=0 → py)."""
    for name in EDGE_CASES:
        assert scanner.natural_key(name) == scanner._py_natural_key(name)
        assert scanner._sort_key(name) == scanner._py_sort_key(name)
        assert scanner._normalize_kanji(name) == scanner._py_normalize_kanji(name)
        assert scanner._kanji_run_to_int(name) == scanner._py_kanji_run_to_int(name)


def test_rust_disabled_falls_back(monkeypatch):
    monkeypatch.setattr(scanner, "_rs", None)
    for name in EDGE_CASES:
        assert scanner.natural_key(name) == scanner._py_natural_key(name)
        assert scanner._sort_key(name) == scanner._py_sort_key(name)


def test_surrogate_names_fall_back():
    """surrogateescape names: Rust &str cannot hold them → wrapper falls back."""
    name = "第\udcff10話"
    expected = scanner._py_natural_key(name)
    # Rust raises on extraction; the dispatching wrapper must still equal py.
    with pytest.raises(Exception):
        bookq_rs.natural_key(name)
    assert scanner.natural_key(name) == expected
    assert scanner._sort_key(name) == scanner._py_sort_key(name)


def test_batch_surrogate_falls_back():
    names = ["ok1", "第\udcff10話", "ok2"]
    got = names[:]
    scanner._sort_natural(got)
    assert got == sorted(names, key=lambda n: (scanner._py_natural_key(n), n))


LIVE_ROOT = os.environ.get("BOOKQ_LIVE_ROOT")


@pytest.mark.skipif(not LIVE_ROOT, reason="BOOKQ_LIVE_ROOT not set")
def test_parity_live_names():
    """Every name in the real library passes both impls (hashed reporting)."""
    names = {p.name for p in Path(LIVE_ROOT).rglob("*")}
    assert names, "live root produced no names"
    _assert_parity(sorted(names), "live")
