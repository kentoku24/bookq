"""S03 — transport layer tests (#78).

- pairTransport.js evaluated in a real browser (no mirrored re-impl): the
  DataChannel adapter must expose the same interface as the WebSocket adapter,
  and the signaling glue must relay offer/answer/ice through SIGNAL frames.
- The ASGI endpoint itself via in-process TestClient: hello/ping/pong, the
  full pairing frame sequence, and a zero-leak shutdown assertion.
"""
import base64
import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("BOOKQ_TEST_MODE", "true")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.reader_sync import protocol  # noqa: E402

PAIR_TRANSPORT_JS = REPO_ROOT / "app" / "static" / "viewer" / "pairTransport.js"


# ── browser-side adapter + signaling glue ──────────────────────────


@pytest.fixture
def any_browser(request):
    if os.environ.get("BROWSER") == "webkit":
        return request.getfixturevalue("webkit_browser")
    return request.getfixturevalue("browser")


@pytest.fixture
def pt_page(any_browser):
    context = any_browser.new_context()
    pg = context.new_page()
    pg.goto("about:blank")
    pg.add_script_tag(path=str(PAIR_TRANSPORT_JS))
    yield pg
    context.close()


def test_pair_transport_js_exposes_adapter_api(pt_page):
    assert PAIR_TRANSPORT_JS.exists()
    assert pt_page.evaluate(
        "() => !!(window.PairTransport && PairTransport.PROTOCOL_VERSION === %d"
        " && typeof PairTransport.createWebSocketTransport === 'function'"
        " && typeof PairTransport.createDataChannelTransport === 'function'"
        " && typeof PairTransport.request === 'function')"
        % protocol.PROTOCOL_VERSION)


def test_dc_adapter_matches_ws_interface_and_relays_frames(pt_page):
    """Two ends of a fake DataChannel wired back-to-back: the adapter must
    deliver parsed frame objects both ways and expose send/close/onframe/onclose."""
    result = pt_page.evaluate(
        """() => {
          // minimal fake channel pair matching the RTCDataChannel surface
          function fakeChan() {
            return {readyState: 'open', peer: null,
                    send(d){ this.peer.onmessage({data: d}); },
                    close(){ this.readyState='closed'; if(this.onclose) this.onclose();
                             if(this.peer){ this.peer.readyState='closed'; if(this.peer.onclose) this.peer.onclose(); } },
                    onmessage: null, onclose: null};
          }
          const a = fakeChan(), b = fakeChan(); a.peer = b; b.peer = a;
          const ta = PairTransport.createDataChannelTransport(a);
          const tb = PairTransport.createDataChannelTransport(b);
          const got = [];
          tb.onframe = f => got.push(f);
          ta.send(PairTransport.request('PING', {seq: 3}, {sessionId: 's1'}));
          const closed = [];
          ta.onclose = () => closed.push('a'); tb.onclose = () => closed.push('b');
          ta.close();
          return {got, closed,
                  api: ['send','close','onframe','onclose'].every(k => k in ta)};
        }""")
    assert result["api"] is True
    assert len(result["got"]) == 1
    f = result["got"][0]
    assert f["v"] == protocol.PROTOCOL_VERSION and f["type"] == "PING"
    assert f["requestId"] and f["sessionId"] == "s1" and f["payload"] == {"seq": 3}
    assert sorted(result["closed"]) == ["a", "b"]


def test_request_frame_shape(pt_page):
    f = pt_page.evaluate(
        "() => PairTransport.request('TURN', {delta:1,crossChapter:true,fromChapter:null},"
        " {sessionId:'s', workEpoch:2, baseRevision:9})")
    assert f["v"] == protocol.PROTOCOL_VERSION
    assert f["type"] == "TURN"
    assert f["sessionId"] == "s" and f["workEpoch"] == 2 and f["baseRevision"] == 9
    assert isinstance(f["requestId"], str) and f["requestId"]
    protocol.parse_request(f)  # must satisfy the S01 server-side contract


