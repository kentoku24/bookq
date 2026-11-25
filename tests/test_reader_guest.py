"""Guest access — unlogged-in QR-link pairing (guest-access-link).

Real auth server (SECRET_KEY set, TEST_MODE unset) spawned like
test_reader_ws's auth_server fixture: the leader pairs as ALICE, the
guest redeems the CREATE ACK's ``guestToken`` via
``POST /api/v1/guest/redeem`` and rides the ``bookq_guest`` cookie for
HTTP + WS as a read-only follower of the leader's identity.

Protocol layer covers link establishment, follower-equivalent ops,
read-only enforcement and revocation; the browser layer replays the QR
URL flow end-to-end (login.js redeem → viewer shell → hash.js
joinGuest → auto-pair → follower turn convergence).
"""
import contextlib
import re
import time

import httpx
import pytest
from websockets.exceptions import InvalidHandshake
from websockets.sync.client import connect as ws_connect

from test_reader_state import (  # noqa: E402
    _drain_until,
    _expect,
    _open_and_ready,
    _recv,
    _req,
    _turn,
)
from test_reader_sync_integration import _open, _synced  # noqa: E402
from test_reader_ws import (  # noqa: E402
    ALICE,
    BOB,
    _session_cookie,
    _spawn_server,
)

_WS_PATH = "/ws/reader-sync"
WORK = "simple_manga"
FIRST_PAGE = {"folder": WORK, "name": "1.png"}
GUEST_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


ADMIN = {"email": "admin@example.com", "name": "Admin", "picture": ""}


@pytest.fixture(scope="module")
def guest_state_dir(tmp_path_factory):
    """Users DB for the real-auth server — exposed so tests can drive
    `users` CLI / `/admin` transitions against the live directory."""
    from rs_spawn import users_cli

    state_dir = tmp_path_factory.mktemp("reader-guest-state")
    for user in (ALICE, BOB):
        cp = users_cli(state_dir, "add", user["email"])
        assert cp.returncode == 0, cp.stderr
    cp = users_cli(state_dir, "add", ADMIN["email"], "--role", "admin")
    assert cp.returncode == 0, cp.stderr
    return state_dir


@pytest.fixture(scope="module")
def guest_server(library_tree, guest_state_dir):
    """Live server WITHOUT test-mode: real signed-cookie + guest auth."""
    proc, base_url = _spawn_server(library_tree, guest_state_dir)
    yield base_url
    proc.terminate()
    proc.wait(timeout=5)


# ── helpers ─────────────────────────────────────────────────────────


def _ws_user(base_url: str, user: dict):
    return ws_connect(
        base_url.replace("http", "ws") + _WS_PATH,
        additional_headers={"Cookie": f"session={_session_cookie(user)}"},
    )


def _ws_guest(base_url: str, token: str):
    return ws_connect(
        base_url.replace("http", "ws") + _WS_PATH,
        additional_headers={"Cookie": f"bookq_guest={token}"},
    )


def _create_leader(ws) -> dict:
    """CREATE on an already-HELLO'd leader link; returns ACK data."""
    ws.send(_req("CREATE", rid="c"))
    return _expect(ws, "ACK")["data"]


def _redeem(base_url: str, token: str, *, cookie: str | None = None):
    headers = {"Cookie": cookie} if cookie else {}
    return httpx.post(
        base_url + "/api/v1/guest/redeem",
        json={"token": token},
        headers=headers,
        timeout=10,
    )


def _guest_cookie_value(resp: httpx.Response) -> str | None:
    for sc in resp.headers.get_list("set-cookie"):
        m = re.match(r"bookq_guest=([^;]+)", sc)
        if m:
            return m.group(1)
    return None


def _get(base_url: str, path: str, *, cookie: str | None = None):
    headers = {"Cookie": cookie} if cookie else {}
    return httpx.get(base_url + path, headers=headers, timeout=10)


def _leave(ws, sid):
    try:
        ws.send(_req("LEAVE", sid=sid, rid="lv"))
    except Exception:
        pass


def _mint(base_url: str):
    """Leader CREATE → (leader_ws, ack_data, guestToken). Link stays open —
    caller LEAVEs/closes it when done."""
    ws = _ws_user(base_url, ALICE)
    assert _recv(ws)["type"] == "HELLO"
    data = _create_leader(ws)
    token = data.get("guestToken")
    assert token, "CREATE ACK must carry guestToken for a member owner"
    return ws, data, token


