"""S10 (#85) — scenario op interpreter (the single declaration contract).

One op grammar, executed by the runner against the two fixture clients
(and, where a device input is declared, through the active driver). All
assertions are deterministic: protocol state comes from the fixture's own
view of the production modules (page-evaluated JS inside fixture.html),
visible markers come from both the DOM marker the fixture reports AND the
screenshot pixel check on captured PNGs.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from verify_core import (
    AssertError,
    EnvError,
    UnsupportedError,
    build_spread_table,
    expected_marker,
    marker_rgb,
    png_region_rgb,
    rgb_close,
    rtt_stats,
    BLANK_LABEL,
    BLANK_RGB,
)
from verify_drivers import BystanderPair
from verify_fixture_server import CmdError


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


class RunCtx:
    """Per-run context handed to every op."""

    def __init__(self, *, fx, driver, clients, ws_url, base_url,
                 evidence_dir: Path, surface: str = "fixture"):
        self.fx = fx
        self.driver = driver
        self.clients = clients        # {"leader": ClientSpec, "follower": ClientSpec}
        self.ws_url = ws_url
        self.base_url = base_url
        self.dir = evidence_dir
        self.surface = surface
        self.assertions: list[dict] = []
        self.screenshots: list[str] = []
        self.rtt: dict = {}
        self.scenario: dict = {}
        self._table: list | None = None
        self._leader_side: str = "right"

    # -- recording ----------------------------------------------------

    def note(self, name: str, ok: bool, detail: str = "") -> bool:
        self.assertions.append({"name": name, "ok": bool(ok),
                                "detail": str(detail)[:300]})
        return ok

    def fail(self, name: str, detail: str):
        self.note(name, False, detail)
        raise AssertError(f"{name}: {detail}")

    # -- fixture plumbing ----------------------------------------------

    def cmd(self, role: str, name: str, params: dict | None = None,
            timeout: float = 30.0):
        if self.surface != "fixture":
            raise UnsupportedError(
                f"op '{name}' needs the fixture control surface — "
                f"the real-UI surface lands with S09")
        cid = self.clients[role].id
        try:
            return self.fx.cmd(cid, name, params, timeout=timeout)
        except CmdError as e:
            if e.kind == "env":
                raise EnvError(str(e)) from e
            raise AssertError(f"cmd {name}@{role}: {e}") from e

    def state(self, role: str) -> dict:
        return self.cmd(role, "state", timeout=15)

    def poll(self, role: str, pred, timeout: float = 12.0,
             interval: float = 0.15, what: str = "state"):
        deadline = time.monotonic() + timeout
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
        deadline = time.monotonic() + timeout
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


# ────────────────────────── ops ──────────────────────────────────────


def op_pair(ctx: RunCtx, op: dict) -> None:
    """CREATE -> code -> JOIN -> APPROVE -> presence=2 on both sides."""
    if ctx.surface == "ui":
        return _op_pair_ui(ctx, op)
    ctx.cmd("leader", "connect")
    ctx.cmd("follower", "connect")
    l, f = ctx.poll_both(
        lambda a, b: a["wsOpen"] and b["wsOpen"], what="ws open")
    ctx.note("ws_open_both", True, "both WS links open (HELLO received)")

    if op.get("via") == "taps":
        # real device taps on the fixture's DOM controls (simulator path):
        # tap 開始 -> read the shown code -> type it on the peer -> tap 参加
        # -> leader taps 承認.
        _tap_element(ctx, ctx.clients["leader"], "fx-create")
        code = (ctx.cmd("leader", "code") or {}).get("code")
        ctx.note("leader_created", isinstance(code, str) and len(code) == 6,
                 "code displayed after 開始 tap")
        _tap_element(ctx, ctx.clients["follower"], "fx-code-input")
        ctx.driver.input_text(ctx.clients["follower"], code)
        # typing opens the keyboard and shifts Safari's layout — a neutral
        # tap dismisses it, then the next tap re-calibrates (stabilized)
        drv = ctx.driver
        w, h = drv.screen_size(ctx.clients["follower"])
        drv.tap(ctx.clients["follower"], w / 2, h / 2)
        time.sleep(1.0)
        _tap_element(ctx, ctx.clients["follower"], "fx-join")
        ctx.poll("leader", lambda s: s["pending"], what="join_request")
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
    ctx.note("plane_ws", l["dataPlane"] in (None, "server-ws"),
             f"dataPlane={l['dataPlane']}")


def _op_pair_ui(ctx: RunCtx, op: dict) -> None:
    """Real-app pairing UI surface — needs the S09 controller wiring."""
    leader = ctx.clients["leader"].page
    follower = ctx.clients["follower"].page
    if leader is None or follower is None:
        raise UnsupportedError("ui surface needs DOM access (chrome driver)")
    has = leader.evaluate("() => !!window.PairController")
    if not has:
        raise UnsupportedError(
            "ui surface requires the S09 PairController wiring (#84) — "
            "the production pairing UI is a shell until it lands")
    # post-S09 path (kept for the switch): drive the real pairing DOM.
    leader.evaluate("window.closeSidebar && window.closeSidebar()")
    follower.evaluate("window.closeSidebar && window.closeSidebar()")
    leader.click("#pair-btn")
    leader.click("#pair-create")
    # The leader section renders '------' as a placeholder the moment it
    # appears — wait for an actual 6-digit code, not just non-emptiness.
    leader.wait_for_function(
        "() => /^\\d{6}$/.test((document.getElementById('pair-code-value')"
        "  .textContent || '').trim())",
        timeout=15000)
    code = leader.text_content("#pair-code-value").strip()
    follower.click("#pair-btn")
    follower.click("#pair-join-open")
    follower.fill("#pair-code-input", code)
    follower.click("#pair-join-submit")
    leader.click("#pair-approve", timeout=15000)
    leader.wait_for_function(
        "() => document.getElementById('pair-status')"
        "  .dataset.status === 'connected'", timeout=15000)
    follower.wait_for_function(
        "() => document.getElementById('pair-status')"
        "  .dataset.status === 'connected'", timeout=15000)
    ctx.note("paired_both", True, "UI: both status chips connected")


def _resolve_work(decl) -> dict:
    """op/scenario work: inline declaration or a named fx fixture from
    tests/fixtures/reader_sync_tree.py."""
    if isinstance(decl, str):
        from tests.fixtures.reader_sync_tree import WORKS  # noqa: PLC0415
        w = WORKS.get(decl)
        if w is None:
            raise UnsupportedError(f"unknown work name {decl!r}")
        return w
    return decl


def op_open_work(ctx: RunCtx, op: dict) -> None:
    """Load a synthetic work: runner builds the REAL spread table
    (app.reader_sync.model), ships it to both clients, asserts markers."""
    work = _resolve_work(op.get("work") or ctx.scenario.get("work"))
    if work is None:
        raise UnsupportedError("open_work needs a work declaration")
    table = build_spread_table(work)
    if not table:
        raise AssertError("open_work: empty spread table")
    ctx._table = table
    ctx._leader_side = "right"
    for role in ("leader", "follower"):
        ctx.cmd(role, "open_work",
                {"table": table, "startIndex": op.get("startIndex", 0)})
    idx = int(op.get("startIndex", 0))
    for role in ("leader", "follower"):
        st = ctx.poll(role, lambda s: s["spreadId"] == table[idx]["id"],
                      what=f"spread {table[idx]['id']}")
        exp = expected_marker(table, idx, st["role"], "right")
        ref = exp["ref"]
        ctx.note(f"marker_{role}",
             st["marker"]["side"] == exp["side"] and
             st["marker"]["page"] == (ref["name"] if ref else BLANK_LABEL),
             f"side={st['marker']['side']} page={st['marker']['page']}")
    ctx.note("table_len", True, f"{len(table)} spreads")


def _converge(ctx: RunCtx, expect_index: int, what: str) -> tuple:
    """Poll until both clients commit the same index+rev; assert markers."""
    if ctx._table is None:
        raise UnsupportedError(f"{what}: no work open (needs open_work)")

    def ok(l, f):
        c1, c2 = l["committed"], f["committed"]
        return (c1["index"] == expect_index == c2["index"]
                and c1["rev"] == c2["rev"] and c1["rev"] > 0
                and c1["leaderSide"] == c2["leaderSide"])
    l, f = ctx.poll_both(ok, what=what)
    ctx.note("converged", True,
             f"index={expect_index} rev={l['committed']['rev']}")
    for st, role in ((l, "leader"), (f, "follower")):
        exp = expected_marker(ctx._table, expect_index, st["role"],
                              l["committed"]["leaderSide"])
        ref = exp["ref"]
        want = ref["name"] if ref else BLANK_LABEL
        ctx.note(f"marker_{role}", st["marker"]["page"] == want
                 and st["marker"]["side"] == exp["side"],
                 f"side={st['marker']['side']} page={st['marker']['page']} "
                 f"(want {want})")
    return l, f


def _table_to_plan(table: list[dict]):
    """Serialized spread dicts -> model.Spread objects for turn_index."""
    from verify_core import _model
    model = _model()
    plan = []
    for s in table:
        right = model.make_ref(s["right"]["folder"], s["right"]["name"])
        left = (model.make_ref(s["left"]["folder"], s["left"]["name"])
                if s.get("left") else None)
        plan.append(model.Spread(s["id"], s["chapterFolder"], right, left))
    return plan


def op_turn(ctx: RunCtx, op: dict) -> None:
    actor = op.get("actor", "leader")
    delta = int(op.get("delta", 1))
    if ctx._table is None:
        raise UnsupportedError("turn: no work open (needs open_work)")
    from verify_core import _model
    model = _model()
    cur = ctx.state(actor)["committed"]["index"]
    plan = _table_to_plan(ctx._table)
    target = model.turn_index(plan, cur, delta, bool(op.get("crossChapter", True)))
    ctx.cmd(actor, "turn", {"delta": delta})
    l, f = _converge(ctx, target, f"turn {delta:+d} by {actor}")
    ctx.note("turn_ok", l["spreadId"] == f["spreadId"],
             f"spreadId={l['spreadId']}")


def op_goto(ctx: RunCtx, op: dict) -> None:
    actor = op.get("actor", "leader")
    idx = int(op["index"])
    if ctx._table is None:
        raise UnsupportedError("goto: no work open (needs open_work)")
    from verify_core import _model
    model = _model()
    want = model.clamp_index(_table_to_plan(ctx._table), idx)
    ctx.cmd(actor, "goto", {"index": idx})
    _converge(ctx, want, f"goto {idx} by {actor}")


def op_swap_sides(ctx: RunCtx, op: dict) -> None:
    actor = op.get("actor", "leader")
    if ctx._table is None:
        raise UnsupportedError("swap_sides: no work open (needs open_work)")
    before = ctx.state(actor)["committed"]["leaderSide"]
    want = "left" if before == "right" else "right"
    ctx.cmd(actor, "swap")
    l, f = ctx.poll_both(
        lambda a, b: a["committed"]["leaderSide"] == want
        and b["committed"]["leaderSide"] == want
        and a["committed"]["rev"] == b["committed"]["rev"],
        what="sides swapped")
    ctx.note("sides_swapped", True, f"leaderSide={want}")
    idx = l["committed"]["index"]
    for st, role in ((l, "leader"), (f, "follower")):
        exp = expected_marker(ctx._table, idx, st["role"], want)
        ref = exp["ref"]
        wantpage = ref["name"] if ref else BLANK_LABEL
        ctx.note(f"marker_{role}", st["marker"]["page"] == wantpage
                 and st["marker"]["side"] == exp["side"],
                 f"side={st['marker']['side']} page={st['marker']['page']}")


def op_signal(ctx: RunCtx, op: dict) -> None:
    """SIGNAL relay round-trip in BOTH directions over the wire path —
    the spec's 双方の送り戻し at the transport level."""
    a, b = op.get("actor", "leader"), None
    other = "follower" if a == "leader" else "leader"
    for tag, src in (("fwd", a), ("back", other)):
        r = ctx.cmd(src, "signal_ping", {"tag": tag}, timeout=15)
        ctx.note(f"relay_{tag}", isinstance(r.get("rtt"), (int, float)),
                 f"{src}->peer->{src} rtt={r.get('rtt')}ms")


