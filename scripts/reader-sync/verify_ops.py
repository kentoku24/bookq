"""S11 (#86) — scenario op interpreter on the REAL wire protocol.

One op grammar, executed by the runner against the two fixture clients
(and, where a device input is declared, through the active driver). The
fixture drives the production ``PairClient`` end-to-end, so every op is
the real protocol: CREATE/JOIN/APPROVE/READY/TURN/GOTO/SET_OFFSET/
SWAP_SIDES/OPEN_WORK/APPLIED/SET_ACTIVITY/RESUME/LEAVE/SIGNAL/
TRANSPORT_STATE.

Every converging op asserts the spec's full predicate, not just a page
number (spec: 「同じページ番号になった」だけでなく、2台が正しい左右画像を
表示し復帰できることを自動証明する):

- identical ``sessionId`` / ``workEpoch`` / applied ``revision``
- the committed ``spreadId`` and each member's expected side + PageRef
  (the server-built manifest's own spread table)
- the rendered marker (DOM page name + side) AND the assigned image's
  decode state (``ok`` or defined ``blank``/``error`` — never a false
  "synced" display)
- zero unexpected sequenced command frames on the peer's wire
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from verify_core import (
    AssertError,
    EnvError,
    UnsupportedError,
    expected_page,
    marker_rgb,
    png_marker_fraction,
    rtt_stats,
    spread_table_index,
    BLANK_LABEL,
    BLANK_RGB,
)
from verify_drivers import BystanderPair
from verify_fixture_server import CmdError

# sequenced wire frames that count as "a request" — a peer echoing any of
# these after a remote apply is the dup/missing-request bug the spec hunts.
OP_WIRE_TYPES = frozenset(
    {"TURN", "GOTO", "SET_OFFSET", "SWAP_SIDES", "OPEN_WORK"})

_CMD_FOR_OP = {"turn": "turn", "goto": "goto", "swap_sides": "swap",
               "set_offset": "set_offset", "open_work": "open_work"}


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


class RunCtx:
    """Per-run context handed to every op."""

    def __init__(self, *, fx, driver, clients, ws_url, base_url,
                 evidence_dir: Path, surface: str = "fixture",
                 dev_container: str | None = None,
                 tscale: float = 1.0):
        self.fx = fx
        self.driver = driver
        self.clients = clients        # {"leader": ClientSpec, "follower": ClientSpec}
        self.ws_url = ws_url
        self.base_url = base_url
        self.dir = evidence_dir
        self.surface = surface
        self.dev_container = dev_container
        # poll-timeout multiplier — simulator webkit is measurably slower
        # than desktop chromium (image decode, manifest apply, AX taps).
        self.tscale = tscale
        self.assertions: list[dict] = []
        self.screenshots: list[str] = []
        self.rtt: dict = {}
        self.latency: dict = {}
        self.scenario: dict = {}
        self._table: list | None = None   # summary spreads [{id,r,l}]
        self._leader_side: str = "left"

    # -- recording ----------------------------------------------------

    def note(self, name: str, ok: bool, detail: str = "") -> bool:
        self.assertions.append({"name": name, "ok": bool(ok),
                                "detail": str(detail)[:900]})
        return ok

    def fail(self, name: str, detail: str):
        self.note(name, False, detail)
        raise AssertError(f"{name}: {detail}")

    # -- fixture plumbing ----------------------------------------------

    # read-only commands a transient control-channel stall may safely retry
    _IDEMPOTENT_CMDS = frozenset(
        {"state", "code", "value", "rect", "last_tap", "pull_trace",
         "ctl_plane", "noop"})

    def cmd(self, role: str, name: str, params: dict | None = None,
            timeout: float = 30.0):
        if self.surface != "fixture":
            raise UnsupportedError(
                f"cmd '{name}' needs the fixture control surface — "
                "the ui surface is driven via ui_input/open_url/expect ops")
        cid = self.clients[role].id
        tries = 2 if name in self._IDEMPOTENT_CMDS else 1
        err = None
        for attempt in range(tries):
            try:
                return self.fx.cmd(cid, name, params, timeout=timeout)
            except CmdError as e:
                err = e
                if e.kind != "env":
                    break            # assertion-class error — no retry
                if attempt + 1 < tries:
                    # a stalled /next long-poll can starve one delivery;
                    # the page's poll loop self-heals within ~1s of it.
                    time.sleep(1.0)
        if err.kind == "env":
            raise EnvError(str(err)) from err
        raise AssertError(f"cmd {name}@{role}: {err}") from err

    def state(self, role: str) -> dict:
        if self.surface == "ui":
            return self._ui_state(role)
        return self.cmd(role, "state", timeout=15)

    def poll(self, role: str, pred, timeout: float = 12.0,
             interval: float = 0.15, what: str = "state"):
        deadline = time.monotonic() + timeout * self.tscale
        last = None
        while time.monotonic() < deadline:
            try:
                last = self.state(role)
            except (AssertError, EnvError):
                last = None
            if last is not None and pred(last):
                return last
            time.sleep(interval)
        raise AssertError(
            f"poll timeout ({timeout}s) waiting for {what} on {role}; "
            f"last={json.dumps(last, ensure_ascii=False)[:300] if last else None}")

    def poll_both(self, pred, timeout: float = 12.0, what: str = "state"):
        deadline = time.monotonic() + timeout * self.tscale
        la = fa = None
        while time.monotonic() < deadline:
            try:
                la = self.state("leader")
            except (AssertError, EnvError):
                la = None
            try:
                fa = self.state("follower")
            except (AssertError, EnvError):
                fa = None
            if la is not None and fa is not None and pred(la, fa):
                return la, fa
            time.sleep(0.15)
        raise AssertError(
            f"poll timeout ({timeout}s) waiting for {what}; "
            f"leader={json.dumps(la, ensure_ascii=False)[:250] if la else None} "
            f"follower={json.dumps(fa, ensure_ascii=False)[:250] if fa else None}")

    # -- ui-surface observation ----------------------------------------
    # chrome reads PairController/PairedView over page.evaluate; surfaces
    # without JS-eval (iOS Simulator Safari / PWA webclip) get the same
    # shape through the in-page ?pair_report=<cid> hook, which POSTs the
    # raw {pc,pv,solo} to the same-origin test endpoint (#142).

    def _ui_state(self, role: str) -> dict:
        spec = self.clients[role]
        if spec.page is not None:
            d = spec.page.evaluate(
                """() => {
                  const pc = window.PairController
                    ? window.PairController.state : {status: 'missing'};
                  const pv = window.PairedView
                    ? window.PairedView.state() : null;
                  return {pc: pc, pv: pv,
                          solo: !(pv && pv.active)};
                }""")
        else:
            d = self._pair_report_state(role)
        return _normalize_ui_state(d)

    def _pair_report_state(self, role: str) -> dict:
        cid = self.clients[role].id
        try:
            d = _api_json(self, "/api/v1/pair-report?id=" + cid,
                          timeout=10)
        except Exception as e:
            raise AssertError(
                f"pair-report[{cid}]: endpoint read failed: {e}")
        st = (d or {}).get("state")
        if st is None:
            raise AssertError(
                f"pair-report[{cid}]: no report yet — is the page open "
                "with ?pair_report= ?")
        spec = self.clients[role]
        # A tab-focus loss (openurl reusing a stray same-host tab,
        # Safari parking the bookq page in the background) freezes the
        # page's JS — reports go stale. Refocus the bookq tab once per
        # ~25s; never while a suspend op froze this client's net on
        # purpose (spec.net_frozen) — staleness IS the test there.
        age = time.time() - float(st.get("received_at") or 0)
        if age < 15:
            spec._refocus_n = 0
        elif (not getattr(spec, "net_frozen", False)
              and time.monotonic() - getattr(spec, "_refocus_at", 0) > 25):
            spec._refocus_at = time.monotonic()
            spec._refocus_n = getattr(spec, "_refocus_n", 0) + 1
            self.note("ui_refocus", True,
                      f"{role} report stale {age:.0f}s — refocus "
                      f"attempt {spec._refocus_n}")
            from verify_drivers import _simctl
            try:
                if spec._refocus_n >= 3:    # openurl keeps missing the tab
                    _simctl("terminate", spec.udid, "com.apple.mobilesafari",
                            check=False)
                    time.sleep(1.0)
                _simctl("openurl", spec.udid, spec.launch_url)
            except Exception:
                pass
        return st


def _report_fresh(ctx: "RunCtx", role: str, max_age: float) -> bool:
    """True when the client's pair-report is live (posted within
    max_age seconds). Goes through _pair_report_state so a stale
    report also gets the refocus/terminate heal's side effect."""
    try:
        st = ctx._pair_report_state(role)
    except AssertError:
        return False
    try:
        return time.time() - float(st.get("received_at") or 0) <= max_age
    except (TypeError, ValueError):
        return False


def _normalize_ui_state(d: dict) -> dict:
    """{pc,pv,solo} (from page.evaluate or the pair-report hook) -> the
    canonical state dict every ui-surface assert consumes."""
    pc, pv = d.get("pc") or {}, d.get("pv") or {}
    return {
        "status": pc.get("status"), "phase": pc.get("phase"),
        "sessionId": pc.get("sessionId"), "role": pc.get("role")
            or (pv or {}).get("role"),
        "side": pc.get("side") or (pv or {}).get("side"),
        "transport": pc.get("transport"),
        "spreadId": pc.get("spreadId") or (pv or {}).get("spreadId"),
        "revision": pc.get("revision") if pc.get("revision") is not None
            else (pv or {}).get("revision"),
        "workEpoch": pc.get("workEpoch") if pc.get("workEpoch") is not None
            else (pv or {}).get("workEpoch"),
        "manifestVersion": pc.get("manifestVersion")
            or (pv or {}).get("manifestVersion"),
        "presence": pc.get("presence") or {},
        "pending": pc.get("joinRequest"),
        "error": pc.get("error"), "endReason": pc.get("endReason"),
        "applied": {"index": (pv or {}).get("spreadIndex", -1),
                    "spreadId": (pv or {}).get("spreadId"),
                    "leaderSide": (pv or {}).get("leaderSide"),
                    "total": (pv or {}).get("spreadTotal"),
                    "revision": pc.get("revision")
                        if pc.get("revision") is not None
                        else (pv or {}).get("revision")},
        "view": {"ref": (pv or {}).get("ref"),
                 "anchorRef": (pv or {}).get("anchorRef"),
                 "side": (pv or {}).get("side"),
                 "active": (pv or {}).get("active"),
                 "blank": (pv or {}).get("blank")},
    }


# ────────────────────────── shared asserts ───────────────────────────


def _chapter_of(spread_id: str) -> int:
    try:
        return int(str(spread_id).split(":")[0][1:])
    except (ValueError, IndexError):
        return -1


def _turn_target(table: list[dict], idx: int, delta: int,
                 cross_chapter: bool) -> int:
    """model.turn_index over the summary table — the chapter index is
    inside each spread id (c{K}:s{N}) so no folder names are needed."""
    if not table:
        return -1
    target = idx + delta
    if cross_chapter:
        return max(0, min(target, len(table) - 1))
    ch = _chapter_of(table[idx]["id"])
    lo = idx
    while lo > 0 and _chapter_of(table[lo - 1]["id"]) == ch:
        lo -= 1
    hi = idx
    while hi + 1 < len(table) and _chapter_of(table[hi + 1]["id"]) == ch:
        hi += 1
    return max(lo, min(target, hi))


def _find_spread_with_page(table: list[dict], name: str) -> int:
    for i, s in enumerate(table):
        if s.get("r") == name or s.get("l") == name:
            return i
    return -1


def _need_table(ctx: RunCtx, what: str) -> None:
    if not ctx._table:
        raise UnsupportedError(f"{what}: no work open (needs open_work)")


def _committed_same(a: dict, b: dict) -> bool:
    aa, bb = a["applied"], b["applied"]
    return (aa["spreadId"] == bb["spreadId"] and aa["spreadId"] is not None
            and aa["revision"] == bb["revision"]
            and a["sessionId"] == b["sessionId"] and a["sessionId"]
            and a["workEpoch"] == b["workEpoch"])


def _assert_pair_ids(ctx: RunCtx, l: dict, f: dict, what: str) -> None:
    ctx.note("session_same", l["sessionId"] == f["sessionId"]
             and l["sessionId"] is not None, f"sid match ({what})")
    ctx.note("epoch_same", l["workEpoch"] == f["workEpoch"],
             f"workEpoch={l['workEpoch']}")
    ctx.note("revision_same",
             l["applied"]["revision"] == f["applied"]["revision"],
             f"rev={l['applied']['revision']}")
    ctx.note("spread_same",
             l["applied"]["spreadId"] == f["applied"]["spreadId"],
             f"spreadId={l['applied']['spreadId']}")
    ctx.note("no_error", not l.get("error") and not f.get("error")
             and not l.get("endReason") and not f.get("endReason"),
             "no error/endReason on either client")