@contextlib.contextmanager
def _minted(base_url: str):
    """_mint + guaranteed LEAVE/close on exit."""
    ws, data, token = _mint(base_url)
    try:
        yield ws, data, token
    finally:
        _leave(ws, data["sessionId"])
        ws.close()


def _guest_pair(base_url: str):
    """Full guest pairing: leader CREATE → redeem → guest JOIN{guestToken}
    → leader APPROVE → paired. Returns
    (leader_ws, leader_data, token, guest_ws, guest_data, sa, sb) where
    sa/sb are the post-APPROVE STATE frames."""
    leader_ws, ldata, token = _mint(base_url)
    r = _redeem(base_url, token)
    assert r.status_code == 200, r.text
    guest_ws = _ws_guest(base_url, token)
    assert _recv(guest_ws)["type"] == "HELLO"
    guest_ws.send(_req("JOIN", {"guestToken": token}, rid="jg"))
    gdata = _expect(guest_ws, "ACK")["data"]
    assert gdata["role"] == "follower"
    _expect(leader_ws, "JOIN_REQUEST")
    leader_ws.send(
        _req(
            "APPROVE",
            {"memberId": gdata["memberId"]},
            sid=ldata["sessionId"],
            rid="a",
        )
    )
    sa = _expect(leader_ws, "STATE")
    sb = _expect(guest_ws, "STATE")
    return leader_ws, ldata, token, guest_ws, gdata, sa, sb


def _close_pair(leader_ws, sid, guest_ws):
    _leave(guest_ws, sid)
    guest_ws.close()
    _leave(leader_ws, sid)
    leader_ws.close()


# ── 1-4: token mint + redeem contract ───────────────────────────────


def test_create_ack_includes_guest_token(guest_server):
    with _minted(guest_server) as (_ws, data, token):
        assert GUEST_TOKEN_RE.match(token)
        assert token != data["code"]


def test_guest_redeem_issues_cookie(guest_server):
    with _minted(guest_server) as (_ws, _data, token):
        r = _redeem(guest_server, token)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert isinstance(body.get("expiresInSec"), int)
        assert 0 < body["expiresInSec"] <= 8 * 3600
        assert _guest_cookie_value(r) == token
        sc = [s for s in r.headers.get_list("set-cookie")
              if s.startswith("bookq_guest=")][0].lower()
        assert "httponly" in sc and "samesite=lax" in sc and "secure" in sc


def test_guest_redeem_invalid_and_malformed(guest_server):
    with _minted(guest_server):
        r = _redeem(guest_server, "A" * 32)
        assert r.status_code == 401
        assert r.json()["detail"] == "guest link invalid"
        for bad in ["short", "has space!!", "x" * 200, ""]:
            r = _redeem(guest_server, bad)
            assert r.status_code == 400, (bad, r.status_code, r.text)


def test_guest_redeem_authed_no_cookie(guest_server):
    with _minted(guest_server) as (_ws, _data, token):
        r = _redeem(
            guest_server, token,
            cookie=f"session={_session_cookie(ALICE)}",
        )
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        assert _guest_cookie_value(r) is None


# ── 5-6: guest cookie resolves as owner ─────────────────────────────


def test_guest_cookie_browses_as_owner(guest_server):
    with _minted(guest_server) as (_ws, _data, token):
        cookie = f"bookq_guest={token}"
        # Per-user surfaces stay member-only even though the guest
        # resolves to the owner's identity for library reads — positions
        # would leak the owner's private reading history.
        r = _get(guest_server, "/api/v1/reading-positions", cookie=cookie)
        assert r.status_code == 403
        # Folder visibility is a library-global store, not per-user —
        # guests still need it to honor hidden roots while browsing.
        assert _get(guest_server, "/api/v1/folder-visibility",
                    cookie=cookie).status_code == 200
        r = _get(guest_server, "/", cookie=cookie)
        assert r.status_code == 200
        assert "tile-grid" in r.text and "login-box" not in r.text
        assert _get(guest_server, "/api/v1/tree?depth=1",
                    cookie=cookie).status_code == 200
        img = _get(guest_server, "/image/simple_manga/1.png", cookie=cookie)
        assert img.status_code == 200 and len(img.content) > 0
        assert _get(guest_server, "/api/v1/version",
                    cookie=cookie).status_code == 200


def test_guest_ws_handshake(guest_server):
    with _minted(guest_server) as (_ws, _data, token):
        gws = _ws_guest(guest_server, token)
        try:
            assert _recv(gws)["type"] == "HELLO"
        finally:
            gws.close()


# ── 7-10: link establishment + follower-equivalent ops ──────────────