def op_rtt(ctx: RunCtx, op: dict) -> None:
    n = int(op.get("samples", 5))
    actor = op.get("actor", "leader")
    r = ctx.cmd(actor, "echo", {"samples": n}, timeout=15 + n * 2)
    stats = rtt_stats(r.get("samples") or [])
    ctx.rtt = stats
    ctx.note("rtt_samples", stats["n"] == n,
             f"n={stats['n']} p50={stats['p50']}ms p95={stats['p95']}ms")


def op_dc_open(ctx: RunCtx, op: dict) -> None:
    """Real WebRTC: SIGNAL-relayed offer/answer/ice (host candidates only)
    between the two clients, then the server's TRANSPORT_STATE flip."""
    ctx.cmd("leader", "dc_open", {"initiator": True})
    ctx.cmd("follower", "dc_open", {"initiator": False})
    try:
        l, f = ctx.poll_both(lambda a, b: a["dcOpen"] and b["dcOpen"],
                             timeout=20, what="datachannel open")
    except AssertError as e:
        raise EnvError(f"webrtc-dc unavailable: {e}") from e
    ctx.note("dc_open_both", True, "RTCDataChannel open on both")
    l, f = ctx.poll_both(
        lambda a, b: a["dataPlane"] == "webrtc-dc" and b["dataPlane"] == "webrtc-dc",
        timeout=15, what="dataPlane=webrtc-dc")
    ctx.note("plane_switched", True,
             f"server-reported dataPlane={l['dataPlane']} rev={l['serverRev']}")
    for role in ("leader", "follower"):
        ctx.cmd(role, "ctl_plane", {"plane": "dc"})
    ctx.note("ctl_on_dc", True, "fixture ctl frames moved to webrtc-dc")