def _assert_member_view(ctx: RunCtx, st: dict, role: str,
                        table_role: str | None = None) -> None:
    """Marker + image-decode assert for ONE device — the spec's
    左右PageRef期待値 + 画像decode成功またはblank表示."""
    if ctx.surface == "ui":
        v = st.get("view") or {}
        ctx.note(f"side_{role}", v.get("side") in ("right", "left"),
                 f"side={v.get('side')}")
        if v.get("blank"):
            ctx.note(f"blank_{role}", True, "blank side rendered")
        else:
            ref = v.get("ref")
            ctx.note(f"ref_{role}", bool(ref and ref.get("name")),
                     f"ref={bool(ref)}")
        return
    applied = st["applied"]
    exp = expected_page(ctx._table, applied["spreadId"], st["role"],
                        applied.get("leaderSide") or "left")
    want = exp["page"] or BLANK_LABEL
    m = st["marker"]
    ctx.note(f"marker_{role}",
             m["page"] == want and m["side"] == exp["side"],
             f"side={m['side']} page={m['page']} (want {want})")
    img = m.get("imgState")
    if want == BLANK_LABEL:
        ctx.note(f"img_{role}", img == "blank",
                 f"blank side, imgState={img}")
    else:
        if img != "ok":
            # decode races the marker by design — poll briefly for the
            # img to finish loading before calling it a failure.
            try:
                st2 = ctx.poll(role, lambda s: (
                    s.get("marker") or {}).get("imgState") == "ok",
                    timeout=6, what=f"img decode {want}")
                img = (st2.get("marker") or {}).get("imgState")
            except AssertError:
                pass
        ctx.note(f"img_{role}", img == "ok",
                 f"decoded /image/{want} imgState={img}")


def _wire_snap(ctx: RunCtx, role: str) -> int:
    """Snapshot of a client's sentTypes length — a peer-wire cleanliness
    baseline for the next committed op."""
    if ctx.surface != "fixture":
        return 0
    try:
        return len(ctx.cmd(role, "state", timeout=10).get("sentTypes") or [])
    except (AssertError, EnvError):
        return 0


def _wire_assert(ctx: RunCtx, role: str, snap: int,
                 what: str = "remote apply", allowed: int = 0) -> None:
    """No UNEXPECTED sequenced frames on the wire after the snapshot —
    TURN/GOTO/SET_OFFSET/SWAP_SIDES/OPEN_WORK beyond ``allowed`` (the
    count the role legitimately issued, e.g. its own concurrent subs)
    would be an echo/loop bug; APPLIED/READY are housekeeping."""
    if ctx.surface != "fixture":
        return
    try:
        st = ctx.cmd(role, "state", timeout=10)
    except (AssertError, EnvError):
        return
    tail = (st.get("sentTypes") or [])[snap:]
    seq = [t for t in tail if t in OP_WIRE_TYPES]
    ctx.note(f"wire_clean_{role}", len(seq) <= allowed,
             f"{what}: sequenced {seq[-12:]} (allowed {allowed})")


def _dc_relay(ctx: RunCtx, actor: str) -> bool:
    """§3.4 leader-sequenced plane: while the effective data plane is
    webrtc-dc, a follower's sequenced request travels over the data
    channel and the LEADER relays it verbatim (+payload.origin) through
    its own control channel — so the leader legitimately emits the frame
    and the follower's WS wire shows nothing. sentTypes only counts WS
    sends, so this shifts the allowance leader-wards."""
    if actor != "follower" or ctx.surface != "fixture":
        return False
    try:
        return ctx.state("follower").get("transport") == "webrtc-dc"
    except (AssertError, EnvError):
        return False


def _converge_index(ctx: RunCtx, expect_idx: int, what: str,
                    timeout: float = 15.0) -> tuple:
    """Poll until both clients applied the expected spread — then assert
    the whole spec predicate (ids, revision, markers, images)."""
    _need_table(ctx, what)
    expect_idx = max(0, min(expect_idx, len(ctx._table) - 1))
    want_id = ctx._table[expect_idx]["id"]
    l, f = ctx.poll_both(
        lambda a, b: a["applied"]["spreadId"] == want_id
        and b["applied"]["spreadId"] == want_id
        and _committed_same(a, b),
        timeout=timeout, what=what)
    ctx.note("converged", True,
             f"{want_id} rev={l['applied']['revision']}")
    _assert_pair_ids(ctx, l, f, what)
    for st, role in ((l, "leader"), (f, "follower")):
        _assert_member_view(ctx, st, role)
    return l, f


def _converge_any(ctx: RunCtx, what: str, min_rev: int = 0,
                  timeout: float = 15.0) -> tuple:
    """Poll until both clients applied the SAME committed state (id+rev)
    without fixing a target — for ops whose landing spot is server-chosen
    (SET_OFFSET's anchor, OPEN_WORK's initial page, concurrent races).

    ``min_rev`` guards the premature-match race: with ``expected_index:
    "any"`` both devices already satisfy _committed_same on the pre-burst
    state, so without a floor the poll returns before a single request
    commits and commit_count reads +0. Once a matching state at rev >=
    min_rev appears, require it to hold across ~0.9s of polls so a
    mid-burst sample isn't mistaken for the settled outcome."""
    deadline = time.monotonic() + timeout * ctx.tscale
    settle_need = 0.9 * ctx.tscale
    last_key = None
    last_change = time.monotonic()
    l = f = None
    while time.monotonic() < deadline:
        try:
            l = ctx.state("leader")
            f = ctx.state("follower")
        except (AssertError, EnvError):
            l = f = None
        now = time.monotonic()
        if (l is not None and f is not None
                and _committed_same(l, f)
                and l["phase"] == "reading" and f["phase"] == "reading"
                and l["applied"]["revision"] >= min_rev):
            key = (l["applied"]["spreadId"], l["applied"]["revision"],
                   f["applied"]["revision"])
            if key != last_key:
                last_key = key
                last_change = now
            elif now - last_change >= settle_need:
                ctx.note("converged", True,
                         f"{l['applied']['spreadId']} "
                         f"rev={l['applied']['revision']}")
                _assert_pair_ids(ctx, l, f, what)
                return l, f
        time.sleep(0.15)
    raise AssertError(
        f"poll timeout ({timeout}s) waiting for {what}; "
        f"leader={json.dumps(l, ensure_ascii=False)[:250] if l else None} "
        f"follower={json.dumps(f, ensure_ascii=False)[:250] if f else None}")


def _capture_table(ctx: RunCtx) -> None:
    """ctx._table = the committed manifest's own spread table — the
    ground truth every expectation derives from (never a runner guess)."""
    if ctx.surface != "fixture":
        return
    st = ctx.state("leader")
    man = st.get("manifest") or {}
    spreads = man.get("spreads")
    if not spreads:
        raise AssertError("open_work: leader reports no manifest spreads")
    ctx._table = spreads
    ctx.note("manifest_table", True,
             f"{len(spreads)} spreads from committed manifest "
             f"(mv={st.get('manifestVersion')})")


def _expect_match(st: dict, op: dict) -> bool:
    """All declared expect fields must match — unlisted fields are free."""
    if "phase" in op and st.get("phase") != op["phase"]:
        return False
    if "status" in op and st.get("status") != op["status"]:
        return False
    if "transport" in op and st.get("transport") != op["transport"]:
        return False
    if "spreadId" in op and st["applied"]["spreadId"] != op["spreadId"]:
        return False
    if "error" in op:
        # live client error is transient — it clears on the next applied
        # STATE; lastError is the fixture's sticky record of the last
        # 'error' event reason. Match either.
        if op["error"] not in (st.get("error") or "",
                               st.get("lastError") or ""):
            return False
    if "endReason" in op and (st.get("endReason") or "") != op["endReason"]:
        return False
    if "solo" in op:
        solo = not st.get("sessionId") or st.get("endReason") is not None
        if bool(op["solo"]) != solo:
            return False
    if "presence" in op:
        for m in st.get("presence", {}).values():
            if m.get("status") != op["presence"]:
                return False
    if "applied_index" in op and st["applied"]["index"] != op["applied_index"]:
        return False
    if "rejected" in op:
        if not any(op["rejected"] in (r or "")
                   for r in (st.get("rejected") or [])):
            return False
    mk = st.get("marker") or {}
    if "marker_page" in op and mk.get("page") != op["marker_page"]:
        return False
    if "marker_side" in op and mk.get("side") != op["marker_side"]:
        return False
    if "img_state" in op and mk.get("imgState") != op["img_state"]:
        return False
    if "dc" in op and bool(st.get("dcOpen")) != bool(op["dc"]):
        return False
    return True


# ────────────────────────── ops ──────────────────────────────────────


def op_pair(ctx: RunCtx, op: dict) -> None:
    """CREATE -> code -> JOIN -> APPROVE -> presence=2 on both sides."""
    if ctx.surface == "ui":
        return _op_pair_ui(ctx, op)
    ctx.cmd("leader", "connect")
    ctx.cmd("follower", "connect")

    if op.get("via") == "taps":
        # real device taps on the fixture's DOM controls (simulator path):
        # tap 開始 -> read the shown code -> type it on the peer -> tap 参加
        # -> leader taps 承認.
        _tap_element(ctx, ctx.clients["leader"], "fx-create")
        # CREATE is a real round-trip — the code lands when the ACK does,
        # so poll rather than racing a single read on a slow device.
        code = None
        deadline = time.monotonic() + 10 * ctx.tscale
        while time.monotonic() < deadline:
            code = (ctx.cmd("leader", "code") or {}).get("code")
            if isinstance(code, str) and len(code) == 6:
                break
            code = None
            time.sleep(0.4)
        ctx.note("leader_created", isinstance(code, str) and len(code) == 6,
                 "code displayed after 開始 tap")
        if not isinstance(code, str) or len(code) != 6:
            ctx.fail("pair", f"no 6-digit code after fx-create tap "
                             f"({code!r}) — typing it would be garbage")
        fsp = ctx.clients["follower"]
        # typing on iOS needs a focused field — a calibration-miss tap
        # leaves the code field empty and 参加 would JOIN "" (or nothing
        # if the tap itself missed). Verify the value actually landed,
        # re-focusing and retyping once before declaring failure.
        entered = False
        for _attempt in range(2):
            _tap_element(ctx, fsp, "fx-code-input")
            ctx.driver.input_text(fsp, code)
            time.sleep(0.6)
            try:
                got = ctx.cmd("follower", "value",
                              {"id": "fx-code-input"}, timeout=10)
            except (AssertError, EnvError):
                got = {}
            if (got or {}).get("v") == code:
                entered = True
                break
        ctx.note("code_entered", entered,
                 f"fx-code-input holds {got.get('v')!r}")
        # typing opens the keyboard and shifts Safari's layout — a neutral
        # center tap (delta=0 zone) dismisses it, then re-calibrate.
        drv = ctx.driver
        w, h = drv.screen_size(fsp)
        drv.tap(fsp, w / 2, h / 2)
        time.sleep(1.0)
        # First verify the click handler actually ran — a tap swallowed by
        # a dismissing keyboard hits the DOM but never fires click, so
        # poll the follower's OWN wire (sentTypes/status) rather than only
        # the leader's pending flag, and retap while nothing was sent.
        def _join_left(s):
            if "JOIN" in (s.get("sentTypes") or []):
                return True
            if s.get("status") in ("joining", "paired") or \
                    s.get("phase") == "waiting":
                return True
            if s.get("error") or s.get("lastError"):
                raise AssertError(
                    f"join click ran but errored: "
                    f"{s.get('error') or s.get('lastError')}")
            return False
        sent = False
        last_err = None
        for _attempt in range(3):
            _tap_element(ctx, fsp, "fx-join")
            try:
                ctx.poll("follower", _join_left,
                         timeout=8, what="join dispatch")
                sent = True
                break
            except AssertError as e:
                last_err = e
                if "errored" in str(e):
                    raise
        ctx.note("join_dispatched", sent,
                 f"follower sent JOIN after "
                 f"{_attempt + 1} tap(s){'' if sent else f' ({last_err})'}")
        if not sent:
            ctx.fail("pair", f"fx-join tap never dispatched JOIN: {last_err}")
        ctx.poll("leader", lambda s: s["pending"], timeout=10,
                 what="join_request")
        ctx.note("join_request_seen", True, "leader saw the pending member")
        _tap_element(ctx, ctx.clients["leader"], "fx-approve")
    else:
        created = ctx.cmd("leader", "create")
        code = created.get("code")      # secret — used once, never asserted
        ctx.note("leader_created", created.get("role") == "leader",
                 "CREATE ack role=leader")
        ctx.cmd("follower", "join", {"code": code})
        ctx.poll("leader", lambda s: s["pending"], what="join_request")
        ctx.note("join_request_seen", True, "leader saw the pending member")
        ctx.cmd("leader", "approve")

    l, f = ctx.poll_both(
        lambda a, b: len(a["presence"]) == 2 and len(b["presence"]) == 2,
        what="paired presence")
    ctx.note("paired_both", True, "presence=2 on both clients")
    ctx.note("session_same",
             l["sessionId"] is not None and l["sessionId"] == f["sessionId"],
             "sessionId identical")
    roles = {m["role"] for m in l["presence"].values()}
    ctx.note("roles", roles == {"leader", "follower"}, f"roles={roles}")
    ctx.note("plane_ws", l["transport"] in (None, "server-ws"),
             f"dataPlane={l['transport']}")


def _op_pair_ui(ctx: RunCtx, op: dict) -> None:
    """Real-app pairing UI surface — the PRODUCTION gesture (#142):
    sidebar launcher (#pair-btn, in #sidebar-header) → one-tap
    connect() → MATCH auto-join, or the code-entry view reached through
    an ended panel. Chrome drives the DOM; the simulator driver walks
    the Safari AX tree."""
    via = op.get("via") or "match"
    lsp, fsp = ctx.clients["leader"], ctx.clients["follower"]
    if ctx.driver.name == "simulator":
        # a prior suspend test can leave Safari backgrounded — bring it
        # forward or every coordinate tap lands on SpringBoard.
        for spec in (lsp, fsp):
            try:
                ctx.driver.foreground(spec)
            except Exception:
                pass
    # 'match' must start clean — a leftover session hijacks MATCH.
    # 'code' instead NEEDS the connected session a prior pair op made
    # (its flow reaches the join view via an ended panel).
    if via != "code":
        _ui_ensure_idle(ctx)
    if lsp.page is not None and fsp.page is not None:
        return _op_pair_ui_dom(ctx, via)
    if ctx.driver.name == "simulator":
        return _op_pair_ui_ax(ctx, via)
    raise UnsupportedError("ui pair needs chrome DOM or simulator AX")