def test_guest_join_guesttoken_pairs(guest_server):
    leader_ws, ldata, _token, guest_ws, gdata, sa, sb = _guest_pair(
        guest_server)
    sid = ldata["sessionId"]
    try:
        assert gdata["role"] == "follower"
        assert gdata["memberId"] != ldata["memberId"]
        # presence on BOTH sides: the follower is named ゲスト and keyed
        # by the guest's memberId
        for st in (sa, sb):
            assert len(st["presence"]) == 2
        m_l = sa["presence"].get(gdata["memberId"])
        m_g = sb["presence"].get(gdata["memberId"])
        assert m_l and m_l["role"] == "follower" and m_l["name"] == "ゲスト"
        assert m_g and m_g["role"] == "follower" and m_g["name"] == "ゲスト"
    finally:
        _close_pair(leader_ws, sid, guest_ws)


def test_guest_join_bad_token_rejected(guest_server):
    with _minted(guest_server):
        # a bad token as the ONLY credential fails closed at the
        # handshake itself — resolve_ws_user finds no identity → 403
        with pytest.raises(InvalidHandshake):
            _ws_guest(guest_server, "A" * 32)
        # …while a bad token over a valid session identity reaches
        # join_guest and is rejected bad_code like an unknown code
        ws = _ws_user(guest_server, ALICE)
        try:
            assert _recv(ws)["type"] == "HELLO"
            ws.send(_req("JOIN", {"guestToken": "A" * 32}, rid="jg"))
            rej = _expect(ws, "REJECT")
            assert rej["reason"] == "bad_code"
            assert rej["request"] == "JOIN"
        finally:
            ws.close()


def test_guest_follower_ops_converge(guest_server):
    """Guest follower drives the same op set a normal follower does —
    TURN/GOTO/SET_OFFSET/SWAP_SIDES converge; PING/UI_SYNC/SIGNAL relay."""
    leader_ws, ldata, _token, guest_ws, _gdata, sa, _sb = _guest_pair(
        guest_server)
    sid = ldata["sessionId"]
    try:
        rev = _open_and_ready(leader_ws, guest_ws, sid, sa["revision"])
        # TURN from the GUEST side converges both devices
        guest_ws.send(_turn(1, sid=sid, rid="gt", epoch=1))
        st_l, _ = _drain_until(
            leader_ws, lambda f: f.get("type") == "STATE"
            and f.get("spreadId") == "c0:s1")
        st_g, _ = _drain_until(
            guest_ws, lambda f: f.get("type") == "STATE"
            and f.get("spreadId") == "c0:s1")
        assert st_l["revision"] == st_g["revision"]
        rev = st_g["revision"]
        # GOTO absolute op from guest → c0:s2 (page 11.png lives there)
        guest_ws.send(_req(
            "GOTO",
            {"page": {"folder": WORK, "name": "11.png"}},
            sid=sid, rid="gg", epoch=1, base=rev))
        st_l, _ = _drain_until(
            leader_ws, lambda f: f.get("type") == "STATE"
            and f.get("spreadId") == "c0:s2")
        st_g, _ = _drain_until(
            guest_ws, lambda f: f.get("type") == "STATE"
            and f.get("revision") == st_l["revision"])
        rev = st_g["revision"]
        # SET_OFFSET rebuilds the manifest for both (epoch/mv bump)
        guest_ws.send(_req("SET_OFFSET", {"offset": 1},
                           sid=sid, rid="go", epoch=1, base=rev))
        _, seen_l = _drain_until(
            leader_ws, lambda f: f.get("type") == "STATE"
            and f.get("workEpoch") == 2)
        _, seen_g = _drain_until(
            guest_ws, lambda f: f.get("type") == "STATE"
            and f.get("workEpoch") == 2)
        assert any(f["type"] == "MANIFEST" for f in seen_l)
        assert any(f["type"] == "MANIFEST" for f in seen_g)
        # READY round for epoch 2 → reading again
        for ws, rid in ((leader_ws, "rl"), (guest_ws, "rg")):
            ws.send(_req("READY", {"manifestVersion": 2},
                         sid=sid, rid=rid, epoch=2))
        _drain_until(
            leader_ws, lambda f: f.get("type") == "STATE"
            and f.get("phase") == "reading" and f.get("workEpoch") == 2)
        st_g, _ = _drain_until(
            guest_ws, lambda f: f.get("type") == "STATE"
            and f.get("phase") == "reading" and f.get("workEpoch") == 2)
        # SWAP_SIDES flips the projection on both
        guest_ws.send(_req("SWAP_SIDES", {"leaderSide": "right"},
                           sid=sid, rid="gs", epoch=2,
                           base=st_g["revision"]))
        st = _expect(guest_ws, "STATE")
        assert st["leaderSide"] == "right"
        _drain_until(
            leader_ws, lambda f: f.get("type") == "STATE"
            and f.get("leaderSide") == "right")
        # non-sequenced relays identical to a normal follower
        guest_ws.send(_req("PING", {"seq": 5}, sid=sid, rid="gp"))
        assert _expect(guest_ws, "PONG")["seq"] == 5
        guest_ws.send(_req(
            "UI_SYNC",
            {"view": "browse", "path": "x", "top": 1,
             "viewH": 100, "scrollH": 500, "mode": "tile"},
            sid=sid, rid="gu"))
        assert _expect(leader_ws, "UI_SYNC")["from"] == "follower"
        guest_ws.send(_req("SIGNAL", {"kind": "offer", "data": {"sdp": "g"}},
                           sid=sid, rid="gsg"))
        sig = _expect(leader_ws, "SIGNAL")
        assert sig["kind"] == "offer" and sig["from"] == "follower"
        guest_ws.send(_req("SET_ACTIVITY", {"state": "active"},
                           sid=sid, rid="ga"))
        ack, _ = _drain_until(
            guest_ws, lambda f: f.get("type") == "ACK"
            and f.get("request") == "SET_ACTIVITY")
        assert ack["request"] == "SET_ACTIVITY"
    finally:
        _close_pair(leader_ws, sid, guest_ws)


