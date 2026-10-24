"""S10 (#85) — synthetic work declarations for the dual-reader runner.

Every work name used by ``scripts/reader-sync/scenarios.json`` resolves
here. Page names carry the ``fx:`` prefix on purpose: verify_core.redact /
safe_name treat only ``fx:``-prefixed identifiers as evidence-safe
synthetics — a real folder name in evidence collapses to a sha256 stub,
so a fixture page name leaking real data is structurally impossible.
"""
from __future__ import annotations


def _pages(folder: str, n: int) -> list[str]:
    return [f"{folder}/p{i:03d}.png" for i in range(1, n + 1)]


def work(folder: str, n_pages: int, offset: int = 0,
         second_chapter: int | None = None) -> dict:
    """Single- or two-chapter synthetic work declaration."""
    chapters = [{"folder": folder, "pages": _pages(folder, n_pages)}]
    if second_chapter:
        ch2 = folder + "-ch2"
        chapters.append({"folder": ch2, "pages": _pages(ch2, second_chapter)})
    return {"offset": offset, "chapters": chapters}


WORKS = {
    # 6-page single chapter -> 3 full spreads, no blanks
    "fx6": work("fx:workA", 6),
    # 5-page single chapter -> last spread has a blank left side
    "fx5": work("fx:workB", 5),
    # offset=1: first spread pairs page1 with a blank left
    "fx5_offset1": work("fx:workC", 5, offset=1),
    # two 3-page chapters: turns must not cross the chapter boundary
    "fx_two_chapters": work("fx:workD", 3, second_chapter=3),
}