def _ui_ensure_idle(ctx: RunCtx) -> None:
    """Leftover sessions from a previous scenario/run hijack MATCH —
    end any live session on both clients before a fresh pair op."""
    lsp, fsp = ctx.clients["leader"], ctx.clients["follower"]
    if lsp.page is not None and fsp.page is not None:
        for p in (lsp.page, fsp.page):
            p.evaluate(
                "() => { const c = window.PairController;"
                "  if (c && !['idle','ended'].includes(c.state.status))"
                "    c.leave(); }")
        for p in (lsp.page, fsp.page):
            p.wait_for_function(
                "() => !window.PairController"
                " || ['idle','ended','error'].includes("
                "      PairController.state.status)", timeout=15000)
        return
    if ctx.driver.name == "simulator":
        # a live-but-stranded session can sit in ANY panel section —
        # leader-waiting (#pair-leader-cancel), follower-waiting
        # (#pair-waiting-cancel), session (#pair-leave) or error
        # (#pair-error-leave). Tap whichever cancel is mounted, via the
        # status strip opener when the panel itself is closed.
        _CANCEL_NEEDLES = ["解除", "#pair-leader-cancel",
                           "#pair-waiting-cancel", "#pair-error-leave",
                           "#pair-join-back"]
        for spec in (lsp, fsp):
            deadline = time.monotonic() + 20 * ctx.tscale
            while time.monotonic() < deadline:
                try:
                    st = (ctx._pair_report_state(spec.role)
                          .get("pc") or {}).get("status")
                except AssertError:
                    st = None
                if st in (None, "idle", "ended"):
                    break
                try:
                    _ui_tap(ctx, spec, _CANCEL_NEEDLES, "cancel (stale)",
                            tries=3, delay=0.4)
                except EnvError:
                    pass
                time.sleep(0.8)
            else:
                raise EnvError(
                    f"ensure-idle: {spec.role} stuck at status={st!r}")
            _ui_close_panel(ctx, spec)


def _ui_close_panel(ctx: RunCtx, spec, timeout: float = 8.0) -> None:
    """Leave the pair panel shut — a still-open overlay swallows the next
    launcher tap as a backdrop close() and connect() never fires."""
    deadline = time.monotonic() + timeout * ctx.tscale
    while time.monotonic() < deadline:
        try:
            rep = ctx._pair_report_state(spec.role)
        except AssertError:
            time.sleep(0.5)
            continue
        rects = rep.get("rects") or {}
        r = (rects.get("#pair-close") or rects.get("#pair-error-close"))
        if not (r and r.get("w")):
            return
        try:
            _ui_tap(ctx, spec, ["#pair-close", "#pair-error-close"],
                    "panel close", tries=3, delay=0.4)
        except EnvError:
            return
        time.sleep(0.5)


def _wait_code_dom(page, timeout: float = 15000) -> str:
    page.wait_for_function(
        "() => /^\\d{6}$/.test((document.getElementById('pair-code-value')"
        "  .textContent || '').trim())", timeout=timeout)
    return page.text_content("#pair-code-value").strip()


def _open_sidebar_dom(page) -> None:
    """The launcher lives in #sidebar-header — a closed sidebar puts it
    outside the viewport (that was the original #pair-btn env_unavailable:
    the old op closed the sidebar first and could never click it)."""
    page.evaluate(
        "() => { const s = document.getElementById('sidebar');"
        "  const v = document.getElementById('viewer');"
        "  if (s) { s.classList.add('open');"
        "    v && v.classList.add('sidebar-open'); } }")


def _op_pair_ui_dom(ctx: RunCtx, via: str) -> None:
    leader = ctx.clients["leader"].page
    follower = ctx.clients["follower"].page
    has = leader.evaluate("() => !!window.PairController")
    if not has:
        raise UnsupportedError(
            "ui surface requires the PairController wiring (#84)")
    _open_sidebar_dom(leader)
    _open_sidebar_dom(follower)
    if via == "code":
        # The code-entry view is reachable in production ONLY from an
        # 'ended' panel's choice section — an idle/ended launcher tap
        # fires connect() and MATCH intercepts it. Deterministic reach:
        # follower opens its panel (secSession), leader leaves through
        # the real UI, then connect()s for a fresh code; the follower's
        # still-open panel falls back to secChoice → 参加 → code entry.
        follower.click("#pair-status")
        leader.click("#pair-status")
        leader.click("#pair-leave")
        leader.click("#pair-btn")
        code = _wait_code_dom(leader)
        follower.click("#pair-join-open")
        follower.fill("#pair-code-input", code)
        # sanitizeCode auto-submits on the 6th digit — no submit click.
    elif via == "match":
        leader.click("#pair-btn")          # open() + connect()
        code = _wait_code_dom(leader)
        follower.click("#pair-btn")        # MATCH joins + auto-approves
    else:
        raise UnsupportedError(f"pair via {via!r}")
    for p in (leader, follower):
        p.wait_for_function(
            "() => document.getElementById('pair-status')"
            "  .dataset.status === 'connected'", timeout=15000)
    ctx.note("paired_both", True, f"UI-dom({via}): both chips connected")


# ── simulator real-input path ─────────────────────────────────────────
# Safari exposes only NATIVE chrome in the AX tree — web content has no
# AX labels, so tap_element can never hit page elements. The
# ?pair_report= hook posts each known control's client rect plus the
# last pointerdown; the runner calibrates screen->client once (same idea
# as the fixture's _stable_offset) and taps real coordinates.

_PAIR_LAUNCHER = ["2台で見開き"]
_STRIP_NEEDLES = ["接続済", "相手不在", "再接続中", "読み込み中"]

_NEEDLE_SEL = {
    "2台で見開き": "#pair-btn",
    "接続済": "#pair-status", "相手不在": "#pair-status",
    "再接続中": "#pair-status", "読み込み中": "#pair-status",
    "解除": "#pair-leave", "左右交換": "#pair-swap",
    "参加": "#pair-join-open", "コードで参加する": "#pair-leader-join",
    "6桁のコードを入力": "#pair-code-input",
    "コードを入力": "#pair-code-input",
    "フォルダ一覧": "#menu-btn", "閉じる": "#pair-close",
    "見開きペアリング切替": "#spread-shift",
    "次のページ": "#zone-next", "前のページ": "#zone-prev",
}
# parents that must be open for a control to exist — the rect tapped when
# the target itself is absent (sidebar → menu, pair panel → strip).
# Every chain is one level — _ui_tap fires at most one opener per poll,
# so ["#pair-status", "#menu-btn", "#pair-btn"] means: strip when it has
# a real rect, else the フォルダ一覧 drawer, then the 🔗 launcher — the
# launcher is open()-only while status is non-idle.
_OPENERS = {
    "#pair-btn": ["#menu-btn"],
    "#pair-status": ["#menu-btn"],
    "#pair-swap": ["#pair-status", "#menu-btn", "#pair-btn"],
    "#pair-leave": ["#pair-status", "#menu-btn", "#pair-btn"],
    "#pair-join-open": ["#pair-status", "#menu-btn", "#pair-btn"],
    "#pair-code-input": ["#pair-join-open", "#pair-status", "#menu-btn",
                         "#pair-btn"],
    "#pair-leader-cancel": ["#pair-status", "#menu-btn", "#pair-btn"],
    "#pair-waiting-cancel": ["#pair-status", "#menu-btn", "#pair-btn"],
    "#pair-error-leave": ["#pair-status", "#menu-btn", "#pair-btn"],
    "#pair-join-back": ["#pair-status", "#menu-btn", "#pair-btn"],
    "#browse-header": ["#menu-btn"],
}


def _ui_offset(ctx: RunCtx, spec) -> tuple:
    """Screen->client offset: probe the page top until a pointerdown
    registers in the hook, then cache the delta on the ctx."""
    off_map = getattr(ctx, "_ui_off", None)
    if off_map is None:
        off_map = ctx._ui_off = {}
    if spec.role in off_map:
        return off_map[spec.role]
    drv = ctx.driver
    w, h = drv.screen_size(spec)
    cy = 60.0
    while cy < h * 0.45:
        t0 = int(time.time() * 1000)
        drv.tap(spec, w / 2, cy)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            try:
                lt = (ctx._pair_report_state(spec.role).get("lastTap")
                      or {})
            except AssertError:
                lt = {}
            if lt.get("t", 0) >= t0 - 200 and lt.get("x") is not None:
                off = (w / 2 - float(lt["x"]), cy - float(lt["y"]))
                off_map[spec.role] = off
                ctx.note("ui_offset", True,
                         f"{spec.role} offset=({off[0]:.0f},{off[1]:.0f})")
                return off
            time.sleep(0.25)
        cy += 24.0
    raise EnvError(f"ui offset: no pointerdown reached the page "
                   f"({spec.role})")


def _ui_rect(rep: dict, needles) -> dict | None:
    """Resolve a needle to a client rect — a known control selector, a
    raw '#sel' needle, or a browse tile (data-path / display name)."""
    rects = rep.get("rects") or {}
    tiles = rep.get("tiles") or []
    for n in needles:
        sel = _NEEDLE_SEL.get(n) or (n if n.startswith("#") else None)
        r = rects.get(sel) if sel else None
        if r is None:
            for t in tiles:
                if n == t.get("path") or n in (t.get("name") or ""):
                    r = t["rect"]
                    break
        if r and r["w"] > 0 and r["h"] > 0 and \
                r["x"] + r["w"] > 0 and r["y"] + r["h"] > 0 and \
                r.get("top") is not False:
            return r
    return None


def _ui_tap(ctx: RunCtx, spec, needles, what: str,
            tries: int = 16, delay: float = 0.7) -> str:
    """Tap a real UI control on the simulator: resolve the needle to a
    client rect via the pair-report hook, open a parent surface when the
    control isn't mounted yet, tap the calibrated screen point."""
    if not isinstance(needles, (list, tuple)):
        needles = [needles]
    drv = ctx.driver
    off = None
    prev_r = None
    fired = {}
    last_rep = None
    for it in range(tries):
        try:
            rep = ctx._pair_report_state(spec.role)
        except AssertError:
            # page not loaded yet / hook not armed — keep polling, a
            # miss after all tries below is the real EnvError.
            time.sleep(delay)
            continue
        r = _ui_rect(rep, needles)
        if r is not None:
            vp_h = float((rep.get("vp") or {}).get("h") or 0)
            if vp_h and (r["y"] < 0 or r["y"] + r["h"] > vp_h):
                # drag just far enough to center the tile — a fixed full
                # swipe overshoots and ping-pongs past the fold forever.
                need = (r["y"] + r["h"] / 2) - vp_h * 0.5
                sw, sh = drv.screen_size(spec)
                dist = min(max(abs(need), sh * 0.15), sh * 0.55)
                if need > 0:
                    drv.swipe(spec, sw / 2, sh * 0.65,
                              sw / 2, sh * 0.65 - dist, 0.5)
                else:
                    drv.swipe(spec, sw / 2, sh * 0.35,
                              sw / 2, sh * 0.35 + dist, 0.5)
                prev_r = None
                time.sleep(delay)
                continue
            # settle: only tap once the rect holds still across two
            # consecutive reports — a momentum scroll or an async
            # layout shift (pair-session-info reflow) would tap a stale
            # coordinate and land on a different control.
            if prev_r is None or abs(r["y"] - prev_r["y"]) >= 3 \
                    or abs(r["x"] - prev_r["x"]) >= 3:
                prev_r = r
                time.sleep(delay)
                continue
            if off is None:
                off = _ui_offset(ctx, spec)
            x = r["x"] + r["w"] / 2 + off[0]
            y = r["y"] + r["h"] / 2 + off[1]
            drv.tap(spec, x, y)
            ctx.note(f"ui_tap_{needles[0]}", True,
                     f"{what} @({x:.0f},{y:.0f})")
            return needles[0]
        prev_r = None
        last_rep = rep
        rects = rep.get("rects") or {}
        # fire ONE opener per poll, and each opener at most once per ~5
        # polls — #menu-btn is a toggle, so re-firing every iteration
        # would flap the sidebar; but a tap that missed (overlay race,
        # mid-animation) must get to try again or the call is doomed.
        for n in needles:
            sel = _NEEDLE_SEL.get(n) or (n if n.startswith("#") else None)
            for op_sel in (_OPENERS.get(sel) or []):
                if it - fired.get(op_sel, -99) < 5:
                    continue
                orr = rects.get(op_sel)
                if orr and orr["w"] > 0 and orr.get("top") is not False:
                    fired[op_sel] = it
                    if off is None:
                        off = _ui_offset(ctx, spec)
                    drv.tap(spec, orr["x"] + orr["w"] / 2 + off[0],
                            orr["y"] + orr["h"] / 2 + off[1])
                    break
            else:
                continue
            break
        time.sleep(delay)
    # dump the page context that made the needle unresolvable — the
    # failure mode differs entirely between 'inside a subfolder',
    # 'reader covering the grid' and 'page dead / stale report'.
    _pc = (last_rep or {}).get("pc") or {}
    _tiles = [t.get("path") or (t.get("name") or "")[:16]
              for t in ((last_rep or {}).get("tiles") or [])][:10]
    _age = (time.time() - float((last_rep or {}).get("received_at") or 0)
            if last_rep else -1)
    raise EnvError(
        f"ui tap: {what} not found on screen (needles={needles}) "
        f"[phase={_pc.get('phase')} status={_pc.get('status')} "
        f"hdr={(last_rep or {}).get('browseHeader')!r} "
        f"reader={(last_rep or {}).get('readerView')} "
        f"tiles={_tiles} repAge={_age:.0f}s]")