def test_guest_cannot_leader_ops(guest_server):
    leader_ws, ldata, _token, guest_ws, gdata, _sa, _sb = _guest_pair(
        guest_server)
    sid = ldata["sessionId"]
    try:
        guest_ws.send(_req("OPEN_WORK",
                           {"folder": WORK, "page": FIRST_PAGE},
                           sid=sid, rid="go", epoch=0))
        assert _expect(guest_ws, "REJECT")["reason"] == "not_leader"
        guest_ws.send(_req("APPROVE", {"memberId": gdata["memberId"]},
                           sid=sid, rid="gap"))
        assert _expect(guest_ws, "REJECT")["reason"] == "not_leader"
    finally:
        _close_pair(leader_ws, sid, guest_ws)


def test_guest_cannot_create_or_match(guest_server):
    """An unbound guest link must never lead: CREATE/MATCH would mint a
    fresh grant bound to the guest's own session, surviving the leader's
    disconnect."""
    with _minted(guest_server) as (_ws, _data, token):
        g = _ws_guest(guest_server, token)
        try:
            assert _recv(g)["type"] == "HELLO"
            for rtype in ("CREATE", "MATCH"):
                g.send(_req(rtype, rid="x"))
                assert _expect(g, "REJECT")["reason"] == "forbidden"
            # The link is unharmed — a normal JOIN{guestToken} still works.
            g.send(_req("JOIN", {"guestToken": token}, rid="jg"))
            assert _expect(g, "ACK")["data"]["role"] == "follower"
            g.send(_req("LEAVE", rid="lv"))
        finally:
            g.close()


# ── 11: read-only enforcement ───────────────────────────────────────


def test_guest_readonly_blocks_writes(guest_server):
    with _minted(guest_server) as (_ws, _data, token):
        cookie = f"bookq_guest={token}"
        r = httpx.put(
            guest_server + "/api/v1/reading-positions",
            params={"folder": "x"},
            json={"page": 1, "total": 2, "ts": 1},
            headers={"Cookie": cookie}, timeout=10)
        assert r.status_code == 403
        r = httpx.put(
            guest_server + "/api/v1/folder-visibility",
            json={"roots": []},
            headers={"Cookie": cookie}, timeout=10)
        assert r.status_code == 403
        r = httpx.post(
            guest_server + "/api/v1/index/refresh",
            headers={"Cookie": cookie}, timeout=10)
        assert r.status_code == 403
        r = httpx.delete(
            guest_server + "/api/v1/reading-positions",
            headers={"Cookie": cookie}, timeout=10)
        assert r.status_code == 403
        assert _get(guest_server, "/admin", cookie=cookie).status_code == 403
        assert _get(guest_server, "/api/v1/tree?depth=1",
                    cookie=cookie).status_code == 200
        # redeem itself is exempt — last-wins refresh while paired
        assert _redeem(guest_server, token, cookie=cookie).status_code == 200


# ── 12-14: revocation ───────────────────────────────────────────────