def test_signaling_glue_offer_answer_ice(pt_page):
    """Stub RTCPeerConnection: the glue must produce SIGNAL-ready payloads,
    consume the peer's signals, and surface a DataChannel on both sides."""
    result = pt_page.evaluate(
        """() => new Promise(resolve => {
          const pcs = [];
          function fakePc() {
            const pc = {local: null, remote: null, ice: [],
              onicecandidate: null, ondatachannel: null,
              createDataChannel(n){ const dc = fakePc.mk(n); pc.made = dc; return dc; },
              async createOffer(){ return {type:'offer', sdp:'of'}; },
              async createAnswer(){ return {type:'answer', sdp:'an'}; },
              async setLocalDescription(d){ this.local = d; },
              async setRemoteDescription(d){ this.remote = d;
                    if (d.type === 'offer' && this.ondatachannel)
                       this.ondatachannel({channel: fakePc.mk('sync')}); },
              async addIceCandidate(c){ this.ice.push(c); },
            };
            pcs.push(pc); return pc;
          }
          fakePc.mk = n => ({label:n, readyState:'open', sent:[],
                send(d){this.sent.push(d)}, close(){}, onmessage:null, onclose:null});
          const outA = [], outB = [], gotA = [], gotB = [];
          const A = PairTransport.createSignalingPeer({initiator: true,
                send: s => outB.push(s), pcFactory: fakePc,
                onChannel: dc => { A.dc = PairTransport.createDataChannelTransport(dc); }});
          const B = PairTransport.createSignalingPeer({initiator: false,
                send: s => outA.push(s), pcFactory: fakePc,
                onChannel: dc => { B.dc = PairTransport.createDataChannelTransport(dc); }});
          A.start();
          // host candidate fires asynchronously in a real pc — emit one here
          pcs[0].onicecandidate({candidate: {candidate:'candidate:1 host udp 1.2.3.4', sdpMid:'0'}});
          const pump = () => {
            while (outA.length) { gotA.push(outA[0]); A.applySignal(outA.shift()); }
            while (outB.length) { gotB.push(outB[0]); B.applySignal(outB.shift()); }
          };
          const settle = n => Promise.resolve().then(pump)
            .then(() => n > 0 ? settle(n - 1) : resolve({gotA, gotB, pcs, dcA: !!A.dc, dcB: !!B.dc}));
          settle(8);
        })""")
    # gotB = signals B consumed (emitted by A); gotA = signals A consumed (from B)
    kinds_a = [s["kind"] for s in result["gotA"]]
    kinds_b = [s["kind"] for s in result["gotB"]]
    # initiator emits offer + its host candidate; answerer emits the answer
    assert sorted(kinds_b) == ["ice", "offer"] and kinds_a == ["answer"]
    offerer, answerer = result["pcs"]
    assert offerer["local"]["type"] == "offer"
    assert answerer["remote"]["type"] == "offer"
    assert answerer["local"]["type"] == "answer"
    assert offerer["remote"]["type"] == "answer"
    # host-only ICE: the candidate was relayed verbatim to the answerer
    assert answerer["ice"][0]["candidate"].startswith("candidate:1 host")
    assert result["dcA"] and result["dcB"]


def test_transport_switcher_keeps_state_across_swap(pt_page):
    """Hot-swapping adapters (server-ws <-> webrtc-dc) must not lose the
    single frame handler or queued sends."""
    result = pt_page.evaluate(
        """() => {
          function fakeChan() { return {readyState:'open', peer:null,
            send(d){ if(this.peer) this.peer.onmessage({data:d}); },
            close(){}, onmessage:null, onclose:null}; }
          const a1 = fakeChan(), a2 = fakeChan();
          const sw = PairTransport.createSwitcher();
          const got = [];
          sw.onframe = f => got.push(f);
          sw.setTransport(PairTransport.createDataChannelTransport(a1));
          sw.send(PairTransport.request('PING'));
          a1.peer = fakeChan(); a1.peer.onmessage = () => {};
          // incoming on the live adapter reaches the handler
          a1.onmessage({data: JSON.stringify({v:1, type:'STATE', x:1})});
          // swap: handler stays, sends go to the new adapter
          const sent = [];
          a2.send = d => sent.push(JSON.parse(d));
          sw.setTransport(PairTransport.createDataChannelTransport(a2));
          sw.send(PairTransport.request('PING', {seq:2}));
          a2.onmessage({data: JSON.stringify({v:1, type:'PING', seq:5})});
          return {got, sent};
        }""")
    assert [g["type"] for g in result["got"]] == ["STATE", "PING"]
    assert [s["type"] for s in result["sent"]] == ["PING"]


