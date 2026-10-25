"""Wire contract for dual-reader sync frames (#76 / S01).

Parent issue #75 §3.3: versioned frames, transport-agnostic — identical shape
over server-ws or webrtc-dc. Images/binary never ride the data plane.
"""
from __future__ import annotations

import json
import math

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 64 * 1024            # 64KiB per normal frame
MAX_MANIFEST_BYTES = 32 * 1024 * 1024  # 32MiB aggregate manifest chunks

ERR_VERSION = "ERR_VERSION"
ERR_MALFORMED = "ERR_MALFORMED"
ERR_TYPE = "ERR_TYPE"
ERR_RANGE = "ERR_RANGE"
ERR_SIZE = "ERR_SIZE"

REQUEST_TYPES = frozenset({
    "CREATE", "JOIN", "APPROVE", "REJECT", "RESUME", "LEAVE",
    "TURN", "GOTO", "SET_OFFSET", "SWAP_SIDES", "OPEN_WORK",
    "SET_ACTIVITY", "READY", "PING", "APPLIED",
    "SIGNAL", "TRANSPORT_STATE", "UI_SYNC",
})
PHASES = frozenset({"waiting", "loading", "reading", "selecting", "paused", "ended"})
ROLES = frozenset({"leader", "follower"})
SIDES = frozenset({"right", "left"})
SIGNAL_KINDS = frozenset({"offer", "answer", "ice"})
UI_VIEWS = frozenset({"browse"})
UI_MODES = frozenset({"tile", "list", "card"})
MAX_UI_PATH_CHARS = 4096

_REQUEST_KEYS = ("requestId", "sessionId", "workEpoch", "baseRevision", "type", "payload")
_STATE_KEYS = ("sessionId", "revision", "workEpoch", "manifestVersion",
               "phase", "spreadId", "offset", "leaderSide", "presence")