def test_guest_revoked_on_leader_leave(guest_server):
    leader_ws, ldata, token, guest_ws, _gdata, _sa, _sb = _guest_pair(
        guest_server)
    sid = ldata["sessionId"]
    cookie = f"bookq_guest={token}"
    try:
        _leave(leader_ws, sid)
        assert _expect(guest_ws, "END")["reason"] == "left"
        leader_ws.close()
        assert _get(guest_server, "/api/v1/tree?depth=1",
                    cookie=cookie).status_code == 401
        assert _redeem(guest_server, token).status_code == 401
        with pytest.raises(InvalidHandshake):
            _ws_guest(guest_server, token)
        r = _get(guest_server, "/", cookie=cookie)
        assert "login-box" in r.text
    finally:
        guest_ws.close()
        try:
            leader_ws.close()
        except Exception:
            pass


def test_guest_revoked_on_new_session(guest_server):
    """Ending the pair kills the grant; a NEW pair mints a NEW token —
    the old one stays dead (no cross-session resurrection)."""
    leader_ws, ldata, old_token, guest_ws, _gdata, _sa, _sb = _guest_pair(
        guest_server)
    sid = ldata["sessionId"]
    old_cookie = f"bookq_guest={old_token}"
    try:
        _leave(leader_ws, sid)
        assert _expect(guest_ws, "END")["reason"] == "left"
        leader_ws.close()
        guest_ws.close()
        assert _get(guest_server, "/api/v1/tree?depth=1",
                    cookie=old_cookie).status_code == 401
        with _minted(guest_server) as (_ws2, _d2, new_token):
            assert new_token != old_token
            assert _redeem(guest_server, new_token).status_code == 200
            assert _get(guest_server, "/api/v1/tree?depth=1",
                        cookie=f"bookq_guest={new_token}").status_code == 200
            assert _redeem(guest_server, old_token).status_code == 401
    finally:
        try:
            guest_ws.close()
        except Exception:
            pass
        try:
            leader_ws.close()
        except Exception:
            pass


def test_guest_second_device_browse_only(guest_server):
    """The same token redeems on a second device (browse as owner) but
    only ONE follower slot exists — the second JOIN is code_consumed."""
    leader_ws, ldata, token, guest_ws, _gdata, _sa, _sb = _guest_pair(
        guest_server)
    sid = ldata["sessionId"]
    second_ws = None
    try:
        r = _redeem(guest_server, token)
        assert r.status_code == 200
        assert _guest_cookie_value(r) == token
        assert _get(guest_server, "/api/v1/tree?depth=1",
                    cookie=f"bookq_guest={token}").status_code == 200
        second_ws = _ws_guest(guest_server, token)
        assert _recv(second_ws)["type"] == "HELLO"
        second_ws.send(_req("JOIN", {"guestToken": token}, rid="jg2"))
        rej = _expect(second_ws, "REJECT")
        assert rej["reason"] == "code_consumed"
    finally:
        if second_ws is not None:
            second_ws.close()
        _close_pair(leader_ws, sid, guest_ws)


# ── browser layer: the QR URL flow end-to-end ───────────────────────


def _authed_page(browser, base_url, user):
    """Browser context carrying a forged session cookie for `user`."""
    ctx = browser.new_context(viewport={"width": 900, "height": 600},
                              has_touch=True)
    ctx.add_cookies([{
        "name": "session",
        "value": _session_cookie(user),
        "domain": "127.0.0.1",
        "path": "/",
    }])
    pg = ctx.new_page()
    pg.goto(base_url)
    pg.wait_for_selector(".tile")
    return ctx, pg