# ── in-process ASGI endpoint + shutdown leak ───────────────────────


@pytest.fixture
def tc_app(tmp_path, monkeypatch):
    monkeypatch.setenv("DOWNLOADS_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "tc-secret")
    from app.main import create_app
    return create_app()


def _tc_cookie(secret: str, email: str) -> str:
    import itsdangerous
    signer = itsdangerous.TimestampSigner(secret)
    data = base64.b64encode(json.dumps(
        {"user": {"email": email, "name": email, "picture": ""}}).encode())
    return signer.sign(data).decode()


def test_ws_endpoint_inprocess_hello_ping_pong(tc_app):
    from fastapi.testclient import TestClient
    with TestClient(tc_app) as client:
        # cookie works under real auth and is ignored under test-mode auth
        client.cookies.set("session", _tc_cookie("tc-secret", "a@example.com"))
        with client.websocket_connect("/ws/reader-sync") as ws:
            hello = json.loads(ws.receive_text())
            assert hello["type"] == "HELLO"
            ws.send_text(json.dumps({
                "v": 1, "requestId": "p", "sessionId": None, "workEpoch": 0,
                "baseRevision": 0, "type": "PING", "payload": {"seq": 4}}))
            pong = json.loads(ws.receive_text())
            assert pong["type"] == "PONG" and pong["seq"] == 4


def test_ws_endpoint_inprocess_pairing(tc_app):
    from fastapi.testclient import TestClient
    with TestClient(tc_app) as client:
        client.cookies.set("session", _tc_cookie("tc-secret", "a@example.com"))
        with client.websocket_connect("/ws/reader-sync") as wa, \
             client.websocket_connect("/ws/reader-sync") as wb:
            assert json.loads(wa.receive_text())["type"] == "HELLO"
            assert json.loads(wb.receive_text())["type"] == "HELLO"
            wa.send_text(json.dumps({
                "v": 1, "requestId": "c", "sessionId": None, "workEpoch": 0,
                "baseRevision": 0, "type": "CREATE", "payload": {}}))
            ack = json.loads(wa.receive_text())
            assert ack["type"] == "ACK" and ack["data"]["role"] == "leader"
            wb.send_text(json.dumps({
                "v": 1, "requestId": "j", "sessionId": None, "workEpoch": 0,
                "baseRevision": 0, "type": "JOIN",
                "payload": {"code": ack["data"]["code"]}}))
            jack = json.loads(wb.receive_text())
            assert jack["type"] == "ACK"
            jr = json.loads(wa.receive_text())
            assert jr["type"] == "STATE" or jr["type"] == "JOIN_REQUEST"
            while jr["type"] != "JOIN_REQUEST":
                jr = json.loads(wa.receive_text())
            wa.send_text(json.dumps({
                "v": 1, "requestId": "a", "sessionId": ack["data"]["sessionId"],
                "workEpoch": 0, "baseRevision": 0, "type": "APPROVE",
                "payload": {"memberId": jack["data"]["memberId"]}}))
            # both sides end at STATE with 2 members
            for w in (wa, wb):
                f = json.loads(w.receive_text())
                while f["type"] not in ("STATE",):
                    f = json.loads(w.receive_text())
                while len(f.get("presence", {})) != 2:
                    f = json.loads(w.receive_text())
                assert f["type"] == "STATE"


def test_shutdown_leaves_no_reader_sync_tasks(tc_app):
    """After app shutdown: no surviving sessions, links, or async tasks."""
    from fastapi.testclient import TestClient
    reg = tc_app.state.reader_sync
    with TestClient(tc_app) as client:
        client.cookies.set("session", _tc_cookie("tc-secret", "a@example.com"))
        with client.websocket_connect("/ws/reader-sync") as wa, \
             client.websocket_connect("/ws/reader-sync") as wb:
            json.loads(wa.receive_text()); json.loads(wb.receive_text())
            wa.send_text(json.dumps({
                "v": 1, "requestId": "c", "sessionId": None, "workEpoch": 0,
                "baseRevision": 0, "type": "CREATE", "payload": {}}))
            json.loads(wa.receive_text())  # ACK
            json.loads(wa.receive_text())  # STATE
        # sockets closed by context; shutdown runs on TestClient exit
    assert not reg.sessions
    assert not reg.links
    assert reg.live_task_count() == 0