def _ui_browse_root(ctx: RunCtx, spec, pops: int = 10) -> bool:
    """Get the page back to the ROOT browse grid.

    Three distinct states can block a work-tile tap:
    - drill-down: browseHeader reads '↩ <name>' — tap it to pop a level
    - reader view: after leave the page keeps showing the solo reader
      (#/view/…); the browse grid is unmounted so tiles vanish even
      though browseHeader still reports the folder state. Mounted
      #zone-prev rects mean the reader is on screen. The escape path
      is a same-document hash nav to #/browse/ — the in-app path
      (sidebar root header) is a no-op at root because it only moves
      the browse tree, never the view mode.
    - a stray pair modal over the grid — close it on the way out."""
    for _ in range(pops):
        try:
            rep = ctx._pair_report_state(spec.role)
        except AssertError:
            rep = {}
        hdr = rep.get("browseHeader") or ""
        in_reader = bool(rep.get("readerView"))
        if in_reader and spec.kind == "safari":
            from verify_drivers import _simctl
            _simctl("openurl", spec.udid,
                    (spec.launch_url or "").split("#")[0] + "#/browse/")
            time.sleep(1.2)
            continue
        if "フォルダ" in hdr:
            _ui_close_panel(ctx, spec)
            return True
        try:
            _ui_tap(ctx, spec, ["#browse-header"], "browse up",
                    tries=5, delay=0.5)
        except EnvError:
            return False
        time.sleep(0.6)
    return False


def _wait_code_ui(ctx: RunCtx, spec, timeout: float = 25.0) -> str:
    """The session code from PairController.state (canonical) — NOT the
    #pair-code-value DOM text, which can still show the previous
    session's digits when the panel re-opens."""
    deadline = time.monotonic() + timeout * ctx.tscale
    while time.monotonic() < deadline:
        try:
            c = ((ctx._pair_report_state(spec.role).get("pc") or {})
                 .get("code") or "")
        except AssertError:
            c = ""
        c = str(c).strip()
        if len(c) == 6 and c.isdigit():
            return c
        time.sleep(0.6)
    raise EnvError("pair code never appeared on the leader")


def _op_pair_ui_ax(ctx: RunCtx, via: str) -> None:
    lsp, fsp = ctx.clients["leader"], ctx.clients["follower"]
    if via == "code":
        # Same deterministic reach as the DOM path: follower panel open
        # (secSession) → leader ends via the real UI → leader reconnects
        # for a fresh code → follower panel is on secChoice → 参加.
        _ui_tap(ctx, fsp, _STRIP_NEEDLES, "status strip (follower)")
        _ui_tap(ctx, lsp, _STRIP_NEEDLES, "status strip (leader)")
        _ui_tap(ctx, lsp, ["解除"], "leave button (leader)")
        _ui_close_panel(ctx, lsp)
        _ui_tap(ctx, lsp, _PAIR_LAUNCHER, "pair launcher (leader)")
        code = _wait_code_ui(ctx, lsp)
        _ui_tap(ctx, fsp, ["参加"], "join open (follower)")
        _ui_tap(ctx, fsp, ["6桁のコードを入力", "コードを入力"],
                "code field (follower)")
        # idb ui text types into whatever holds focus — if the field
        # lost focus (panel re-render, soft-keyboard timing) the digits
        # go nowhere. Watch joinCode and retype until the 6th digit
        # lands (sanitizeCode auto-submits at length 6).
        typed = ""
        for _ in range(6):
            ctx.driver.input_text(fsp, code)
            time.sleep(1.2)
            try:
                rep = ctx._pair_report_state(fsp.role)
            except AssertError:
                rep = {}
            typed = rep.get("joinCode")
            st = (rep.get("pc") or {}).get("status")
            if st == "connected" or (typed is not None and len(typed) >= 6):
                break
            if typed is None:
                # joinCode null = the field UNMOUNTED — sanitizeCode
                # auto-submits at 6 digits, so a vanished field means
                # JOIN already fired; let the paired poll arbitrate.
                break
            try:
                _ui_tap(ctx, fsp, ["6桁のコードを入力", "コードを入力"],
                        "code field (follower, retry)", tries=4, delay=0.5)
            except EnvError:
                # field unmounted between report and tap — submitted.
                break
        ctx.note("code_typed", True,
                 f"joinCode={'submitted' if typed is None else 'len=' + str(len(typed))}")
    elif via == "match":
        # No code read needed — the launcher's connect() either creates
        # a waiting session or MATCH-joins an existing one; the poll
        # below is the assert. A still-open overlay eats the launcher
        # tap as a backdrop close, so shut any stray panel first.
        for spec in (lsp, fsp):
            _ui_close_panel(ctx, spec)
        _ui_tap(ctx, lsp, _PAIR_LAUNCHER, "pair launcher (leader)")
        _ui_tap(ctx, fsp, _PAIR_LAUNCHER, "pair launcher (follower)")
    else:
        raise UnsupportedError(f"pair via {via!r}")
    ctx.poll_both(
        lambda a, b: a["status"] == "connected"
        and b["status"] == "connected",
        timeout=30, what="paired via real UI")
    ctx.note("paired_both", True, f"UI-sim({via}): both connected")
    for spec in (lsp, fsp):
        _ui_close_panel(ctx, spec)