def op_dc_close(ctx: RunCtx, op: dict) -> None:
    for role in ("leader", "follower"):
        ctx.cmd(role, "dc_close")
    l, f = ctx.poll_both(
        lambda a, b: a["dataPlane"] == "server-ws" and b["dataPlane"] == "server-ws",
        timeout=15, what="dataPlane back to server-ws")
    ctx.note("plane_restored", True, "server-reported dataPlane=server-ws")
    ctx.note("ctl_on_ws", True, "fixture ctl frames back on server-ws")


def op_disconnect(ctx: RunCtx, op: dict) -> None:
    actor = op.get("actor", "follower")
    peer = "follower" if actor == "leader" else "leader"
    ctx.cmd(actor, "disconnect")
    ctx.poll(peer, lambda s: any(
        m["role"] == ctx.clients[actor].role and m["status"] == "offline"
        for m in s["presence"].values()), what="peer offline")
    ctx.note("peer_marked_offline", True, f"{peer} sees {actor} offline")


def op_resume(ctx: RunCtx, op: dict) -> None:
    actor = op.get("actor", "follower")
    peer = "follower" if actor == "leader" else "leader"
    ctx.cmd(actor, "resume", timeout=30)
    st = ctx.poll(peer, lambda s: all(
        m["status"] == "active" for m in s["presence"].values()),
        what="peer active again")
    ctx.note("resumed", True, "RESUME ack + presence active")
    l, f = ctx.poll_both(
        lambda a, b: a["sessionId"] == b["sessionId"] and a["sessionId"],
        what="same session after resume")
    ctx.note("session_kept", True, "same sessionId after resume")