def test_guest_qr_url_pairs_and_turns(browser, guest_server):
    """The QR URL read on an unlogged-in device: login.js redeems →
    viewer shell → hash.js joinGuest → auto-pair → follower ops converge."""
    timeout = 20000
    ctx_l, pl = _authed_page(browser, guest_server, ALICE)
    ctx_g = None
    try:
        pl.evaluate("() => window.PairController.create()")
        pl.wait_for_function(
            "() => !!window.PairController.state.guestToken",
            timeout=timeout)
        guest_token = pl.evaluate(
            "() => window.PairController.state.guestToken")
        code = pl.evaluate("() => window.PairController.state.code")
        assert guest_token and code
        # guest device: fresh context, NO cookies — opens the QR URL
        ctx_g = browser.new_context(
            viewport={"width": 900, "height": 600}, has_touch=True)
        pg = ctx_g.new_page()
        pg.goto(f"{guest_server}/#/join/{code}?gt={guest_token}")
        pg.wait_for_selector(".tile", timeout=timeout)
        for p in (pl, pg):
            p.wait_for_function(
                "() => window.PairController.state.status === 'connected'"
                " || window.PairController.state.status === 'loading'",
                timeout=timeout)
        assert pl.evaluate(
            "() => window.PairController.state.role") == "leader"
        assert pg.evaluate(
            "() => window.PairController.state.role") == "follower"
        # The shell marker the read-only/offline JS keys on — rendered
        # server-side from the grant, pinned end-to-end here.
        assert pg.evaluate("() => window.__BOOKQ_GUEST__") == "true"
        assert pl.evaluate("() => window.__BOOKQ_GUEST__") == "false"
        # leader opens a work → shared spread reaches 'reading' on both
        _open(pl, "long")
        for p in (pl, pg):
            p.wait_for_function(
                "() => window.PairController.state.phase === 'reading'",
                timeout=timeout)
        # guest-side turn converges both devices on the same spread.
        # blur() first: an autofocused panel input would swallow the
        # keydown (app.js ignores keys from INPUT targets).
        s0 = pg.evaluate("() => window.PairController.state.spreadId")
        assert s0
        pg.evaluate(
            "() => document.activeElement"
            " && document.activeElement.blur && document.activeElement.blur()")
        pg.keyboard.press("ArrowLeft")
        # prove the turn actually committed (spreadId moved off s0)…
        pg.wait_for_function(
            "(s) => window.PairController.state.spreadId !== s",
            arg=s0, timeout=timeout)
        # …then both devices sit on the same spread+revision
        _synced(pl, pg, timeout=timeout)
        st_g = pg.evaluate("() => window.PairController.state")
        st_l = pl.evaluate("() => window.PairController.state")
        assert st_g["spreadId"] == st_l["spreadId"] != s0
    finally:
        if ctx_g is not None:
            ctx_g.close()
        ctx_l.close()


def test_guest_invite_button_qr_pairs(browser, guest_server):
    """The REAL invite path: member taps 🔗 → 「ゲストを招待」 — a CREATE
    (MATCH never mints a guestToken), then the QR's encoded composite URL
    is what an unlogged-in device opens to pair."""
    timeout = 20000
    ctx_l, pl = _authed_page(browser, guest_server, ALICE)
    ctx_g = None
    try:
        pl.click("#pair-btn")
        pl.wait_for_selector("#pair-guest-invite", state="visible",
                             timeout=timeout)
        pl.click("#pair-guest-invite")
        pl.wait_for_function(
            "() => { var q = document.getElementById('pair-qr');"
            " return q && !q.hidden && !!q.dataset.qrUrl; }",
            timeout=timeout)
        url = pl.evaluate(
            "() => document.getElementById('pair-qr').dataset.qrUrl")
        assert "/#/join/" in url and "?gt=" in url, url
        # CREATE-origin sessions are never MATCH candidates, so the leader
        # note must point at code/QR — not an auto-connect that can't fire
        note = pl.text_content("#pair-leader-note")
        assert "コードまたはQR" in note, note
        assert "接続ボタン" not in note, note
        # guest device: fresh context, NO cookies — opens the QR URL
        ctx_g = browser.new_context(
            viewport={"width": 900, "height": 600}, has_touch=True)
        pg = ctx_g.new_page()
        pg.goto(url)
        pg.wait_for_selector(".tile", timeout=timeout)
        for p in (pl, pg):
            p.wait_for_function(
                "() => window.PairController.state.status === 'connected'"
                " || window.PairController.state.status === 'loading'",
                timeout=timeout)
        assert pl.evaluate(
            "() => window.PairController.state.role") == "leader"
        assert pg.evaluate(
            "() => window.PairController.state.role") == "follower"
    finally:
        if ctx_g is not None:
            ctx_g.close()
        ctx_l.close()


