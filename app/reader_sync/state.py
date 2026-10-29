"""dual-reader authoritative shared state + sequencer (#80 / S05).

Parent issue #75 §3.4: the sequencer is a *logical role*, not a place —
on the ``server-ws`` data plane the server sequences (strict receive order),
on ``webrtc-dc`` the leader sequences (same-tick arrivals ordered by the
role-deterministic tie-break, then forwarded). Either way there is exactly
one ordering authority and ``revision`` is strictly monotonic.

This module is pure and transport-free: no sockets, no I/O, no clock. The
``session`` argument is duck-typed (``sessions.PairSession``) — this module
never imports ``sessions``, keeping the dependency one-directional.

Wire contract for sequenced requests (TURN/GOTO/SET_OFFSET/SWAP_SIDES/
OPEN_WORK/READY/APPLIED):

- relative ops (TURN) are applied in sequencer receive order inside the
  same workEpoch — they never check baseRevision.
- absolute ops (GOTO/SET_OFFSET/SWAP_SIDES) require
  ``baseRevision == session.revision`` — a stale base is rejected and the
  latest snapshot returned, so an old absolute move can never overwrite a
  newer commit.
- OPEN_WORK skips every optimistic check: a work pick is self-contained
  (folder+page in the payload), "latest pick wins" is the intent, and the
  pending_load generation guard already discards an older load finishing
  late — rejecting a pick framed on a stale base only swallowed a live
  tile tap on the leader's browse list (silent no-op).
- any stale workEpoch / manifestVersion / out-of-phase request is rejected
  with a typed reason and leaves the committed snapshot exactly unchanged.
"""
from __future__ import annotations

from app.reader_sync import model, protocol

# ── reject reasons for sequenced ops (wire-stable strings) ─────────
# literals for reasons already defined in sessions.py (peer_offline /
# not_leader / malformed) are re-declared here so the state layer stays
# self-contained; the STRINGS are the contract, not the constant's home.
REASON_STALE_EPOCH = "stale_epoch"          # workEpoch mismatch
REASON_STALE_REVISION = "stale_revision"    # baseRevision mismatch (absolute)
REASON_STALE_MANIFEST = "stale_manifest"    # payload.manifestVersion mismatch
REASON_STALE_CHAPTER = "stale_chapter"      # chapter-limited TURN, chapter moved
REASON_STALE_REQUEST = "stale_request"      # requestId past the dedup window
REASON_NOT_READY = "not_ready"              # members not READY on this manifest
REASON_NO_WORK = "no_work"                  # no manifest committed yet
REASON_BAD_PHASE = "bad_phase"              # selecting / ended / waiting
REASON_RANGE = "range"                      # model ERR_RANGE (bad page/delta…)
REASON_BAD_ORIGIN = "bad_origin"            # non-leader relaying origin
REASON_NO_INITIAL_PAGE = "no_initial_page"  # OPEN_WORK without a page
REASON_UNAVAILABLE = "unavailable"          # resolver not wired
REASON_PEER_OFFLINE = "peer_offline"        # peer absent / suspended
REASON_NOT_LEADER = "not_leader"
REASON_MALFORMED = "malformed"

# Request types that carry an absolute position/config — optimistic
# concurrency: they must be based on the latest committed revision (#75 §3.4).
# OPEN_WORK is deliberately absent: see the module docstring.
ABSOLUTE_TYPES = frozenset({"GOTO", "SET_OFFSET", "SWAP_SIDES"})

# All request types the sequencer owns (sessions._dispatch routes these here).
SEQUENCED_TYPES = frozenset({
    "TURN", "GOTO", "SET_OFFSET", "SWAP_SIDES", "OPEN_WORK", "READY", "APPLIED",
})

# Role-deterministic tie-break for simultaneous requests on the
# leader-sequenced plane (#75 §3.4): the sequencer's own requests
# (role == "leader") order first, then the follower's, stable within each
# role by arrival index — identical inputs always yield one identical order.
ROLE_RANK = {"leader": 0, "follower": 1}

# Manifest failure code → REJECT/ERROR reason vocabulary (client-facing).
MANIFEST_ERR_REASON = {
    "ERR_FORBIDDEN": "forbidden",
    "ERR_NOT_FOUND": "not_found",
    "ERR_EMPTY_WORK": "empty_work",
    "ERR_BROKEN_ARCHIVE": "broken_archive",
    "ERR_PAGE_NOT_FOUND": "page_not_found",
    "ERR_NO_INITIAL_PAGE": "no_initial_page",
    "ERR_RANGE": "range",
    "ERR_SIZE": "size",
    "ERR_MALFORMED": "malformed",
    "ERR_TIMEOUT": "timeout",
}


