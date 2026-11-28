import { describe, it, expect, beforeEach } from 'vitest';

// Wire-level composition tests through the REAL PairClient serialization
// path — a mocked sendUiSync cannot catch the client whitelist dropping
// additive fields, so these drive the genuine sendUiSync/entry frames
// through an injected fake transport.
import '../../static/viewer/pairClient.js';

function fakeTransport() {
  return {
    onframe: null, onclose: null, onopen: null,
    sent: [],
    send(o) { this.sent.push(o); },
    close() { this.closed = true; },
    emit(f) { if (this.onframe) this.onframe(f); },
  };
}

function makeClient(t, opts) {
  return globalThis.PairClient.createClient(Object.assign({
    url: 'ws://test/ws/reader-sync',
    transportFactory: () => t,
    storage: null,
    persistStorage: null,
    events: null,
  }, opts || {}));
}

function pairUp(t, client, ackData) {
  const p = client.create();
  t.emit({v: 1, type: 'HELLO'});
  const create = t.sent.find((f) => f.type === 'CREATE');
  const data = Object.assign({
    sessionId: 's1', memberId: 'm1', role: 'leader',
    resumeToken: 'rt1', code: '123456',
  }, ackData || {});
  t.emit({
    v: 1, type: 'ACK', request: 'CREATE', requestId: create.requestId,
    data: data,
  });
  // Paired state: presence must show both members for _paired() to hold.
  const presence = {
    m1: {role: 'leader', status: 'active'},
    m2: {role: 'follower', status: 'active'},
  };
  if (data.trust) {
    presence.m1.trust = data.trust;
    presence.m2.trust = 'member';
  }
  t.emit({
    v: 1, type: 'STATE', sessionId: 's1', revision: 1, workEpoch: 0,
    manifestVersion: 0, phase: 'selecting', spreadId: null, offset: 0,
    leaderSide: 'right', presence: presence,
  });
  return p;
}

describe('pairClient wire composition', () => {
  let t, client;

  beforeEach(() => {
    t = fakeTransport();
    client = makeClient(t);
  });

  it('sendUiSync forwards a positive-integer browseGeneration to the wire', async () => {
    await pairUp(t, client, {trust: 'member'});
    t.sent.length = 0;
    client.sendUiSync({
      view: 'browse', path: null, top: 10, viewH: 100,
      mode: 'tile', browseGeneration: 2,
    });
    const ui = t.sent.find((f) => f.type === 'UI_SYNC');
    expect(ui).toBeTruthy();
    expect(ui.payload.browseGeneration).toBe(2);
  });

  it('sendUiSync strips malformed generation tags rather than sending them', async () => {
    await pairUp(t, client, {trust: 'member'});
    for (const bad of ['2', 0, -1, 2.5, NaN]) {
      t.sent.length = 0;
      client.sendUiSync({
        view: 'browse', path: null, top: 10, viewH: 100,
        browseGeneration: bad,
      });
      const ui = t.sent.find((f) => f.type === 'UI_SYNC');
      expect(ui, `tag ${bad} must not reach the wire`).toBeTruthy();
      expect('browseGeneration' in ui.payload).toBe(false);
    }
  });

  it('infers legacy member trust when the server never emits trust', async () => {
    // Pre-trust server rollback: the ACK carries no trust field at all.
    // Guests cannot exist in such a deployment, so absence is unambiguous —
    // the leader must keep legacy drive authority end-to-end.
    await pairUp(t, client, {});   // no trust in the ACK
    expect(client.state.trust).toBe('member');
    expect(client.state.canDrive).toBe(true);

    // And the client's own send gate must pass OPEN_WORK/browse frames —
    // a leader dead-ended here is the rollback breakage this prevents.
    t.sent.length = 0;
    client.sendUiSync({view: 'browse', path: null, top: 1, viewH: 100});
    expect(t.sent.some((f) => f.type === 'UI_SYNC')).toBe(true);
  });

  it('keeps explicit guest trust authoritative — inference never upgrades it', async () => {
    await pairUp(t, client, {trust: 'guest'});
    expect(client.state.trust).toBe('guest');
    expect(client.state.canDrive).toBe(false);
  });

  it('keeps explicit member trust authoritative on trust-aware servers', async () => {
    await pairUp(t, client, {trust: 'member'});
    expect(client.state.trust).toBe('member');
    expect(client.state.canDrive).toBe(true);
  });

  it('emits features on CREATE/MATCH/RESUME when the composition opts in', async () => {
    // CREATE — driven through the opted client's own transport.
    const ct = fakeTransport();
    const c2 = makeClient(ct, {features: [globalThis.PairClient.CAP_MEMBER_DRIVE]});
    const p = c2.create();
    ct.emit({v: 1, type: 'HELLO'});
    const create = ct.sent.find((f) => f.type === 'CREATE');
    expect(create.payload.features).toEqual(['member-drive-v1']);
    ct.emit({
      v: 1, type: 'ACK', request: 'CREATE', requestId: create.requestId,
      data: {sessionId: 's9', memberId: 'm9', role: 'leader',
             resumeToken: 'rt9', code: '654321', trust: 'member'},
    });
    await p;

    // MATCH — one-tap pairing advertises on the entry frame too.
    const mt = fakeTransport();
    const c4 = makeClient(mt, {features: [globalThis.PairClient.CAP_MEMBER_DRIVE]});
    const mp = c4.match();
    mt.emit({v: 1, type: 'HELLO'});
    const matchf = mt.sent.find((f) => f.type === 'MATCH');
    expect(matchf, 'MATCH entry frame must be sent').toBeTruthy();
    expect(matchf.payload.features).toEqual(['member-drive-v1']);
    mt.emit({
      v: 1, type: 'ACK', request: 'MATCH', requestId: matchf.requestId,
      data: {sessionId: 's10', memberId: 'm10', role: 'leader',
             resumeToken: 'rt10', code: '111111', trust: 'member'},
    });
    await mp;

    // RESUME — same advertisement on the resume entry frame. resume()
    // reads credentials from the injected storage, not arguments.
    const creds = JSON.stringify({
      sessionId: 's9', memberId: 'm9', role: 'leader', resumeToken: 'rt9',
    });
    const storage = {
      getItem: (k) => (k === 'bookq.readerSync.v1' ? creds : null),
      setItem: () => {}, removeItem: () => {},
    };
    const rt = fakeTransport();
    const c3 = makeClient(rt, {
      features: [globalThis.PairClient.CAP_MEMBER_DRIVE],
      storage: storage,
    });
    const rp = c3.resume();
    rt.emit({v: 1, type: 'HELLO'});
    const resume = rt.sent.find((f) => f.type === 'RESUME');
    expect(resume.payload.features).toEqual(['member-drive-v1']);
    rt.emit({
      v: 1, type: 'ACK', request: 'RESUME', requestId: resume.requestId,
      data: {sessionId: 's9', memberId: 'm9', role: 'leader', trust: 'member'},
    });
    await rp;
    c2.destroy();
    c4.destroy();
    c3.destroy();
  });
});