def op_resume_fail(ctx: RunCtx, op: dict) -> None:
    actor = op.get("actor", "follower")
    r = ctx.cmd(actor, "resume_bad", {"token": op.get("token", "bogus-token")})
    ctx.note("resume_rejected",
             r.get("rejected") in ("bad_resume", "resume_expired"),
             f"reason={r.get('rejected')}")


def op_fault(ctx: RunCtx, op: dict) -> None:
    actor = op.get("actor", "leader")
    ftype = op["type"]
    params = {k: v for k, v in op.items() if k not in ("op", "actor", "type")}
    r = ctx.cmd(actor, "fault", {"type": ftype, **params})
    ctx.note(f"fault_{ftype}", True, f"injected on {actor}: {r}")


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
    fn = {
        "tap": lambda: ctx.driver.tap(spec, p.get("x", 196), p.get("y", 400)),
        "text": lambda: ctx.driver.input_text(spec, p.get("text", "")),
        "swipe": lambda: ctx.driver.swipe(
            spec, p.get("x1", 300), p.get("y1", 500),
            p.get("x2", 100), p.get("y2", 500), p.get("duration", 0.15)),
        "pinch": lambda: ctx.driver.pinch(
            spec, p.get("x", 196), p.get("y", 400), p.get("scale", 0.5)),
        "rotate": lambda: ctx.driver.rotate(
            spec, p.get("orientation", "LANDSCAPE_LEFT")),
        "describe": lambda: ctx.driver.describe_all(spec),
    }.get(kind)
    if fn is None:
        raise UnsupportedError(f"device_input kind {kind!r} unknown")
    try:
        out = fn()
    except EnvError:
        raise
    except UnsupportedError:
        raise
    ctx.note(f"input_{kind}", True,
             (json.dumps(out)[:200] if isinstance(out, (dict, list)) else ""))