class ProtocolError(ValueError):
    """Typed frame rejection; .code is one of ERR_*."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _fail(code: str, msg: str):
    raise ProtocolError(code, msg)


def parse_frame(raw) -> dict:
    """bytes/str/dict -> dict. Enforces the size cap and the version field."""
    if isinstance(raw, (bytes, bytearray)):
        if len(raw) > MAX_FRAME_BYTES:
            _fail(ERR_SIZE, f"frame exceeds {MAX_FRAME_BYTES} bytes")
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            _fail(ERR_MALFORMED, "frame is not utf-8")
    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
            _fail(ERR_SIZE, f"frame exceeds {MAX_FRAME_BYTES} bytes")
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            _fail(ERR_MALFORMED, "frame is not json")
    if not isinstance(raw, dict):
        _fail(ERR_MALFORMED, "frame must be an object")
    if raw.get("v") != PROTOCOL_VERSION:
        _fail(ERR_VERSION, f"unsupported v={raw.get('v')!r}")
    return raw


def parse_page_ref(x) -> dict:
    if (not isinstance(x, dict)
            or not isinstance(x.get("folder"), str)
            or not isinstance(x.get("name"), str)):
        _fail(ERR_MALFORMED, "PageRef needs {folder:str,name:str}")
    return x


def _require_int(f: dict, key: str) -> int:
    v = f[key]
    if not isinstance(v, int) or isinstance(v, bool):
        _fail(ERR_MALFORMED, f"{key} must be int")
    if v < 0:
        _fail(ERR_RANGE, f"{key} must be >= 0")
    return v


def parse_request(raw) -> dict:
    """Full client-request validation: version, keys, types, ranges."""
    f = parse_frame(raw)
    for k in _REQUEST_KEYS:
        if k not in f:
            _fail(ERR_MALFORMED, f"request missing key: {k}")
    if not isinstance(f["requestId"], str) or not f["requestId"]:
        _fail(ERR_MALFORMED, "requestId must be non-empty str")
    if f["sessionId"] is not None and not isinstance(f["sessionId"], str):
        _fail(ERR_MALFORMED, "sessionId must be str|null")
    _require_int(f, "workEpoch")
    _require_int(f, "baseRevision")
    t = f["type"]
    if not isinstance(t, str):
        _fail(ERR_MALFORMED, "type must be str")
    if t not in REQUEST_TYPES:
        _fail(ERR_TYPE, f"unknown request type: {t}")
    if not isinstance(f["payload"], dict):
        _fail(ERR_MALFORMED, "payload must be an object")
    _validate_payload(t, f["payload"])
    return f


def _validate_payload(t: str, p: dict) -> None:
    if t == "TURN":
        if p.get("delta") not in (1, -1):
            _fail(ERR_RANGE, "TURN.delta must be ±1")
        if not isinstance(p.get("crossChapter"), bool):
            _fail(ERR_MALFORMED, "TURN.crossChapter must be bool")
        fc = p.get("fromChapter")
        if fc is not None and not isinstance(fc, str):
            _fail(ERR_MALFORMED, "TURN.fromChapter must be str|null")
    elif t == "GOTO":
        parse_page_ref(p.get("page"))
    elif t == "SET_OFFSET":
        if p.get("offset") not in (0, 1):
            _fail(ERR_RANGE, "SET_OFFSET.offset must be 0 or 1")
    elif t == "SWAP_SIDES":
        if p.get("leaderSide") not in SIDES:
            _fail(ERR_RANGE, "SWAP_SIDES.leaderSide must be right|left")
    elif t == "JOIN":
        code = p.get("code")
        if not isinstance(code, str) or len(code) != 6 or not code.isdigit():
            _fail(ERR_MALFORMED, "JOIN.code must be a 6-digit str")
    elif t == "OPEN_WORK":
        if not isinstance(p.get("folder"), str):
            _fail(ERR_MALFORMED, "OPEN_WORK.folder must be str")
        if p.get("page") is not None:
            parse_page_ref(p["page"])
    elif t == "SIGNAL":
        if p.get("kind") not in SIGNAL_KINDS:
            _fail(ERR_TYPE, "SIGNAL.kind must be offer|answer|ice")
    elif t == "APPLIED":
        if not isinstance(p.get("requestId"), str):
            _fail(ERR_MALFORMED, "APPLIED.requestId must be str")
    elif t == "UI_SYNC":
        _validate_ui_sync(p)


def _validate_ui_sync(p: dict) -> None:
    """Ephemeral mirrored-UI payload (browse scroll/position).

    Relayed peer-to-peer like SIGNAL — never sequenced, so it must not
    carry reader state. ``top``/``viewH`` are px in the sender's scroller;
    the receiver derives its own target from them (e.g. follower shows
    ``top + viewH``).
    """
    if p.get("view") not in UI_VIEWS:
        _fail(ERR_TYPE, "UI_SYNC.view must be 'browse'")
    path = p.get("path")
    if path is not None and (not isinstance(path, str)
                             or len(path) > MAX_UI_PATH_CHARS):
        _fail(ERR_MALFORMED, "UI_SYNC.path must be str|null")
    top = p.get("top")
    if (not isinstance(top, (int, float)) or isinstance(top, bool)
            or not math.isfinite(top) or top < 0):
        _fail(ERR_RANGE, "UI_SYNC.top must be finite >= 0")
    view_h = p.get("viewH")
    if (not isinstance(view_h, (int, float)) or isinstance(view_h, bool)
            or not math.isfinite(view_h) or view_h <= 0 or view_h > 100000):
        _fail(ERR_RANGE, "UI_SYNC.viewH must be finite 0<h<=100000")
    mode = p.get("mode")
    if mode is not None and mode not in UI_MODES:
        _fail(ERR_RANGE, "UI_SYNC.mode must be tile|list|card")
    seq = p.get("seq")
    if seq is not None and (not isinstance(seq, int) or isinstance(seq, bool)
                            or seq < 0):
        _fail(ERR_RANGE, "UI_SYNC.seq must be int >= 0")


def validate_state(raw) -> dict:
    """Server state frame validation."""
    f = parse_frame(raw)
    for k in _STATE_KEYS:
        if k not in f:
            _fail(ERR_MALFORMED, f"state missing key: {k}")
    _require_int(f, "revision")
    _require_int(f, "workEpoch")
    _require_int(f, "manifestVersion")
    if f["phase"] not in PHASES:
        _fail(ERR_TYPE, f"unknown phase: {f['phase']}")
    if f["leaderSide"] not in SIDES:
        _fail(ERR_TYPE, f"unknown leaderSide: {f['leaderSide']}")
    if f["offset"] not in (0, 1):
        _fail(ERR_RANGE, "offset must be 0 or 1")
    if f["spreadId"] is not None and not isinstance(f["spreadId"], str):
        _fail(ERR_MALFORMED, "spreadId must be str|null")
    if not isinstance(f["presence"], dict):
        _fail(ERR_MALFORMED, "presence must be an object")
    return f


def encode_frame(obj: dict) -> bytes:
    """json-encode a frame; reject anything over the per-frame cap."""
    b = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(b) > MAX_FRAME_BYTES:
        _fail(ERR_SIZE, f"frame exceeds {MAX_FRAME_BYTES} bytes")
    return b