def test_guest_qr_url_revoked_to_login(browser, guest_server):
    """Leader disconnect kills the grant — the guest's next load lands
    back on the login page (access lost, not just pairing ended)."""
    timeout = 20000
    ctx_l, pl = _authed_page(browser, guest_server, ALICE)
    ctx_g = None
    try:
        pl.evaluate("() => window.PairController.create()")
        pl.wait_for_function(
            "() => !!window.PairController.state.guestToken",
            timeout=timeout)
        guest_token = pl.evaluate(
            "() => window.PairController.state.guestToken")
        code = pl.evaluate("() => window.PairController.state.code")
        ctx_g = browser.new_context(
            viewport={"width": 900, "height": 600}, has_touch=True)
        pg = ctx_g.new_page()
        pg.goto(f"{guest_server}/#/join/{code}?gt={guest_token}")
        pg.wait_for_selector(".tile", timeout=timeout)
        pg.wait_for_function(
            "() => window.PairController.state.status === 'connected'"
            " || window.PairController.state.status === 'loading'",
            timeout=timeout)
        pl.evaluate("() => window.PairController.leave()")
        pg.wait_for_function(
            "() => { var s = window.PairController.state;"
            " return s.status === 'ended' || s.endReason != null; }",
            timeout=timeout)
        pg.reload()
        pg.wait_for_selector(".login-box", timeout=timeout)
        assert pg.locator(".login-btn").is_visible()
    finally:
        if ctx_g is not None:
            ctx_g.close()
        ctx_l.close()


# ── second-wave hardening ────────────────────────────────────────────


def test_guest_redeem_missing_token_400(guest_server):
    """Body validation precedes the authed {"ok":true} shortcut — a
    member's malformed redeem is a 400, not a silent pass."""
    sess = {"Cookie": f"session={_session_cookie(ALICE)}"}
    r = httpx.post(guest_server + "/api/v1/guest/redeem",
                   json={}, headers=sess, timeout=10)
    assert r.status_code == 400
    r = httpx.post(guest_server + "/api/v1/guest/redeem",
                   json={"token": "short"}, headers=sess, timeout=10)
    assert r.status_code == 400
    # Same for an unauthenticated caller.
    r = httpx.post(guest_server + "/api/v1/guest/redeem",
                   json={}, timeout=10)
    assert r.status_code == 400


def test_guest_join_both_keys_malformed(guest_server):
    """JOIN accepts exactly one credential KEY — presence XOR, not
    validity XOR. `{code: <bad>, guestToken: <valid>}` must not slip
    through and silently join on the token."""
    with _minted(guest_server) as (_ws, data, token):
        ws = _ws_user(guest_server, BOB)
        try:
            assert _recv(ws)["type"] == "HELLO"
            ws.send(_req("JOIN",
                         {"code": "bad", "guestToken": token}, rid="jx"))
            rej = _expect(ws, "REJECT")
            assert rej["reason"] == "malformed"
            assert rej["request"] == "JOIN"
            ws.send(_req("JOIN", {"code": "654321",
                                  "guestToken": token}, rid="jy"))
            rej = _expect(ws, "REJECT")
            assert rej["reason"] == "malformed"
            # neither key → malformed too
            ws.send(_req("JOIN", {}, rid="jz"))
            assert _expect(ws, "REJECT")["reason"] == "malformed"
        finally:
            ws.close()


def test_guest_join_code_forbidden(guest_server):
    """A guest-authenticated link can only ride its own grant — the
    6-digit-code JOIN path is members-only (`forbidden`), so a redeemed
    guest can't brute-force into foreign sessions."""
    with _minted(guest_server) as (_ws, data, token):
        g = _ws_guest(guest_server, token)
        try:
            assert _recv(g)["type"] == "HELLO"
            g.send(_req("JOIN", {"code": data["code"]}, rid="j1"))
            rej = _expect(g, "REJECT")
            assert rej["reason"] == "forbidden"
            # The link stays usable — its own grant still joins.
            g.send(_req("JOIN", {"guestToken": token}, rid="j2"))
            assert _expect(g, "ACK")["data"]["role"] == "follower"
            g.send(_req("LEAVE", rid="lv"))
        finally:
            g.close()


def test_match_session_mints_no_guest_token(guest_server):
    """MATCH auto-pair never asked for a QR — no bearer grant is minted
    for an origin='match' session."""
    ws = _ws_user(guest_server, BOB)
    try:
        assert _recv(ws)["type"] == "HELLO"
        ws.send(_req("MATCH", rid="m"))
        data = _expect(ws, "ACK")["data"]
        assert data["role"] == "leader"
        assert not data.get("guestToken")
        ws.send(_req("LEAVE", sid=data["sessionId"], rid="lv"))
    finally:
        ws.close()


def test_guest_library_surface_denied(guest_server):
    """`/api/v1/library/*` GETs carry side effects (upload slots /
    finalize state) — the read-only guard denies the whole subtree."""
    with _minted(guest_server) as (_ws, _data, token):
        cookie = f"bookq_guest={token}"
        assert _get(guest_server, "/api/v1/library/files",
                    cookie=cookie).status_code == 403
        assert _get(guest_server, "/api/v1/library/uploads/x",
                    cookie=cookie).status_code == 403
        # Members are unaffected.
        sess = f"session={_session_cookie(ALICE)}"
        assert _get(guest_server, "/api/v1/library/files",
                    cookie=sess).status_code != 403