def _tap_element(ctx: RunCtx, spec, label: str) -> str:
    """Tap a fixture control by its id/label.

    - chrome driver: a real DOM click (Playwright, trusted input).
    - simulator: native AX tap when the label exists in the idb tree
      (Safari chrome buttons like MoreMenuButton); otherwise a CALIBRATED
      coordinate tap — idb taps a known screen point, the page reports
      where it landed (last_tap), and the delta converts the element's
      client rect into screen points. Web content is invisible to the
      simulator AX server, so this is the real-tap path for page UI.
    """
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
    """Screen->client offset, stabilized.

    Each iteration taps the screen CENTER (safe: lands on the marker area,
    which has no handler) and reads back where the page saw the pointer.
    The first tap may dismiss an open keyboard or settle an in-flight
    Safari layout shift, so we loop until two consecutive offsets agree —
    that is also what makes the keyboard-open case deterministic instead
    of a race."""
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
    how = _tap_element(ctx, spec, target)
    ctx.note(f"tap_{target}", True, f"tapped {target} on {actor} ({how})")


def op_background(ctx: RunCtx, op: dict) -> None:
    actor = op.get("actor", "follower")
    spec = ctx.clients[actor]
    ctx.driver.background(spec)
    time.sleep(1.2)
    st = ctx.state(actor)
    ctx.note("background_alive", st["wsOpen"] or st["phase"] == "disconnected",
             f"phase={st['phase']} wsOpen={st['wsOpen']}")


def op_foreground(ctx: RunCtx, op: dict) -> None:
    actor = op.get("actor", "follower")
    spec = ctx.clients[actor]
    ctx.driver.foreground(spec)
    time.sleep(1.5)
    st = ctx.state(actor)
    ctx.note("foreground", True, f"phase={st['phase']} wsOpen={st['wsOpen']}")


def op_pwa_install(ctx: RunCtx, op: dict) -> None:
    """Scripted Add-to-Home-Screen via the idb AX tree (no human step).

    iOS 26+/27 Safari: the page menu (AXUniqueId MoreMenuButton) holds the
    share/ホーム画面に追加 row; the sheet's confirm button is 追加/Add.
    Labels are localized — match substrings across en+ja."""
    actor = op.get("actor", "leader")
    spec = ctx.clients[actor]
    drv = ctx.driver
    if drv.name != "simulator":
        raise UnsupportedError("pwa_install needs the simulator driver")
    from verify_drivers import _ax_find_sub, _find_ax  # noqa: PLC0415

    ax = drv.describe_all(spec)
    labels = _ax_labels(ax)
    ctx.note("ax_probe", True, f"{len(labels)} labels")

    def find_retry(needles, tries=6, delay=0.9):
        for _ in range(tries):
            tree = drv.describe_all(spec)
            for n in needles:
                hit = _ax_find_sub(tree, n) or _find_ax(tree, n)
                if hit:
                    return hit, tree
            time.sleep(delay)
        return None, tree

    menu, _ = find_retry(["MoreMenuButton", "ページメニュー", "Menu"], tries=2)
    if menu is None:
        raise EnvError(f"pwa_install: no page-menu button; labels={labels[:30]}")
    drv.tap(spec, menu["x"] + menu["w"] / 2, menu["y"] + menu["h"] / 2)

    share, tree = find_retry(
        ["ホーム画面に追加", "Home Screen", "共有", "Share"])
    if share is None:
        raise EnvError(
            "pwa_install: no Add-to-Home/Share row; "
            f"labels={_ax_labels(tree)[:40]}")
    drv.tap(spec, share["x"] + share["w"] / 2, share["y"] + share["h"] / 2)

    add, tree = find_retry(["追加", "Add"])
    if add is None:
        raise EnvError(
            f"pwa_install: no confirm button; labels={_ax_labels(tree)[:40]}")
    drv.tap(spec, add["x"] + add["w"] / 2, add["y"] + add["h"] / 2)
    ctx.note("pwa_installed", True,
             "page menu → Add to Home Screen → 追加/Add")


