"""dual-reader shared spread model — pure functions (#76 / S01).

Contract: parent issue #75 §3.2. The server builds the shared spread table;
each device only projects its assigned side (pairModel.js projectSpread) and
never recomputes the pairing itself.

Spread = {id, chapterFolder, right: PageRef, left: PageRef|null}.
  offset=0: (p1 R, p2 L) (p3 R, p4 L) …
  offset=1: (p1 R, blank) (p2 R, p3 L) (p4 R, p5 L) …
The right page always leads within a chapter; a missing partner stays blank —
never back-filled, never carried over, and chapters never share a spread.
"""
from __future__ import annotations

from dataclasses import dataclass


class ModelError(ValueError):
    """Typed rejection for invalid model input (distinguishable by .code)."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class PageRef:
    folder: str
    name: str


@dataclass(frozen=True)
class Spread:
    id: str
    chapter_folder: str
    right: PageRef
    left: PageRef | None


def make_ref(folder, name) -> PageRef:
    if not isinstance(folder, str) or not isinstance(name, str):
        raise ModelError("ERR_MALFORMED", "PageRef folder/name must be str")
    return PageRef(folder=folder, name=name)


def _as_ref(chapter_folder: str, page) -> PageRef:
    if isinstance(page, PageRef):
        return page
    if isinstance(page, str):
        return PageRef(folder=chapter_folder, name=page)
    if isinstance(page, dict):
        try:
            return make_ref(page["folder"], page["name"])
        except KeyError as e:
            raise ModelError("ERR_MALFORMED", f"page dict missing key: {e}") from e
    raise ModelError("ERR_MALFORMED", f"unsupported page form: {type(page)}")


def _spread_id(chapter_index: int, spread_index: int) -> str:
    return f"c{chapter_index}:s{spread_index}"


def build_spread_plan(chapters, offset: int) -> list[Spread]:
    """Build the shared spread table.

    chapters: iterable of {"folder": str, "pages": [name|{"folder","name"}|PageRef]}.
    Returns the flat ordered list; every Spread carries its chapterFolder.
    """
    if offset not in (0, 1):
        raise ModelError("ERR_RANGE", f"offset must be 0 or 1, got {offset!r}")
    if chapters is None:
        raise ModelError("ERR_MALFORMED", "chapters is None")
    spreads: list[Spread] = []
    for ci, ch in enumerate(chapters):
        try:
            folder = ch["folder"]
            pages = ch["pages"]
        except (KeyError, TypeError) as e:
            raise ModelError("ERR_MALFORMED", f"chapter {ci} needs folder+pages") from e
        if not isinstance(folder, str):
            raise ModelError("ERR_MALFORMED", f"chapter {ci} folder must be str")
        if not isinstance(pages, (list, tuple)):
            raise ModelError("ERR_MALFORMED", f"chapter {ci} pages must be a list")
        refs = [_as_ref(folder, p) for p in pages]
        n = len(refs)
        si = 0
        i = 0
        if offset == 1:
            # Leading spread pairs the first page with a blank left side.
            if n >= 1:
                spreads.append(Spread(_spread_id(ci, 0), folder, refs[0], None))
                si = 1
            i = 1
        while i < n:
            right = refs[i]
            left = refs[i + 1] if i + 1 < n else None
            spreads.append(Spread(_spread_id(ci, si), folder, right, left))
            si += 1
            i += 2
    return spreads


def spread_index_by_id(plan: list[Spread], spread_id: str) -> int:
    for i, s in enumerate(plan):
        if s.id == spread_id:
            return i
    return -1


def spread_index_for_page(plan: list[Spread], ref) -> int:
    """Index of the spread containing ref (either side), else -1."""
    target = ref if isinstance(ref, PageRef) else make_ref(ref["folder"], ref["name"])
    for i, s in enumerate(plan):
        if s.right == target or s.left == target:
            return i
    return -1


def goto_index(plan: list[Spread], ref) -> int:
    """GOTO: the spread containing ref. Missing page is an explicit error,
    never a silent fallback to another page."""
    idx = spread_index_for_page(plan, ref)
    if idx < 0:
        raise ModelError("ERR_RANGE", f"page not in plan: {ref!r}")
    return idx


def clamp_index(plan: list[Spread], index: int) -> int:
    if not plan:
        return -1
    return max(0, min(index, len(plan) - 1))


def turn_index(plan: list[Spread], index: int, delta: int, cross_chapter: bool) -> int:
    """Resolve a TURN request. Out-of-range clamps to the boundary — the
    chapter's own contiguous block when cross_chapter is False."""
    if not plan:
        return -1
    if not isinstance(index, int) or index < 0 or index >= len(plan):
        raise ModelError("ERR_RANGE", f"spread index out of range: {index!r}")
    if delta not in (1, -1):
        raise ModelError("ERR_RANGE", f"delta must be ±1, got {delta!r}")
    target = index + delta
    if cross_chapter:
        return clamp_index(plan, target)
    chapter = plan[index].chapter_folder
    lo = index
    while lo > 0 and plan[lo - 1].chapter_folder == chapter:
        lo -= 1
    hi = index
    while hi + 1 < len(plan) and plan[hi + 1].chapter_folder == chapter:
        hi += 1
    return max(lo, min(target, hi))


def assigned_ref(spread: Spread, role: str, leader_side: str) -> PageRef | None:
    """Canonical projection: which ref this member renders. leader renders
    leader_side; follower the opposite. Blank side -> None."""
    if leader_side not in ("right", "left"):
        raise ModelError("ERR_RANGE", f"leader_side must be right|left, got {leader_side!r}")
    if role == "leader":
        side = leader_side
    elif role == "follower":
        side = "left" if leader_side == "right" else "right"
    else:
        raise ModelError("ERR_MALFORMED", f"role must be leader|follower, got {role!r}")
    return spread.right if side == "right" else spread.left


def serialize_spread(s: Spread) -> dict:
    return {
        "id": s.id,
        "chapterFolder": s.chapter_folder,
        "right": {"folder": s.right.folder, "name": s.right.name},
        "left": None if s.left is None else {"folder": s.left.folder, "name": s.left.name},
    }