class Reject(Exception):
    """Validation failure inside the sequencer — raised before any mutation,
    so a rejected request always leaves the committed snapshot unchanged."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ── phase derivation (§3.3 vocabulary) ─────────────────────────────


def all_ready(session) -> bool:
    """Every member has READYed the committed manifest."""
    members = list(session.members.values())
    return bool(members) and all(m.id in session.ready for m in members)


def derive_phase(session) -> str:
    """Pure phase projection — recomputed for every STATE, never an
    independent source of truth.

    waiting   — pair incomplete (leader alone or join pending approval)
    paused    — a member is missing (link dead) or not active (suspended)
    selecting — paired, but no work committed / leader is browsing the list
    loading   — manifest committed (or in flight) but not all members READY
    reading   — manifest committed, everyone present and READY
    ended     — emitted only as END frames (a deleted session has no phase)
    """
    members = list(session.members.values())
    if session.pending is not None or len(members) != 2:
        return "waiting"
    if any(m.link is None or not m.link.alive or m.status != "active"
           for m in members):
        return "paused"
    if session.selecting or session.manifest is None:
        return "selecting"
    if session.pending_load is not None or not all_ready(session):
        return "loading"
    return "reading"


def snapshot(session) -> dict:
    """The committed snapshot — only values set by committed transitions.
    Embedded verbatim in REJECT frames ("reject + latest snapshot", §3.4)
    and returned to compare against after any would-be mutation."""
    return {
        "sessionId": session.id,
        "revision": session.revision,
        "workEpoch": session.work_epoch,
        "manifestVersion": session.manifest_version,
        "phase": derive_phase(session),
        "spreadId": session.spread_id,
        "offset": session.offset,
        "leaderSide": session.leader_side,
        "dataPlane": session.data_plane,
    }


def plan_from_manifest(manifest: dict) -> list[model.Spread]:
    """Reconstruct the S01 spread objects from a committed manifest's
    serialized spreadTable (used by TURN/GOTO resolution)."""
    plan: list[model.Spread] = []
    for s in manifest["spreadTable"]:
        left = s.get("left")
        plan.append(model.Spread(
            id=s["id"],
            chapter_folder=s["chapterFolder"],
            right=model.PageRef(s["right"]["folder"], s["right"]["name"]),
            left=None if left is None
            else model.PageRef(left["folder"], left["name"]),
        ))
    return plan


def current_index(session) -> int:
    """Index of the committed spread inside session.plan; -1 with no
    committed position."""
    if not session.plan or session.spread_id is None:
        return -1
    return model.spread_index_by_id(session.plan, session.spread_id)


# ── request-frame checks (shared by every sequenced op) ────────────


def check_epoch(session, req: dict) -> None:
    if req["workEpoch"] != session.work_epoch:
        raise Reject(REASON_STALE_EPOCH)


def check_manifest_version(session, payload: dict) -> None:
    mv = payload.get("manifestVersion")
    if mv is not None and mv != session.manifest_version:
        raise Reject(REASON_STALE_MANIFEST)


def check_base(session, req: dict) -> None:
    """Absolute ops carry optimistic concurrency on the revision."""
    if req["baseRevision"] != session.revision:
        raise Reject(REASON_STALE_REVISION)


def gate_page_turn(session) -> str | None:
    """Reject reason for TURN/GOTO under the current derived phase, else
    None. Page input is only legal while ``reading``: a missing/suspended
    peer (paused), unfinished READY gate (loading), and leader browsing /
    no committed work (selecting) each map to their own reason."""
    phase = derive_phase(session)
    if phase == "reading":
        return None
    if phase == "paused":
        return REASON_PEER_OFFLINE
    if phase == "loading":
        return REASON_NOT_READY
    return REASON_NO_WORK if session.manifest is None else REASON_BAD_PHASE


# ── committed transitions (validate first, then mutate) ────────────


def _map_model_error(e: Exception) -> str:
    return {
        "ERR_RANGE": REASON_RANGE,
        "ERR_MALFORMED": REASON_MALFORMED,
        "ERR_SIZE": "size",
    }.get(getattr(e, "code", ""), REASON_MALFORMED)


def apply_turn(session, payload: dict) -> int:
    """TURN: ±1 spread step. Cross-chapter requests clamp globally;
    chapter-limited ones clamp inside the current chapter — and are stale
    (rejected) when the claimed ``fromChapter`` is no longer current."""
    idx = current_index(session)
    if idx < 0:
        raise Reject(REASON_NO_WORK)
    if not payload.get("crossChapter"):
        claimed = payload.get("fromChapter")
        if claimed is not None and claimed != session.plan[idx].chapter_folder:
            raise Reject(REASON_STALE_CHAPTER)
    try:
        nxt = model.turn_index(session.plan, idx, payload["delta"],
                               bool(payload.get("crossChapter")))
    except model.ModelError as e:
        raise Reject(_map_model_error(e))
    session.spread_id = session.plan[nxt].id
    return nxt


def apply_goto(session, payload: dict) -> int:
    """GOTO: the shared spread containing the requested PageRef — a page
    absent from the manifest is a typed error, never a fallback position."""
    if not session.plan:
        raise Reject(REASON_NO_WORK)
    try:
        nxt = model.goto_index(session.plan, payload["page"])
    except model.ModelError as e:
        raise Reject(_map_model_error(e))
    session.spread_id = session.plan[nxt].id
    return nxt


def apply_swap_sides(session, payload: dict) -> str:
    """SWAP_SIDES: shared left/right assignment. Roles and permissions do
    not move (§3.2)."""
    side = payload.get("leaderSide")
    if side not in protocol.SIDES:
        raise Reject(REASON_RANGE)
    session.leader_side = side
    return side


# ── leader-plane sequencing (§3.4) ─────────────────────────────────


def order_batch(pairs) -> list:
    """Deterministic order for a simultaneous (same-tick) request batch on
    the leader-sequenced webrtc-dc plane.

    ``pairs``: iterable of ``(role, request)`` in the order the leader
    observed them. The sequencer's own (leader) requests sort first, then
    the follower's — stable by arrival index inside each role, so every
    node applying this rule to the same batch emits the identical order.
    """
    return [
        pair for _i, pair in sorted(
            enumerate(pairs),
            key=lambda ip: (ROLE_RANK.get(ip[1][0], len(ROLE_RANK)), ip[0]),
        )
    ]