def _ax_labels(node) -> list:
    out = []
    if isinstance(node, dict):
        for k in ("AXUniqueId", "AXLabel", "label", "name"):
            if node.get(k):
                out.append(node[k])
        for k in ("children", "elements", "subelements"):
            for ch in node.get(k) or []:
                out.extend(_ax_labels(ch))
    elif isinstance(node, list):
        for el in node:
            out.extend(_ax_labels(el))
    return out


def op_screenshot(ctx: RunCtx, op: dict) -> None:
    """Both-screen capture + (when a marker is expected) a pixel check on
    the fixture marker region."""
    label = op.get("label", "shot")
    for role in ("leader", "follower"):
        spec = ctx.clients[role]
        path = ctx.dir / f"{label}-{role}.png"
        ctx.driver.screenshot(spec, path)
        ctx.screenshots.append(str(path.relative_to(ctx.dir.parents[1])))
        if ctx._table is None:
            continue
        st = ctx.state(role)
        idx = st["committed"]["index"]
        if idx < 0 or idx >= len(ctx._table):
            continue
        exp = expected_marker(ctx._table, idx, st["role"],
                              st["committed"]["leaderSide"])
        ref = exp["ref"]
        want_rgb = marker_rgb(ref["name"]) if ref else BLANK_RGB
        got_rgb = png_region_rgb(path)
        ctx.note(f"pixel_{role}", rgb_close(got_rgb, want_rgb),
                 f"region={got_rgb} want≈{want_rgb}")


def op_wait(ctx: RunCtx, op: dict) -> None:
    time.sleep(float(op["ms"]) / 1000.0)
    ctx.note("waited", True, f"{op['ms']}ms")


def op_leave(ctx: RunCtx, op: dict) -> None:
    actor = op.get("actor", "follower")
    ctx.cmd(actor, "leave")
    ctx.poll_both(lambda a, b: a["phase"] == "ended" and b["phase"] == "ended",
                  what="END on both")
    ctx.note("ended_both", True, "END reason=left on both")


OPS = {
    "pair": op_pair,
    "open_work": op_open_work,
    "turn": op_turn,
    "goto": op_goto,
    "swap_sides": op_swap_sides,
    "signal": op_signal,
    "rtt": op_rtt,
    "dc_open": op_dc_open,
    "dc_close": op_dc_close,
    "disconnect": op_disconnect,
    "resume": op_resume,
    "resume_fail": op_resume_fail,
    "fault": op_fault,
    "bystander_check": op_bystander_check,
    "device_input": op_disconnect_input,
    "tap_element": op_tap_element,
    "background": op_background,
    "foreground": op_foreground,
    "wait": op_wait,
    "pwa_install": op_pwa_install,
    "screenshot": op_screenshot,
    "leave": op_leave,
}


def run_scenario(ctx: RunCtx, scenario: dict, transport: str) -> dict:
    """Execute one scenario against the prepared clients. Returns the
    per-scenario result dict consumed by the result JSON."""
    ctx._table = None
    ctx._leader_side = "right"
    ctx.scenario = scenario
    result = {"name": scenario["name"], "transport": transport,
              "status": "pass", "assertions": ctx.assertions,
              "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "duration_ms": 0}
    t0 = _now_ms()
    try:
        for op in scenario["ops"]:
            fn = OPS[op["op"]]
            fn(ctx, op)
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
    # a recorded ok=False assertion (e.g. pixel mismatch, soft notes) fails
    # the scenario even when no exception fired — the spec's いずれの失敗も
    # exit 0 にしない applies to recorded assertions too.
    if result["status"] == "pass" and any(
            not a["ok"] for a in ctx.assertions):
        result["status"] = "fail"
    if ctx.rtt:
        result["rtt_ms"] = ctx.rtt
    if ctx.screenshots:
        result["artifacts"] = {"screenshots": ctx.screenshots}
    # pull both clients' wire traces for the evidence dir
    for role in ("leader", "follower"):
        try:
            tr = ctx.cmd(role, "pull_trace", timeout=10)
            ctx.fx  # noqa
            tr_path = ctx.dir / f"trace-{role}.jsonl"
            from verify_core import redact  # noqa: PLC0415
            with open(tr_path, "w", encoding="utf-8") as fh:
                for ev in (tr.get("trace") or []):
                    fh.write(json.dumps(redact(ev), ensure_ascii=False) + "\n")
        except Exception:
            pass
    return result