def _api_json(ctx: RunCtx, path: str, timeout: float = 10.0) -> object:
    with urllib.request.urlopen(
            ctx.base_url.rstrip("/") + path, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _resolve_work(ctx: RunCtx, decl) -> dict:
    """op/scenario work forms:
    - ``{discover: true, minPages}`` — pick a real folder off the target
      env's own tree via /api/folders + /api/images (the folder name stays
      inside the run; only page names echo into evidence)
    - ``{folder, page:{folder,name}, offset?}`` — an explicit env work
    Named fx fixtures are synthetic-table declarations — S11 exercises the
    REAL OPEN_WORK path, so they are rejected with guidance."""
    if isinstance(decl, str):
        raise UnsupportedError(
            f"named work {decl!r} is a synthetic-table declaration — "
            "S11 scenarios declare env works against the target tree")
    if not isinstance(decl, dict):
        raise UnsupportedError("work declaration must be an object")
    if decl.get("discover"):
        return _discover_work(ctx, int(decl.get("minPages", 6)),
                              decl.get("offset"),
                              int(decl.get("index", 0)))
    return decl


def _discover_work(ctx: RunCtx, min_pages: int, offset,
                   index: int = 0) -> dict:
    """Find a folder with >= min_pages images on the target env. Leaf
    folders sort naturally; the ``index``-th qualifying one wins —
    deterministic across runs of the same env without ever naming it in
    a declaration (``index`` 1+ lets a scenario switch to a DIFFERENT
    work, the spec's leader作品変更, still without naming folders)."""
    # /api/folders rescans the target tree on every request — under load
    # or on a cold post-restart container a single 25s read can time out
    # transiently, which is NOT proof the environment is down. A few
    # patient attempts before declaring env.
    folders = last_err = None
    for attempt in range(4):
        try:
            folders = _api_json(ctx, "/api/folders", timeout=25)
            break
        except Exception as e:
            last_err = e
            time.sleep(3.0)
    if folders is None:
        raise EnvError(
            f"work discovery: /api/folders failed: {last_err}")
    cands = []

    def walk(node, path):
        if not isinstance(node, dict):
            return
        name = node.get("name", "")
        p = f"{path}/{name}".strip("/") if path else name
        if node.get("type") == "folder":
            # (path, tile label): the browse tile shows node.name, which
            # flattening can rewrite to "parent/child" — keep it for the
            # AX tap path.
            cands.append((p, name))
            for ch in node.get("children") or []:
                if isinstance(ch, dict) and ch.get("type") == "folder":
                    walk(ch, p)

    for top in folders if isinstance(folders, list) \
            else (folders or {}).get("folders") or []:
        walk(top, "")
    cands.sort()
    for folder, tile in cands:
        try:
            imgs = _api_json(
                ctx, "/api/images?folder=" +
                urllib.parse.quote(folder, safe=""))
        except Exception:
            continue
        raw = imgs if isinstance(imgs, list) \
            else (imgs or {}).get("images") or []
        names = [i if isinstance(i, str)
                 else (i.get("name") if isinstance(i, dict) else None)
                 for i in raw]
        names = [n for n in names if n]
        if len(names) >= min_pages:
            if index <= 0:
                return {"folder": folder, "tile": tile,
                        "page": {"folder": folder,
                                 "name": sorted(names)[0]},
                        "offset": offset}
            index -= 1
    raise EnvError(
        f"work discovery: no folder with >= {min_pages} pages on the "
        "target env")


def op_open_work(ctx: RunCtx, op: dict) -> None:
    """Real OPEN_WORK on the target env's tree -> manifest -> READY ->
    phase reading on both, initial spread markers + image decode."""
    work = _resolve_work(ctx, op.get("work") or ctx.scenario.get("work"))
    if not isinstance(work, dict) or not work.get("folder"):
        raise UnsupportedError(
            "open_work needs an env work {folder, page:{folder,name}}")
    page = work.get("page")
    if not isinstance(page, dict) or not page.get("name"):
        raise UnsupportedError("env work needs page {folder,name}")
    if ctx.surface == "ui":
        return _op_open_work_ui(ctx, work)
    ctx.cmd("leader", "open_work",
            {"folder": work["folder"], "page": page,
             "offset": work.get("offset")})
    if op.get("phase"):
        # fault-injection declaration: the work is expected NOT to reach
        # reading on the named actor (e.g. a corrupted/dropped manifest)
        actor = op.get("phase_actor", "leader")
        st = ctx.poll(actor, lambda s: s["phase"] == op["phase"],
                      timeout=float(op.get("timeout", 15)),
                      what=f"phase={op['phase']} on {actor}")
        ctx.note("work_stalled", True,
                 f"{actor} parked at phase={st['phase']} as injected")
        return
    l, f = ctx.poll_both(
        lambda a, b: a["phase"] == "reading" and b["phase"] == "reading",
        timeout=40, what="phase=reading after OPEN_WORK")
    _capture_table(ctx)
    # the committed spread must contain the declared initial page — the
    # server's own initial.spreadId choice, verbatim.
    spread_id = l["applied"]["spreadId"]
    idx = spread_table_index(ctx._table, spread_id)
    if idx < 0:
        ctx.fail("initial_spread",
                 f"committed {spread_id} not in manifest table")
    spread = ctx._table[idx]
    names = {spread.get("r"), spread.get("l")}
    ctx.note("initial_page", page["name"] in names,
             f"committed spread {spread_id} holds {page['name']}")
    _assert_pair_ids(ctx, l, f, "open_work")
    for st, role in ((l, "leader"), (f, "follower")):
        _assert_member_view(ctx, st, role)


def _op_open_work_ui(ctx: RunCtx, work: dict) -> None:
    lsp = ctx.clients["leader"]
    page = work["page"]
    if lsp.page is not None:
        lsp.page.evaluate(
            "([folder, page]) => window.ViewerNavigation.request("
            "  {type: 'openWork', folder: folder, page: page,"
            "  via: 'runner'})",
            [work["folder"], page])
    else:
        # simulator: tap the work's browse tile — match the display
        # name OR the full data-path (flattened names like
        # "parent/child" don't survive in .tile-name-text). A tap that
        # misses (overlay, late layout shift) gets one retry.
        needles = [n for n in (work.get("tile") or "", work["folder"])
                   if n]
        # a leftover session/reader leaves the browse grid inside some
        # subfolder — pop back to root so the work tile is reachable.
        _ui_browse_root(ctx, lsp)
        _ui_tap(ctx, lsp, needles, f"work tile {work.get('tile')}")
        reading = (
            lambda a, b: a["phase"] == "reading"
            and b["phase"] == "reading"
            and a["applied"]["spreadId"])
        try:
            l, f = ctx.poll_both(
                reading, timeout=25, what="ui phase=reading (first tap)")
        except AssertError:
            # the reader leaves the browse DOM the moment it mounts —
            # a slow manifest apply on real WebKit looks exactly like
            # this. Grace-poll before blaming the tap: re-tapping when
            # the open is already in flight is guaranteed-doomed.
            try:
                l, f = ctx.poll_both(
                    reading, timeout=20,
                    what="ui phase=reading (open in flight)")
            except AssertError:
                _ui_tap(ctx, lsp, needles,
                        f"work tile retry {work.get('tile')}")
                l, f = ctx.poll_both(
                    reading, timeout=30, what="ui phase=reading")
        ctx.note("ui_open_work", True,
                 f"spreadId={l['applied']['spreadId']} "
                 f"rev={l['applied']['index']}")
        return
    l, f = ctx.poll_both(
        lambda a, b: a["phase"] == "reading" and b["phase"] == "reading"
        and a["applied"]["spreadId"],
        timeout=40, what="ui phase=reading")
    ctx.note("ui_open_work", True,
             f"spreadId={l['applied']['spreadId']} "
             f"rev={l['applied']['index']}")


def _send_move(ctx: RunCtx, op: dict) -> dict:
    """Issue a move op on either surface (fixture cmd or ui intent).
    Returns the cmd result — ``{sent: False}`` means the client's own
    phase/role/link gate refused (observable, not an error)."""
    actor = op.get("actor", "leader")
    t = op["op"]
    params = {k: v for k, v in op.items()
              if k not in ("op", "actor", "expect_reject", "rev_delta",
                           "expected_index", "expected_commits",
                           "turn_delta")}
    if ctx.surface == "ui":
        page = ctx.clients[actor].page
        if page is None:
            return _send_move_ax(ctx, actor, op)
        if t == "turn":
            ok = page.evaluate(
                "([d, cc]) => window.ViewerNavigation.request({type: 'turn',"
                " delta: d, crossChapter: cc, fromChapter: null,"
                " via: 'runner'})",
                [int(op.get("delta", 1)), bool(op.get("crossChapter", True))])
        elif t == "goto":
            ok = page.evaluate(
                "(p) => window.ViewerNavigation.request({type: 'goto',"
                " page: p, via: 'runner'})",
                op.get("page") or {})
        elif t == "swap_sides":
            ok = page.evaluate("() => window.PairController.swapSides()")
        elif t == "set_offset":
            ok = page.evaluate(
                "(o) => window.ViewerNavigation.request("
                "  {type: 'setOffset', offset: o, via: 'runner'})",
                int(op["offset"]))
        return {"sent": bool(ok)}
    return ctx.cmd(actor, _CMD_FOR_OP[t], params) or {"sent": True}


def _send_move_ax(ctx: RunCtx, actor: str, op: dict) -> dict:
    """ui surface on the simulator driver: REAL device input — edge-zone
    taps for TURN (the paired view's tap zones drive the intent), chrome
    button taps for offset, the pair panel for side swap."""
    t = op["op"]
    spec = ctx.clients[actor]
    if t == "turn":
        w, h = ctx.driver.screen_size(spec)
        d = int(op.get("delta", 1))
        n = min(abs(d), 10)
        # tapForward decides which edge is "forward" — 'left' (the manga
        # default) maps delta+1 to the LEFT zone, 'right' to the right.
        try:
            rep = ctx._pair_report_state(spec.role)
        except AssertError:
            rep = {}
        fwd = rep.get("tapForward") or "left"
        forward_left = (fwd == "left")
        want_left = forward_left if d > 0 else not forward_left
        sel = "#zone-prev" if want_left else "#zone-next"
        r = ((rep.get("rects") or {}).get(sel)) or {}
        if r.get("w"):
            x = r["x"] + r["w"] / 2
            y = r["y"] + r["h"] / 2
            off = _ui_offset(ctx, spec)
            x, y = x + off[0], y + off[1]
        else:
            x = 16.0 if want_left else (w - 16.0)
            y = h * 0.5
        for _ in range(n):
            ctx.driver.tap(spec, x, y)
            time.sleep(0.3)
        return {"sent": True}
    if t == "swap_sides":
        _ui_tap(ctx, spec, _STRIP_NEEDLES, "status strip")
        _ui_tap(ctx, spec, ["左右交換"], "swap button")
        return {"sent": True}
    if t == "set_offset":
        # #spread-shift toggles the offset (0<->1) — the chrome button
        # is the real input path; scenario uses offset:1 for the toggle.
        _ui_tap(ctx, spec, ["見開きペアリング切替"], "spread-shift button")
        return {"sent": True}
    raise UnsupportedError(f"ui-ax move {t!r} has no device gesture")


def _assert_reject_or_blocked(ctx: RunCtx, op: dict, send_res: dict,
                              stay_spread: str | None) -> None:
    """A gated move: either the client's own gate refused (sent=false —
    the READING_GATED path, e.g. peer suspended) or the wire answered
    REJECT(reason). Either way the committed spread must NOT move."""
    want = op.get("expect_reject") or True
    actor = op.get("actor", "leader")
    if send_res.get("sent") is False:
        ctx.note("turn_rejected", True,
                 "client-side gate refused the intent (peer paused/phase)")
        st = ctx.state(actor)
        if stay_spread:
            ctx.note("spread_unchanged",
                     st["applied"]["spreadId"] == stay_spread,
                     f"stayed {stay_spread}")
        return
    st = ctx.poll(actor, lambda s: any(
        str(want) in (r or "") for r in (s.get("rejected") or [])),
        timeout=15, what=f"REJECT {want}")
    st2 = ctx.state(actor)
    ctx.note("turn_rejected", True, f"wire REJECT {want}")
    if stay_spread:
        ctx.note("spread_unchanged",
                 st2["applied"]["spreadId"] == stay_spread,
                 f"stayed {stay_spread}")


def op_turn(ctx: RunCtx, op: dict) -> None:
    actor = op.get("actor", "leader")
    delta = int(op.get("delta", 1))
    peer = "follower" if actor == "leader" else "leader"
    if ctx.surface == "ui":
        return _op_turn_ui(ctx, op)
    _need_table(ctx, "turn")
    cur = ctx.state(actor)["applied"]["index"]
    if cur < 0:
        ctx.fail("turn", "no committed spread index")
    rev0 = ctx.state(actor)["applied"]["revision"]
    target = _turn_target(ctx._table, cur, delta,
                          bool(op.get("crossChapter", True)))
    snap = _wire_snap(ctx, peer)
    res = _send_move(ctx, op)
    if op.get("expect_reject"):
        stay = ctx._table[max(0, min(cur, len(ctx._table) - 1))]["id"]
        _assert_reject_or_blocked(ctx, op, res, stay)
        return
    l, f = _converge_index(ctx, target, f"turn {delta:+d} by {actor}")
    _wire_assert(ctx, peer, snap, allowed=1 if _dc_relay(ctx, actor) else 0)
    if "rev_delta" in op:
        # dedup check: a wire-duplicated request must still commit ONCE.
        got = l["applied"]["revision"] - rev0
        ctx.note("rev_delta", got == int(op["rev_delta"]),
                 f"revision +{got} (want {op['rev_delta']})")
    ctx.note("turn_ok", l["applied"]["spreadId"] == f["applied"]["spreadId"],
             f"spreadId={l['applied']['spreadId']}")


def _op_turn_ui(ctx: RunCtx, op: dict) -> None:
    actor = op.get("actor", "leader")
    delta = int(op.get("delta", 1))
    before = ctx.state(actor)["applied"]
    cur, total = before["index"], before.get("total") or 0
    if cur < 0:
        ctx.fail("turn", "ui: no committed spread index")
    target = cur + delta
    if total:
        target = max(0, min(target, total - 1))
    _send_move(ctx, op)
    l, f = ctx.poll_both(
        lambda a, b: a["applied"]["index"] == target
        and b["applied"]["index"] == target and _committed_same(a, b),
        timeout=15, what=f"ui turn {delta:+d} by {actor}")
    ctx.note("converged", True,
             f"ui index={target} rev={l['applied'].get('revision')}")
    _assert_pair_ids(ctx, l, f, "turn")
    _assert_ui_views(ctx, l, f)


def _assert_ui_views(ctx: RunCtx, l: dict, f: dict) -> None:
    """Left/right image correctness on the real viewer: opposite sides,
    the same anchor ref, each ref matching its projected side, and the
    assigned page image actually decoded in the DOM."""
    lv, fv = l.get("view") or {}, f.get("view") or {}
    lside = lv.get("side")
    ctx.note("sides_opposite",
             lside in ("right", "left")
             and (f.get("view") or {}).get("side") ==
             ("left" if lside == "right" else "right"),
             f"leader={lside} follower={(f.get('view') or {}).get('side')}")
    ctx.note("anchor_same", bool(lv.get("anchorRef")) and
             (lv.get("anchorRef") or {}).get("name") ==
             ((fv.get("anchorRef") or {}).get("name")),
             "same spread anchor on both devices")
    for role, st in (("leader", l), ("follower", f)):
        spec = ctx.clients[role]
        if spec.page is None:
            # no DOM-eval on the simulator — member-view asserts
            # (side/anchor/ref/blank) + screenshots are the evidence.
            continue
        ok = spec.page.evaluate(
            """() => {
              const pv = window.PairedView ? window.PairedView.state() : null;
              if (!pv || !pv.active) return {ok: false, why: 'inactive'};
              if (pv.ref === null) return {ok: pv.blank === true, why: 'blank'};
              const want = encodeURIComponent(pv.ref.name);
              const imgs = [...document.querySelectorAll('img')]
                .filter(i => (i.getAttribute('src') || '').endsWith(want));
              return {ok: imgs.some(i => i.complete && i.naturalWidth > 0),
                      why: 'img=' + imgs.length};
            }""")
        ctx.note(f"img_{role}", bool(ok.get("ok")),
                 f"{role}: {ok.get('why')}")


def op_goto(ctx: RunCtx, op: dict) -> None:
    actor = op.get("actor", "leader")
    peer = "follower" if actor == "leader" else "leader"
    _need_table(ctx, "goto")
    pidx = -1
    if isinstance(op.get("index"), int):
        target = max(0, min(op["index"], len(ctx._table) - 1))
        # exact addressing: page names repeat across chapters, so a bare
        # name can resolve to the wrong chapter's spread — the table's
        # pageRefs index cannot. Fall back to the name only when the
        # spread has no right page (blank-side edge spreads use li).
        entry = ctx._table[target]
        pidx = entry.get("ri") if entry.get("ri") is not None else -1
        if pidx < 0:
            pidx = entry.get("li") if entry.get("li") is not None else -1
        name = entry.get("r") or entry.get("l")
    elif isinstance(op.get("page"), str):
        target = _find_spread_with_page(ctx._table, op["page"])
        if target < 0:
            ctx.fail("goto", f"page {op['page']!r} not in manifest")
        name = op["page"]
    else:
        raise UnsupportedError("goto needs index or page")
    if ctx.surface == "ui":
        raise UnsupportedError(
            "goto on the ui surface needs a PageRef the committed manifest "
            "doesn't expose — turn/goto coverage lives on the fixture")
    snap = _wire_snap(ctx, peer)
    cur = ctx.state(actor)["applied"].get("spreadId")
    res = ctx.cmd(actor, "goto",
                  {"pageIndex": pidx} if isinstance(op.get("index"), int)
                  and pidx >= 0 else {"page": name})
    if op.get("expect_reject"):
        _assert_reject_or_blocked(ctx, op, res or {"sent": True}, cur)
        return
    _converge_index(ctx, target, f"goto idx={target} by {actor}")
    _wire_assert(ctx, peer, snap, allowed=1 if _dc_relay(ctx, actor) else 0)


def op_swap_sides(ctx: RunCtx, op: dict) -> None:
    actor = op.get("actor", "leader")
    if ctx.surface == "ui":
        before = ctx.state(actor)["applied"].get("leaderSide") or "left"
        want = "left" if before == "right" else "right"
        _send_move(ctx, op)
        l, f = ctx.poll_both(
            lambda a, b: a["applied"].get("leaderSide") == want
            and b["applied"].get("leaderSide") == want
            and _committed_same(a, b),
            timeout=20, what="ui sides swapped")
        ctx.note("sides_swapped", True, f"leaderSide={want}")
        _assert_pair_ids(ctx, l, f, "swap_sides")
        for st, role in ((l, "leader"), (f, "follower")):
            _assert_member_view(ctx, st, role)
        return
    _need_table(ctx, "swap_sides")
    cur = ctx.state(actor)["applied"].get("spreadId")
    before = ctx.state(actor)["applied"].get("leaderSide") or "left"
    want = "left" if before == "right" else "right"
    res = _send_move(ctx, op)
    if op.get("expect_reject"):
        _assert_reject_or_blocked(ctx, op, res, cur)
        return
    l, f = ctx.poll_both(
        lambda a, b: a["applied"].get("leaderSide") == want
        and b["applied"].get("leaderSide") == want
        and _committed_same(a, b),
        timeout=15, what="sides swapped")
    ctx.note("sides_swapped", True, f"leaderSide={want}")
    _assert_pair_ids(ctx, l, f, "swap_sides")
    for st, role in ((l, "leader"), (f, "follower")):
        _assert_member_view(ctx, st, role)


def op_set_offset(ctx: RunCtx, op: dict) -> None:
    """SET_OFFSET rebuilds the manifest (new manifestVersion + table) —
    the committed spread keeps the right-page anchor."""
    actor = op.get("actor", "leader")
    want = int(op["offset"])
    _need_table(ctx, "set_offset")
    mv0 = ctx.state(actor).get("manifestVersion") or 0
    _send_move(ctx, op)
    l, f = ctx.poll_both(
        lambda a, b: (a.get("manifestVersion") or 0) > mv0
        and _committed_same(a, b) and a["phase"] == "reading"
        and b["phase"] == "reading",
        timeout=40, what=f"offset={want} manifest rebuilt")
    _capture_table(ctx)
    ctx.note("offset_applied", True,
             f"manifestVersion={l['manifestVersion']} "
             f"spreads={len(ctx._table)}")
    _assert_pair_ids(ctx, l, f, "set_offset")
    for st, role in ((l, "leader"), (f, "follower")):
        _assert_member_view(ctx, st, role)


def op_concurrent(ctx: RunCtx, op: dict) -> None:
    """Several move requests issued without waiting between them — the
    spec's 同時NEXT/逆方向同時/連打 competitions.

    Wire semantics the expectation encodes: TURN is RELATIVE (applied in
    receive order, never base-checked), GOTO/SET_OFFSET/SWAP_SIDES are
    ABSOLUTE (baseRevision == revision or stale_revision), and OPEN_WORK
    is always accepted (self-contained pick; latest pick wins).
    ``expected_index`` defaults to declared-order application over the
    subs; pass ``"any"`` when a race outcome is legitimately open, and
    ``expected_commits`` (int or list of acceptable ints) when absolute
    ops may be rejected. Either way: one committed state on both devices,
    every accepted request applied exactly once."""
    _need_table(ctx, "concurrent")
    subs = op["ops"]
    start = ctx.state("leader")
    idx = start["applied"]["index"]
    rev0 = start["applied"]["revision"]
    work_change = False
    for s in subs:
        t = s["op"]
        if t == "turn":
            idx = _turn_target(ctx._table, idx, int(s.get("delta", 1)),
                               bool(s.get("crossChapter", True)))
        elif t == "goto":
            if isinstance(s.get("page"), str):
                idx = _find_spread_with_page(ctx._table, s["page"])
            elif isinstance(s.get("index"), int):
                idx = max(0, min(s["index"], len(ctx._table) - 1))
        elif t == "open_work":
            work_change = True
        # swap_sides keeps the index
    expected_index = op.get("expected_index", idx)
    expected_commits = op.get("expected_commits", len(subs))
    snap = {r: _wire_snap(ctx, r) for r in ("leader", "follower")}

    def fire(s):
        params = {k: v for k, v in s.items() if k not in ("op", "actor")}
        if s["op"] == "open_work":
            work = _resolve_work(ctx, s.get("work")
                                 or ctx.scenario.get("work"))
            params = {"folder": work["folder"], "page": work["page"],
                      "offset": work.get("offset")}
        ctx.cmd(s.get("actor", "leader"), _CMD_FOR_OP[s["op"]], params,
                timeout=20)

    with ThreadPoolExecutor(max_workers=len(subs)) as ex:
        list(ex.map(fire, subs))
    if work_change:
        l, f = _converge_any(ctx, "concurrent open_work",
                             min_rev=rev0 + 1)
        _capture_table(ctx)
    elif expected_index == "any":
        l, f = _converge_any(ctx, "concurrent ops",
                             min_rev=rev0 + 1, timeout=20)
    else:
        l, f = _converge_index(ctx, int(expected_index),
                               "concurrent ops", timeout=20)
    rev_delta = l["applied"]["revision"] - rev0
    ok = (rev_delta in expected_commits
          if isinstance(expected_commits, list)
          else rev_delta == expected_commits)
    ctx.note("commit_count", ok,
             f"revisions +{rev_delta} (expected {expected_commits}) "
             "— every accepted request applied exactly once")
    declared = {"leader": 0, "follower": 0}
    for s in subs:
        declared[s.get("actor", "leader")] += 1
    relay = _dc_relay(ctx, "follower") and declared["follower"] > 0
    _wire_assert(ctx, "follower", snap["follower"], "concurrent",
                 allowed=0 if relay else declared["follower"])
    _wire_assert(ctx, "leader", snap["leader"], "concurrent",
                 allowed=declared["leader"] + declared["follower"]
                 if relay else declared["leader"])


def op_burst(ctx: RunCtx, op: dict) -> None:
    """N rapid-fire turns from one side — every request sequenced once,
    the final spreadId/revision equal to the server's committed values."""
    _need_table(ctx, "burst")
    actor = op.get("actor", "leader")
    delta = int(op.get("delta", 1))
    count = int(op["count"])
    start = ctx.state(actor)
    idx, rev0 = start["applied"]["index"], start["applied"]["revision"]
    target = idx
    for _ in range(count):
        target = _turn_target(ctx._table, target, delta,
                              bool(op.get("crossChapter", True)))
    peer = "follower" if actor == "leader" else "leader"
    snap = _wire_snap(ctx, peer)
    for _ in range(count):
        ctx.cmd(actor, "turn", {"delta": delta,
                                "crossChapter": op.get("crossChapter", True)},
                timeout=20)
    l, f = _converge_index(ctx, target, f"burst x{count} by {actor}",
                           timeout=25)
    rev_delta = l["applied"]["revision"] - rev0
    ctx.note("commit_count", rev_delta == count,
             f"revisions +{rev_delta} for {count} turns "
             "(no dup, none missing)")
    _wire_assert(ctx, peer, snap,
                 allowed=count if _dc_relay(ctx, actor) else 0)


def op_latency(ctx: RunCtx, op: dict) -> None:
    """Intent->committed-apply latency distribution, recorded under the
    ACTIVE transport into the result JSON (spec: transport別warm遅延)."""
    actor = op.get("actor", "leader")
    n = int(op.get("samples", 8))
    r = ctx.cmd(actor, "latency", {"samples": n}, timeout=20 + n * 3)
    stats = rtt_stats(r.get("samples") or [])
    transport = ctx.state(actor).get("transport") or "server-ws"
    ctx.latency[transport] = stats
    ctx.note("latency_samples", stats["n"] == n,
             f"{transport}: n={stats['n']} p50={stats['p50']}ms "
             f"p95={stats['p95']}ms")


def op_peer_latency(ctx: RunCtx, op: dict) -> None:
    """S12 (#87) — intent → PEER-decoded-display latency, one clock.

    The spec's real measurement: the RUNNER's monotonic clock spans
    t0 = intent-send cmd to `actor` → wire/DC path → server commit →
    STATE → peer's fxApplyState → peer image decode → the peer's own
    displayed-event POST back to the fixture server → t1. The sample
    includes control-channel delivery and the event's return leg, so it
    is an UPPER bound on the real user-visible latency — exactly what
    the issue asks recorded, per direction per transport.

    Timeouts and intent refusals count as failures; samples are never
    dropped for being slow (spec: 遅いsampleを恣意的に削除しない).
    ``actor`` may be "both" — directions interleave and record under
    separate ``peer:<transport>:<actor>→<peer>`` keys."""
    if ctx.surface != "fixture":
        raise UnsupportedError("peer_latency needs the fixture surface")
    _need_table(ctx, "peer_latency")
    n = int(op.get("samples", 8))
    actor = op.get("actor", "leader")
    wait_s = float(op.get("timeout_ms", 15000)) / 1000.0
    directions = ["leader", "follower"] if actor == "both" else [actor]
    deltas = {d: 1 for d in directions}
    box = {d: {"samples": [], "failures": 0} for d in directions}
    transports = {}

    for _i in range(n):
        for d in directions:
            peer = "follower" if d == "leader" else "leader"
            cid = ctx.clients[peer].id
            # stale-event hygiene: only an event caused by THIS turn may
            # satisfy the wait — drain first, then demand rev > prev.
            ctx.fx.drain_events(cid)
            st = ctx.state(peer)
            prev_rev = int(st["applied"]["revision"])
            transports[d] = st.get("transport") or "server-ws"
            dd = deltas[d]
            deltas[d] = -dd              # ping-pong inside the table —
                                         # warm, never edge-starved
            t0 = time.monotonic()
            try:
                res = ctx.cmd(d, "turn",
                              {"delta": dd, "crossChapter": True},
                              timeout=max(15.0, wait_s))
            except EnvError:
                raise                    # control plane dead — env, not a sample
            except AssertError:
                box[d]["failures"] += 1  # command-level error = refusal
                continue
            if res and res.get("sent") is False:
                box[d]["failures"] += 1  # client gate refused the intent
                continue
            try:
                ctx.fx.wait_displayed(cid, prev_rev + 1, timeout=wait_s)
                box[d]["samples"].append(
                    (time.monotonic() - t0) * 1000.0)
            except Exception:            # noqa: BLE001 — wait timeout
                box[d]["failures"] += 1

    for d in directions:
        peer = "follower" if d == "leader" else "leader"
        tr = transports.get(d) or "server-ws"
        stats = rtt_stats(box[d]["samples"])
        stats["failures"] = box[d]["failures"]
        stats["method"] = ("runner-clock intent->peer-decoded-display "
                           "upper bound (cmd delivery + sync path + peer "
                           "decode + event return)")
        key = f"peer:{tr}:{d}→{peer}"
        ctx.latency[key] = stats
        ctx.note(
            f"peer_latency_{d}",
            stats["n"] > 0 or stats["failures"] == 0,
            f"{key}: n={stats['n']} p50={stats['p50']}ms "
            f"p95={stats['p95']}ms max={stats['max']}ms "
            f"failures={stats['failures']}")


def op_rtt(ctx: RunCtx, op: dict) -> None:
    n = int(op.get("samples", 5))
    actor = op.get("actor", "leader")
    r = ctx.cmd(actor, "rtt", {"samples": n}, timeout=15 + n * 2)
    stats = rtt_stats(r.get("samples") or [])
    ctx.rtt = stats
    ctx.note("rtt_samples", stats["n"] == n,
             f"n={stats['n']} p50={stats['p50']}ms p95={stats['p95']}ms")


def op_expect(ctx: RunCtx, op: dict) -> None:
    actor = op["actor"]
    timeout = float(op.get("timeout", 15))
    st = ctx.poll(actor, lambda s: _expect_match(s, op),
                  timeout=timeout, what=f"expect on {actor}")
    ctx.note("expect", True,
             json.dumps({k: v for k, v in op.items()
                         if k not in ("op", "actor", "timeout")},
                        ensure_ascii=False)[:200])


def op_dc_open(ctx: RunCtx, op: dict) -> None:
    """Real WebRTC: SIGNAL-relayed offer/answer/ice between the two
    clients, then the server-reported dataPlane flip to webrtc-dc."""
    ctx.cmd("leader", "dc_open")
    ctx.cmd("follower", "dc_open")
    try:
        l, f = ctx.poll_both(lambda a, b: a["dcOpen"] and b["dcOpen"],
                             timeout=25, what="datachannel open")
    except AssertError as e:
        raise EnvError(f"webrtc-dc unavailable: {e}") from e
    ctx.note("dc_open_both", True, "RTCDataChannel open on both")
    l, f = ctx.poll_both(
        lambda a, b: a["transport"] == "webrtc-dc"
        and b["transport"] == "webrtc-dc",
        timeout=15, what="dataPlane=webrtc-dc")
    ctx.note("plane_switched", True,
             f"server-reported dataPlane={l['transport']}")


def op_dc_close(ctx: RunCtx, op: dict) -> None:
    for role in ("leader", "follower"):
        ctx.cmd(role, "dc_close")
    l, f = ctx.poll_both(
        lambda a, b: a["transport"] == "server-ws"
        and b["transport"] == "server-ws",
        timeout=15, what="dataPlane back to server-ws")
    ctx.note("plane_restored", True, "server-reported dataPlane=server-ws")


def op_dc_restart(ctx: RunCtx, op: dict) -> None:
    """Re-signal a fresh DataChannel mid-session — the DC re-establishment
    window the spec wants exercised under operation."""
    ctx.cmd("leader", "dc_restart")
    ctx.cmd("follower", "dc_restart")
    l, f = ctx.poll_both(
        lambda a, b: a["dcOpen"] and b["dcOpen"]
        and a["transport"] == "webrtc-dc" and b["transport"] == "webrtc-dc",
        timeout=30, what="datachannel re-established")
    ctx.note("dc_reestablished", True, "fresh DataChannel after drop")


def op_disconnect(ctx: RunCtx, op: dict) -> None:
    actor = op.get("actor", "follower")
    peer = "follower" if actor == "leader" else "leader"
    if ctx.surface == "ui":
        ctx.driver.close_ws(ctx.clients[actor])
    else:
        ctx.cmd(actor, "disconnect")
    ctx.poll(peer, lambda s: any(
        m["role"] == ctx.clients[actor].role and m["status"] == "offline"
        for m in s.get("presence", {}).values()), what="peer offline")
    ctx.note("peer_marked_offline", True, f"{peer} sees {actor} offline")


def op_resume(ctx: RunCtx, op: dict) -> None:
    actor = op.get("actor", "follower")
    peer = "follower" if actor == "leader" else "leader"
    if ctx.surface == "fixture":
        ctx.cmd(actor, "resume", timeout=30)
    # else: the real client's reconnect machine resumes on its own —
    # waiting for it IS the assert.
    st = ctx.poll(peer, lambda s: all(
        m["status"] == "active" for m in s.get("presence", {}).values())
        and len(s.get("presence", {})) == 2,
        timeout=45, what="peer active again")
    ctx.note("resumed", True, "RESUME + presence active")
    l, f = ctx.poll_both(
        lambda a, b: a["sessionId"] == b["sessionId"] and a["sessionId"]
        and _committed_same(a, b),
        timeout=30, what="same committed state after resume")
    ctx.note("session_kept", True, "same sessionId + committed state")
    _assert_pair_ids(ctx, l, f, "resume")
    for st_, role in ((l, "leader"), (f, "follower")):
        _assert_member_view(ctx, st_, role)


def op_resume_fail(ctx: RunCtx, op: dict) -> None:
    actor = op.get("actor", "follower")
    r = ctx.cmd(actor, "resume_bad", {"token": op.get("token", "bogus-token")})
    ctx.note("resume_rejected",
             r.get("type") == "REJECT"
             and r.get("reason") in ("bad_resume", "resume_expired"),
             f"reply={r.get('type')} reason={r.get('reason')}")


def op_fault(ctx: RunCtx, op: dict) -> None:
    actor = op.get("actor", "leader")
    ftype = op["type"]
    params = {k: v for k, v in op.items() if k not in ("op", "actor", "type")}
    r = ctx.cmd(actor, "fault", {"type": ftype, **params})
    ctx.note(f"fault_{ftype}", True, f"injected on {actor}")


def op_image_fault(ctx: RunCtx, op: dict) -> None:
    actor = op.get("actor", "leader")
    ctx.cmd(actor, "img_fault", {"mode": op["mode"], "ms": op.get("ms")})
    ctx.note("image_fault", True,
             f"mode={op['mode']} on {actor} — next render faulted")


def op_suspend(ctx: RunCtx, op: dict) -> None:
    actor = op.get("actor", "follower")
    peer = "follower" if actor == "leader" else "leader"
    if ctx.surface == "ui":
        spec = ctx.clients[actor]
        if spec.page is not None:
            spec.page.evaluate(
                "() => window.PairController.client"
                " && window.PairController.client.setActivity('suspended')")
        else:
            # simulator: backgrounding Safari neither freezes the page
            # nor fires visibilitychange on the sim — the deterministic
            # suspend terminal is a hard WS freeze (SIGSTOP the app's
            # WebKit.Networking): silence -> heartbeat -> peer 'offline'.
            ctx.driver.freeze_net(spec)
    else:
        ctx.cmd(actor, "set_activity", {"state": "suspended"})
    # On a real device the graceful SET_ACTIVITY can lose the race against
    # iOS freezing the page's WS — the peer then learns 'offline' via the
    # 15s heartbeat silence instead. Both are the suspend terminal.
    ctx.poll(peer, lambda s: any(
        m["role"] == ctx.clients[actor].role
        and m["status"] in ("suspended", "offline")
        for m in s.get("presence", {}).values()),
        timeout=35, what=f"{actor} suspended/offline")
    ctx.note("suspended_seen", True, f"{peer} sees {actor} suspended")


def op_activate(ctx: RunCtx, op: dict) -> None:
    actor = op.get("actor", "follower")
    peer = "follower" if actor == "leader" else "leader"
    if ctx.surface == "ui":
        spec = ctx.clients[actor]
        if spec.page is not None:
            spec.page.evaluate(
                "() => window.PairController.client"
                " && window.PairController.client.setActivity('active')")
        else:
            ctx.driver.thaw_net(spec)
            ctx.driver.foreground(spec)
    else:
        ctx.cmd(actor, "set_activity", {"state": "active"})
    ctx.poll(peer, lambda s: all(
        m["status"] == "active" for m in s.get("presence", {}).values())
        and len(s.get("presence", {})) == 2,
        what="all active")
    ctx.note("all_active", True, "presence back to active")


def op_reload(ctx: RunCtx, op: dict) -> None:
    """Real page reload -> stored-credential RESUME — the reload path the
    spec requires (fixture auto-resumes on boot like PairController._boot).
    """
    actor = op.get("actor", "leader")
    spec = ctx.clients[actor]
    peer = "follower" if actor == "leader" else "leader"
    pre = ctx.state(actor)
    pre_sid, pre_spread = pre.get("sessionId"), pre["applied"]["spreadId"]
    seq = ctx.fx.hello_seq(spec.id) if ctx.surface == "fixture" else 0
    ctx.driver.reload(spec)
    if ctx.surface == "fixture":
        ctx.fx.wait_hello(spec.id, timeout=60, min_seq=seq + 1)
        ctx.note("reloaded", True, f"hello seq>{seq}")
    st = ctx.poll(actor, lambda s: (
        (s.get("sessionId") == pre_sid and len(s.get("presence", {})) == 2
         and all(m["status"] == "active" for m in s["presence"].values()))
        or s.get("endReason") is not None),
                  timeout=60, what="post-reload resume")
    if st.get("sessionId") != pre_sid:
        if op.get("allow_solo") or ctx.driver.name == "simulator":
            ctx.note("reload_terminal", True,
                     f"solo/clean terminal (sessionStorage lost): "
                     f"status={st.get('status')} end={st.get('endReason')}")
            return
        ctx.fail("reload_resume",
                 f"credential resume failed: status={st.get('status')} "
                 f"end={st.get('endReason')}")
    ctx.note("reload_resumed", True, "credential RESUME after reload")
    l, f = ctx.poll_both(
        lambda a, b: _committed_same(a, b)
        and a["applied"]["spreadId"] == pre_spread,
        timeout=30, what="post-reload committed state")
    ctx.note("state_restored", True,
             f"spread {pre_spread} preserved across reload")
    _assert_pair_ids(ctx, l, f, "reload")
    for st_, role in ((l, "leader"), (f, "follower")):
        _assert_member_view(ctx, st_, role)


def op_bfcache(ctx: RunCtx, op: dict) -> None:
    """pagehide->pageshow round-trip: suspend is signaled to the peer, the
    session survives, the committed spread is unchanged."""
    actor = op.get("actor", "follower")
    peer = "follower" if actor == "leader" else "leader"
    pre = ctx.state(actor)
    pre_spread = pre["applied"]["spreadId"]
    spec = ctx.clients[actor]
    if ctx.surface == "ui":
        if spec.page is not None:
            spec.page.evaluate(
                "() => window.dispatchEvent(new Event('pagehide'))")
        else:
            ctx.driver.freeze_net(spec)      # WS silence == pagehide peer signal
    else:
        ctx.cmd(actor, "lifecycle", {"type": "pagehide"})
    ctx.poll(peer, lambda s: any(
        m["role"] == ctx.clients[actor].role
        and m["status"] in ("suspended", "offline")
        for m in s.get("presence", {}).values()),
        timeout=35, what="peer sees suspended after pagehide")
    ctx.note("bfcache_suspended", True, "pagehide -> peer saw suspended")
    if ctx.surface == "ui":
        if spec.page is not None:
            spec.page.evaluate(
                "() => window.dispatchEvent(new Event('pageshow'))")
        else:
            ctx.driver.thaw_net(spec)        # link thaw == pageshow resume
            ctx.driver.foreground(spec)
    else:
        ctx.cmd(actor, "lifecycle", {"type": "pageshow"})
    l, f = ctx.poll_both(
        lambda a, b: all(m["status"] == "active"
                         for m in a.get("presence", {}).values())
        and _committed_same(a, b)
        and a["applied"]["spreadId"] == pre_spread,
        timeout=30, what="pageshow restore")
    ctx.note("bfcache_restored", True,
             "same committed state after pageshow")
    _assert_pair_ids(ctx, l, f, "bfcache")


def op_server_restart(ctx: RunCtx, op: dict) -> None:
    """Restart the target env's viewer container. Defined terminal: every
    member's RESUME gets bad_resume -> session_lost -> re-pair works."""
    if not ctx.dev_container:
        raise EnvError("server_restart needs a docker-managed dev env "
                       "(--dev-env)")
    out = subprocess.run(
        ["docker", "restart", ctx.dev_container],
        capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise EnvError(f"docker restart {ctx.dev_container}: "
                       f"{out.stderr[:200]}")
    ctx.note("container_restarted", True, ctx.dev_container)
    # wait for health, then both clients' auto-reconnect hits dead sessions
    deadline = time.monotonic() + 60
    ok = False
    while time.monotonic() < deadline and not ok:
        try:
            import urllib.request
            with urllib.request.urlopen(
                    ctx.base_url.rstrip("/") + "/api/health",
                    timeout=3) as r:
                ok = json.loads(r.read().decode()).get("status") == "ok"
        except Exception:
            time.sleep(1.0)
    if not ok:
        raise EnvError("target did not come back healthy within 60s")
    l, f = ctx.poll_both(
        lambda a, b: (a.get("endReason") in ("session_lost", "ended", None)
                      and b.get("endReason") in ("session_lost", "ended", None)
                      and a.get("sessionId") is None
                      and b.get("sessionId") is None)
        or (a.get("endReason") == "session_lost"
            and b.get("endReason") == "session_lost"),
        timeout=90, what="session_lost after restart")
    ctx.note("session_lost", True,
             f"terminal endReason={l.get('endReason')}/{f.get('endReason')}")


def op_bystander_check(ctx: RunCtx, op: dict) -> None:
    bp = BystanderPair(ctx.ws_url)
    try:
        facts = bp.pair_and_probe()
    finally:
        bp.close()
    ctx.note("bystander_hello", facts["hello"], "second session HELLO")
    ctx.note("bystander_paired", facts["paired"], "second session paired")
    ctx.note("bystander_pong", facts["pong"], "ping-pong during faults")
    ctx.note("bystander_ended", facts["ended"], "clean leave")


def op_disconnect_input(ctx: RunCtx, op: dict) -> None:
    actor = op.get("actor", "follower")
    spec = ctx.clients[actor]
    kind = op["kind"]
    p = op.get("params") or {}
    idx0 = ctx.state(actor)["applied"]["index"] if ctx._table else None

    def _pt(v, dim):
        # coords may be absolute points or viewport fractions (0..1) so a
        # scenario survives different device widths
        v = float(v)
        return v * dim if 0 < v <= 1 else v

    w = h = None
    try:
        w, h = ctx.driver.screen_size(spec)
    except (EnvError, UnsupportedError, AttributeError):
        w, h = 400, 800
    fn = {
        "tap": lambda: ctx.driver.tap(
            spec, _pt(p.get("x", 0.5), w), _pt(p.get("y", 0.5), h)),
        "text": lambda: ctx.driver.input_text(spec, p.get("text", "")),
        "swipe": lambda: ctx.driver.swipe(
            spec, _pt(p.get("x1", 0.75), w), _pt(p.get("y1", 0.5), h),
            _pt(p.get("x2", 0.15), w), _pt(p.get("y2", 0.5), h),
            p.get("duration", 0.15)),
        "pinch": lambda: ctx.driver.pinch(
            spec, p.get("x", 196), p.get("y", 400), p.get("scale", 0.5)),
        "rotate": lambda: ctx.driver.rotate(
            spec, p.get("orientation", "LANDSCAPE_LEFT")),
        "describe": lambda: ctx.driver.describe_all(spec),
    }.get(kind)
    if fn is None:
        raise UnsupportedError(f"device_input kind {kind!r} unknown")
    # snapshot BEFORE the input lands — a post-input read would see the
    # fresh gesture itself and t > g0.t could never be satisfied.
    g0 = {}
    if ctx.surface == "fixture" and kind in ("tap", "swipe", "pinch"):
        g0 = ctx.state(actor).get("lastGesture") or {}
    try:
        out = fn()
    except EnvError:
        raise
    except UnsupportedError:
        raise
    ctx.note(f"input_{kind}", True,
             (json.dumps(out)[:200] if isinstance(out, (dict, list)) else ""))
    if ctx.surface != "fixture":
        return
    # the real device event must land as a fixture gesture; a tap in a
    # turn zone or a horizontal swipe then commits a real TURN through the
    # same sendIntent path a finger would take. Require a FRESH record —
    # a pinch that synthesizes no pointerup would otherwise satisfy the
    # poll with the previous input's leftover gesture and invent a turn.
    # rotate/describe/text never produce page gestures — nothing to check.
    if kind in ("tap", "swipe", "pinch"):
        st = None
        # idb swipe/tap delivery is itself flaky — a 150ms swipe can be
        # consumed before WebKit promotes it to a pan. One re-delivery
        # separates a lost input from a genuinely undeliverable kind.
        for _try in range(2):
            try:
                st = ctx.poll(actor,
                              lambda s: bool(s.get("lastGesture"))
                              and s["lastGesture"].get("t", 0) > g0.get("t", 0),
                              timeout=10, what="fresh gesture recorded")
                break
            except AssertError:
                if _try == 0:
                    g0 = ctx.state(actor).get("lastGesture") or {}
                    try:
                        fn()
                    except (EnvError, UnsupportedError):
                        pass
        if st is None:
            # no pointer event reached the page at all — an idb/driver
            # delivery gap (e.g. pinch synthesizing no pointerup), not
            # an app regression. The converge check below still judges
            # gestures that DID arrive.
            raise EnvError(
                f"{kind} produced no web-visible gesture on {actor}")
        g = st.get("lastGesture") or {}
        ctx.note(f"gesture_{kind}", g.get("kind") in ("tap", "drift-tap",
                                                     "swipe", "pinch"),
                 f"gesture={g.get('kind')} delta={g.get('delta')}")
        delta = int(g.get("delta") or 0)
        if delta != 0 and idx0 is not None:
            _converge_index(ctx, idx0 + delta,
                            f"device {kind} turn {delta:+d}")


def _tap_element(ctx: RunCtx, spec, label: str) -> str:
    """Tap a fixture control by its id/label — see op_tap_element."""
    drv = ctx.driver
    if drv.name == "chrome":
        drv.tap_element(spec, label)
        return "dom"
    try:
        if drv.tap_element(spec, label):
            return "ax"
    except EnvError:
        pass                            # AX unavailable → coordinate path
    ox, oy = _stable_offset(ctx, spec)
    rect = ctx.cmd(spec.role, "rect", {"id": label})
    tx = rect["x"] + rect["w"] / 2 + ox
    ty = rect["y"] + rect["h"] / 2 + oy
    drv.tap(spec, tx, ty)
    ctx.note(f"calibrated_{spec.role}", True,
             f"offset=({ox:.0f},{oy:.0f}) tap=({tx:.0f},{ty:.0f})")
    return "coord"


def _stable_offset(ctx: RunCtx, spec, tries: int = 5) -> tuple:
    """Screen->client offset, stabilized (see S10 notes — keyboard/layout
    settling needs consecutive agreement, not a single probe)."""
    drv = ctx.driver
    w, h = drv.screen_size(spec)
    cx, cy = w / 2, h / 2
    prev = None
    for _ in range(tries):
        t0_ms = time.time() * 1000
        drv.tap(spec, cx, cy)
        seen = None
        deadline = time.time() + 6
        while time.time() < deadline:
            seen = (ctx.cmd(spec.role, "last_tap") or {}).get("tap")
            if seen and seen.get("t", 0) >= t0_ms - 100 and \
                    seen.get("x") is not None:
                break
            time.sleep(0.15)
        if not seen or seen.get("x") is None:
            raise EnvError(
                f"calibration tap never reached the page ({spec.role})")
        off = (cx - float(seen["x"]), cy - float(seen["y"]))
        if prev and abs(off[0] - prev[0]) < 3 and abs(off[1] - prev[1]) < 3:
            return off
        prev = off
        time.sleep(0.4)                  # let any layout shift settle
    if prev is None:
        raise EnvError(f"offset never stabilized ({spec.role})")
    return prev


def op_tap_element(ctx: RunCtx, op: dict) -> None:
    actor = op.get("actor", "leader")
    spec = ctx.clients[actor]
    target = op["target"]
    idx0 = ctx.state(actor)["applied"]["index"] if ctx._table else None
    how = _tap_element(ctx, spec, target)
    ctx.note(f"tap_{target}", True, f"tapped {target} on {actor} ({how})")
    if "turn_delta" in op and idx0 is not None and op["turn_delta"]:
        _converge_index(ctx, idx0 + int(op["turn_delta"]),
                        f"tap {target} turn {op['turn_delta']:+d}")


def op_background(ctx: RunCtx, op: dict) -> None:
    actor = op.get("actor", "follower")
    spec = ctx.clients[actor]
    ctx.driver.background(spec)
    time.sleep(1.2)
    peer = "follower" if actor == "leader" else "leader"
    st = ctx.poll(peer, lambda s: any(
        m["role"] == ctx.clients[actor].role
        and m["status"] in ("suspended", "offline")
        for m in s.get("presence", {}).values())
        or all(m["status"] == "active" for m in s.get("presence", {}).values()),
        timeout=20, what="peer presence after background")
    ctx.note("background_seen", True,
             f"peer presence={[(m['role'], m['status']) for m in st['presence'].values()]}")


def op_foreground(ctx: RunCtx, op: dict) -> None:
    actor = op.get("actor", "follower")
    spec = ctx.clients[actor]
    ctx.driver.foreground(spec)
    time.sleep(1.5)
    st = ctx.state(actor)
    ctx.note("foreground", True,
             f"status={st.get('status')} phase={st.get('phase')}")


def op_pwa_install(ctx: RunCtx, op: dict) -> None:
    """Scripted Add-to-Home-Screen (driver-level; launch-time pwa kind
    uses the same path)."""
    actor = op.get("actor", "leader")
    spec = ctx.clients[actor]
    drv = ctx.driver
    if drv.name != "simulator":
        raise UnsupportedError("pwa_install needs the simulator driver")
    drv.install_webapp(spec)
    ctx.note("pwa_installed", True,
             "page menu → Add to Home Screen → 追加/Add")


def op_open_url(ctx: RunCtx, op: dict) -> None:
    actor = op.get("actor", "leader")
    spec = ctx.clients[actor]
    if ctx.driver.name == "chrome":
        spec.page.goto(ctx.base_url.rstrip("/") + op["path"],
                       wait_until="domcontentloaded")
    elif ctx.driver.name == "simulator":
        from verify_drivers import _simctl
        _simctl("openurl", spec.udid, ctx.base_url.rstrip("/") + op["path"])
    else:
        raise UnsupportedError("open_url: unsupported driver")
    ctx.note("open_url", True, op["path"])


def op_ui_input(ctx: RunCtx, op: dict) -> None:
    """A REAL input path on the ui surface (chrome): tap zones, keys
    incl. Home/End, slider, overview, swipe, spread-shift. The wire effect
    lands through the production PairController nav port."""
    actor = op.get("actor", "leader")
    spec = ctx.clients[actor]
    page = spec.page
    if page is None:
        raise UnsupportedError("ui_input needs the chrome driver")
    kind = op["kind"]
    p = op.get("params") or {}
    if kind == "key":
        page.keyboard.press(p.get("name", "ArrowLeft"))
    elif kind == "tap_zone":
        sel = "#zone-prev" if p.get("side", "prev") == "prev" else "#zone-next"
        page.click(sel, timeout=8000)
    elif kind == "slider":
        page.evaluate(
            "(v) => { const s = document.getElementById('page-slider');"
            " s.value = v; s.dispatchEvent(new Event('input')); }",
            int(p.get("value", 0)))
    elif kind == "overview":
        page.keyboard.press("g")
        page.wait_for_selector(".ov-cell", timeout=8000)
        page.click(".ov-cell")
    elif kind == "swipe":
        m = page.mouse
        x1, y1 = p.get("x1", 300), p.get("y1", 400)
        x2, y2 = p.get("x2", 60), p.get("y2", 400)
        m.move(x1, y1)
        m.down()
        for i in range(1, 7):
            m.move(x1 + (x2 - x1) * i / 6, y1 + (y2 - y1) * i / 6)
            page.wait_for_timeout(20)
        m.up()
    elif kind == "spread_shift":
        page.click("#spread-shift", timeout=8000)
    elif kind == "open_work":
        page.evaluate(
            "([folder, pg]) => window.ViewerNavigation.request({type:"
            "'openWork', folder: folder, page: pg, via: 'runner'})",
            [p["folder"], p.get("page")])
    elif kind == "browse":
        page.evaluate(
            "() => window.ViewerNavigation.request("
            "  {type: 'browse', via: 'runner'})")
    else:
        raise UnsupportedError(f"ui_input kind {kind!r}")
    ctx.note(f"ui_input_{kind}", True, f"actor={actor}")


def op_screenshot(ctx: RunCtx, op: dict) -> None:
    """Both-screen capture + (when a marker is expected) a pixel check on
    the fixture marker region."""
    label = op.get("label", "shot")
    # a screenshot right after a device input can catch the rotation or
    # layout animation mid-frame — the median then lands between the
    # marker color and the animation blend (sim_device_inputs flake).
    if ctx.driver.name == "simulator":
        time.sleep(0.9)
    for role in ("leader", "follower"):
        spec = ctx.clients[role]
        path = ctx.dir / f"{label}-{role}.png"
        ctx.driver.screenshot(spec, path)
        ctx.screenshots.append(str(path.relative_to(ctx.dir.parents[1])))
        if ctx._table is None or ctx.surface != "fixture":
            continue
        st = ctx.state(role)
        sid = st["applied"]["spreadId"]
        if not sid:
            continue
        exp = expected_page(ctx._table, sid, st["role"],
                            st["applied"].get("leaderSide") or "left")
        page = exp["page"]
        want_rgb = marker_rgb(page) if page else BLANK_RGB
        # layout-proof: the marker must dominate a real screen region
        # wherever Safari chrome leaves it (landscape shifts it below
        # the portrait-tuned sample box — see png_marker_fraction).
        frac = png_marker_fraction(path, want_rgb)
        ctx.note(f"pixel_{role}", frac >= 0.06,
                 f"marker≈{want_rgb} covers {frac:.1%} of screen")


def op_wait(ctx: RunCtx, op: dict) -> None:
    time.sleep(float(op["ms"]) / 1000.0)
    ctx.note("waited", True, f"{op['ms']}ms")


def op_leave(ctx: RunCtx, op: dict) -> None:
    actor = op.get("actor", "follower")
    if ctx.surface == "ui":
        spec = ctx.clients[actor]
        if spec.page is not None:
            spec.page.evaluate(
                "() => window.PairController.leave()")
        else:
            # simulator: real UI leave — strip → pair panel → 解除. A
            # session that already ended (peer left, server END after a
            # suspend/resume edge) shows no strip — idle IS the terminal
            # the END poll below checks.
            try:
                _st = ctx._ui_state(spec.role)
            except AssertError:
                _st = {}
            if _st.get("sessionId") or _st.get("status") == "connected":
                _ui_tap(ctx, spec, _STRIP_NEEDLES, "status strip")
                _ui_tap(ctx, spec, ["解除"], "leave button")
    else:
        ctx.cmd(actor, "leave")
    ctx.poll_both(
        lambda a, b: (a.get("endReason") is not None or
                      not a.get("sessionId"))
        and (b.get("endReason") is not None or not b.get("sessionId")),
        timeout=20, what="END on both")
    ctx.note("ended_both", True, "END/leave on both")


OPS = {
    "pair": op_pair,
    "open_work": op_open_work,
    "turn": op_turn,
    "goto": op_goto,
    "swap_sides": op_swap_sides,
    "set_offset": op_set_offset,
    "concurrent": op_concurrent,
    "burst": op_burst,
    "latency": op_latency,
    "peer_latency": op_peer_latency,
    "rtt": op_rtt,
    "expect": op_expect,
    "dc_open": op_dc_open,
    "dc_close": op_dc_close,
    "dc_restart": op_dc_restart,
    "disconnect": op_disconnect,
    "resume": op_resume,
    "resume_fail": op_resume_fail,
    "fault": op_fault,
    "image_fault": op_image_fault,
    "suspend": op_suspend,
    "activate": op_activate,
    "reload": op_reload,
    "bfcache": op_bfcache,
    "server_restart": op_server_restart,
    "bystander_check": op_bystander_check,
    "device_input": op_disconnect_input,
    "tap_element": op_tap_element,
    "background": op_background,
    "foreground": op_foreground,
    "pwa_install": op_pwa_install,
    "ui_input": op_ui_input,
    "open_url": op_open_url,
    "wait": op_wait,
    "screenshot": op_screenshot,
    "leave": op_leave,
}


def run_scenario(ctx: RunCtx, scenario: dict, transport: str,
                 run_index: int = 1) -> dict:
    """Execute one scenario against the prepared clients. Returns the
    per-scenario result dict consumed by the result JSON."""
    ctx._table = None
    ctx._leader_side = "left"
    ctx.scenario = scenario
    result = {"name": scenario["name"], "transport": transport,
              "run": run_index,
              "status": "pass", "assertions": ctx.assertions,
              "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "duration_ms": 0}
    t0 = _now_ms()
    try:
        # sim+ui runs die quietly when a client's page is still loading
        # or frozen in a stray tab — gate op#1 on fresh reports from
        # both clients (this also gives the _pair_report_state heal time
        # to refocus/terminate-recover a lost tab before the clock).
        if ctx.driver.name == "simulator" and ctx.surface == "ui":
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                # poll EVERY client each round — the freshness read is
                # also the heal trigger, and all() short-circuit would
                # starve the second client's refocus attempts.
                if all([_report_fresh(ctx, spec.role, 10)
                        for spec in ctx.clients.values()]):
                    break
                time.sleep(1.0)
            else:
                raise EnvError(
                    "client pages never came live at run start")
        for op in scenario["ops"]:
            fn = OPS[op["op"]]
            try:
                fn(ctx, op)
            except (EnvError, UnsupportedError) as e:
                # ``optional`` ops cover inputs/surfaces a driver may not
                # be able to deliver (e.g. idb pinch produces no Safari
                # gesture on iOS 27). The gap is recorded visibly but must
                # not abort the rest of the op sequence.
                if not op.get("optional"):
                    raise
                ctx.note(f"optional_{op['op']}", True,
                         f"skipped ({type(e).__name__}): {e}")
    except AssertError as e:
        result["status"] = "fail"
        ctx.note("error", False, f"assert: {e}")
    except UnsupportedError as e:
        result["status"] = "unsupported"
        ctx.note("error", False, f"unsupported: {e}")
    except EnvError as e:
        result["status"] = "env_unavailable"
        ctx.note("error", False, f"env: {e}")
    except Exception as e:  # noqa: BLE001 — unknown failure maps to env
        result["status"] = "env_unavailable"
        ctx.note("error", False, f"{type(e).__name__}: {e}")
    result["duration_ms"] = _now_ms() - t0
    # a frozen client must never leak past its run — an abort between
    # freeze_net and thaw_net (env_unavailable mid-suspend) would keep
    # WebKit.Networking SIGSTOPped and poison every following run.
    if ctx.driver.name == "simulator":
        for spec in ctx.clients.values():
            if getattr(spec, "net_frozen", False):
                try:
                    ctx.driver.thaw_net(spec)
                except Exception:
                    spec.net_frozen = False
    # a recorded ok=False assertion (e.g. pixel mismatch, soft notes) fails
    # the scenario even when no exception fired — the spec's いずれの失敗も
    # exit 0 にしない applies to recorded assertions too.
    if result["status"] == "pass" and any(
            not a["ok"] for a in ctx.assertions):
        result["status"] = "fail"
    if ctx.rtt:
        result["rtt_ms"] = ctx.rtt
    if ctx.latency:
        result["latency_ms"] = ctx.latency
    if ctx.screenshots:
        result["artifacts"] = {"screenshots": ctx.screenshots}
    # pull both clients' wire traces for the evidence dir
    for role in ("leader", "follower"):
        try:
            tr = ctx.cmd(role, "pull_trace", timeout=10)
            tr_path = ctx.dir / f"trace-{role}.jsonl"
            from verify_core import redact  # noqa: PLC0415
            with open(tr_path, "w", encoding="utf-8") as fh:
                for ev in (tr.get("trace") or []):
                    fh.write(json.dumps(redact(ev), ensure_ascii=False) + "\n")
        except Exception:
            pass
    return result
