/* dual-reader client transport adapters + WebRTC signaling glue (#78 / S03).
 * Parent issue #75 §3.1: the data plane is a swappable transport — server-ws
 * (baseline/fallback) or webrtc-dc (low-latency candidate). Frames are the
 * S01 wire contract on both. ICE is host-candidate only (LAN assumption):
 * no STUN/TURN is ever configured.
 *
 * Plain global script; not yet wired into index.html (S09 wires it).
 * Adapter interface (both transports, identical):
 *   .send(frameObj)  — JSON-encoded on the wire
 *   .close()
 *   .onframe = fn(frameObj|null)  — parsed frames; unparseable data is dropped
 *   .onclose = fn()
 *   .name — 'server-ws' | 'webrtc-dc' | caller-provided
 */
(function () {
  'use strict';

  var PROTOCOL_VERSION = 1;

  function _rid() {
    return 'r' + Math.random().toString(36).slice(2, 12) +
      Date.now().toString(36).slice(-4);
  }

  /* Client request frame — shape enforced by app/reader_sync/protocol.py. */
  function request(type, payload, opts) {
    opts = opts || {};
    return {
      v: PROTOCOL_VERSION,
      requestId: _rid(),
      sessionId: opts.sessionId !== undefined ? opts.sessionId : null,
      workEpoch: opts.workEpoch || 0,
      baseRevision: opts.baseRevision || 0,
      type: type,
      payload: payload || {},
    };
  }

  function _adapter(name, raw) {
    var t = {
      name: name,
      raw: raw,
      onframe: null,
      onclose: null,
      send: function (obj) { raw.send(JSON.stringify(obj)); },
      close: function () { try { raw.close(); } catch (e) { /* idempotent */ } },
    };
    raw.onmessage = function (ev) {
      if (!t.onframe) return;
      var f;
      try { f = JSON.parse(ev.data); } catch (e) { return; }
      t.onframe(f);
    };
    raw.onclose = function () { if (t.onclose) t.onclose(); };
    return t;
  }

  /* WebSocket (server-ws). `url` — ws(s)://.../ws/reader-sync. onopen is
   * exposed additionally since a WebSocket opens asynchronously. */
  function createWebSocketTransport(url) {
    var ws = new WebSocket(url);
    var t = _adapter('server-ws', ws);
    t.onopen = null;
    ws.onopen = function () { if (t.onopen) t.onopen(); };
    return t;
  }

  /* RTCDataChannel (webrtc-dc) — or anything send/close/onmessage-shaped,
   * which is what the fake-channel tests use. */
  function createDataChannelTransport(dc) {
    return _adapter('webrtc-dc', dc);
  }

  /* Signaling glue. The server relays {kind:'offer'|'answer'|'ice', data}
   * SIGNAL frames between the paired members; `send` emits such a payload.
   * `pcFactory` defaults to a host-candidate-only RTCPeerConnection
   * (iceServers: []) and exists so tests can inject a stub pc.
   * `onChannel(dc)` fires once on each side with the 'sync' channel. */
  function createSignalingPeer(opts) {
    opts = opts || {};
    var send = opts.send || function () {};
    var onChannel = opts.onChannel || function () {};
    var pcFactory = opts.pcFactory || function () {
      return new RTCPeerConnection({ iceServers: [] });
    };
    var pc = pcFactory();
    var self = {
      pc: pc,
      dc: null,
      start: start,
      applySignal: applySignal,
    };

    pc.onicecandidate = function (ev) {
      if (ev.candidate) {
        var c = ev.candidate;
        send({ kind: 'ice', data: c.toJSON ? c.toJSON() : c });
      }
    };
    pc.ondatachannel = function (ev) {
      self.dc = ev.channel;
      onChannel(ev.channel);
    };

    function start() {
      /* initiator side: own the 'sync' channel and produce the offer */
      if (!opts.initiator) return;
      var dc = pc.createDataChannel('sync');
      self.dc = dc;
      onChannel(dc);
      Promise.resolve()
        .then(function () { return pc.createOffer(); })
        .then(function (offer) {
          return pc.setLocalDescription(offer)
            .then(function () { send({ kind: 'offer', data: offer }); });
        })
        .catch(function () { /* signaling failure -> caller falls back */ });
    }

    function applySignal(sig) {
      if (!sig || typeof sig !== 'object') return;
      var kind = sig.kind, data = sig.data;
      if (kind === 'offer') {
        Promise.resolve()
          .then(function () { return pc.setRemoteDescription(data); })
          .then(function () { return pc.createAnswer(); })
          .then(function (ans) {
            return pc.setLocalDescription(ans)
              .then(function () { send({ kind: 'answer', data: ans }); });
          })
          .catch(function () {});
      } else if (kind === 'answer') {
        Promise.resolve()
          .then(function () { return pc.setRemoteDescription(data); })
          .catch(function () {});
      } else if (kind === 'ice') {
        Promise.resolve()
          .then(function () { return pc.addIceCandidate(data); })
          .catch(function () {});
      }
    }

    return self;
  }

  /* Hot-swap point for transport switching (server-ws <-> webrtc-dc): keeps
   * one onframe/onclose handler while send() follows the active adapter.
   * The switch itself is server-driven at revision boundaries (S03). */
  function createSwitcher() {
    var sw = {
      transport: null,
      onframe: null,
      onclose: null,
      setTransport: function (t) {
        if (sw.transport) {
          sw.transport.onframe = null;
          sw.transport.onclose = null;
        }
        sw.transport = t;
        if (t) {
          t.onframe = function (f) { if (sw.onframe) sw.onframe(f); };
          t.onclose = function () { if (sw.onclose) sw.onclose(); };
        }
      },
      send: function (obj) { if (sw.transport) sw.transport.send(obj); },
      close: function () {
        if (sw.transport) sw.transport.close();
        sw.transport = null;
      },
    };
    return sw;
  }

  globalThis.PairTransport = {
    PROTOCOL_VERSION: PROTOCOL_VERSION,
    request: request,
    createWebSocketTransport: createWebSocketTransport,
    createDataChannelTransport: createDataChannelTransport,
    createSignalingPeer: createSignalingPeer,
    createSwitcher: createSwitcher,
  };
})();