def test_guest_responses_marked_no_store(guest_server):
    """Every guest response carries the SW/page-cache exclusion marker
    AND `Cache-Control: private, no-store` — the browser HTTP cache
    ignores the custom header and would otherwise re-serve content past
    grant revocation."""
    with _minted(guest_server) as (_ws, _data, token):
        cookie = f"bookq_guest={token}"
        for path in ("/api/v1/tree?depth=1", "/"):
            r = _get(guest_server, path, cookie=cookie)
            assert r.status_code == 200, (path, r.status_code)
            assert r.headers.get("x-bookq-guest") == "1", path
            cc = r.headers.get("cache-control", "")
            assert "no-store" in cc and "private" in cc, (path, cc)


def test_logout_clears_guest_cookie(guest_server):
    """Logout drops bookq_guest AND kills the grant server-side — a
    fake-cookie clear can't prove the bearer is dead, so this mints a real
    grant and asserts it stops redeeming/serving after logout."""
    with _minted(guest_server) as (_ws, _data, token):
        cookie = f"bookq_guest={token}"
        assert _redeem(guest_server, token).status_code == 200
        assert (
            _get(guest_server, "/api/v1/tree?depth=1", cookie=cookie)
            .status_code == 200
        )
        r = httpx.get(
            guest_server + "/auth/logout",
            headers={
                "Cookie": cookie,
                "Origin": guest_server,
                "Sec-Fetch-Site": "same-origin",
            },
            timeout=10,
            follow_redirects=False,
        )
        cleared = any(
            sc.startswith("bookq_guest=") and "Max-Age=0" in sc
            for sc in r.headers.get_list("set-cookie")
        )
        assert cleared, (
            f"no bookq_guest clear in {r.headers.get_list('set-cookie')}"
        )
        # The grant itself is revoked — a lifted token must not outlive
        # the sign-out even with the cookie value still in hand.
        assert _redeem(guest_server, token).status_code == 401
        assert (
            _get(guest_server, "/api/v1/tree?depth=1", cookie=cookie)
            .status_code == 401
        )


def test_owner_disable_revokes_grant_and_ends_session(guest_server):
    """Disabling a member must revoke their guest grants outright — a
    disable→re-enable cycle can never resurrect a live bearer. The pair
    session they led ends too (guests' whole surface is the session)."""
    host = guest_server.split("://", 1)[1]
    admin_headers = {
        "Cookie": f"session={_session_cookie(ADMIN)}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": f"http://{host}",
        "Sec-Fetch-Site": "same-origin",
    }
    leader_ws, ldata, token, guest_ws, _gdata, _sa, _sb = _guest_pair(
        guest_server)
    cookie = f"bookq_guest={token}"
    try:
        r = httpx.post(
            guest_server + "/admin/users/disable",
            headers=admin_headers,
            content=b"email=" + ALICE["email"].encode(),
            timeout=10,
            follow_redirects=False,
        )
        assert r.status_code in (303, 200), r.status_code
        assert _expect(guest_ws, "END")["reason"] == "owner_disabled"
        assert _expect(leader_ws, "END")["reason"] == "owner_disabled"
        assert _get(guest_server, "/api/v1/tree?depth=1",
                    cookie=cookie).status_code == 401
        assert _redeem(guest_server, token).status_code == 401
        with pytest.raises(InvalidHandshake):
            _ws_guest(guest_server, token)
        # Re-enable must NOT resurrect the dead grant — revoke_owner is a
        # permanent delete, not a membership-condition re-check.
        r = httpx.post(
            guest_server + "/admin/users/add",
            headers=admin_headers,
            content=b"email=" + ALICE["email"].encode(),
            timeout=10,
            follow_redirects=False,
        )
        assert r.status_code in (303, 200), r.status_code
        assert _redeem(guest_server, token).status_code == 401
        assert _get(guest_server, "/api/v1/tree?depth=1",
                    cookie=cookie).status_code == 401
    finally:
        try:
            guest_ws.close()
        except Exception:
            pass
        try:
            leader_ws.close()
        except Exception:
            pass
        # Shared fixture — leave ALICE active for the remaining tests.
        r = httpx.post(
            guest_server + "/admin/users/add",
            headers=admin_headers,
            content=b"email=" + ALICE["email"].encode(),
            timeout=10,
            follow_redirects=False,
        )
        assert r.status_code in (303, 200), r.status_code
