import { describe, it, expect, beforeEach } from 'vitest';

// pairClient.js is an IIFE publishing globalThis.PairClient — this file
// loads it directly (setup.js does not include it) and drives the guest
// entry path through an injected fake transport: joinGuest must emit
// JOIN{guestToken} after HELLO, and CREATE's ACK must surface
// data.guestToken on state().
import '../../static/viewer/pairClient.js';

const TOKEN = 'g'.repeat(32);

function fakeTransport() {
  return {
    onframe: null, onclose: null, onopen: null,
    sent: [],
    send(o) { this.sent.push(o); },
    close() { this.closed = true; },
    emit(f) { if (this.onframe) this.onframe(f); },
  };
}

function makeClient(t, onEvent) {
  return globalThis.PairClient.createClient({
    url: 'ws://test/ws/reader-sync',
    transportFactory: () => t,
    storage: null,
    persistStorage: null,
    events: null,
    onEvent: onEvent || null,
    features: [globalThis.PairClient.CAP_MEMBER_DRIVE],
  });
}

function helloThen(t, client) {
  // The server speaks first: HELLO releases the queued entry frame.
  t.emit({ v: 1, type: 'HELLO' });
}

describe('pairClient guest access', () => {
  let t, client;

  beforeEach(() => {
    t = fakeTransport();
    client = makeClient(t);
  });

  it('joinGuest sends JOIN{guestToken} and binds as follower', async () => {
    const p = client.joinGuest(TOKEN);
    helloThen(t);
    const join = t.sent.find((f) => f.type === 'JOIN');
    expect(join).toBeTruthy();
    expect(join.payload).toEqual({
      guestToken: TOKEN,
      // ADR 0004 capability advertisement rides every admission payload.
      features: ['member-drive-v1'],
    });
    expect(join.sessionId).toBeNull();
    const ack = {
      v: 1, type: 'ACK', request: 'JOIN', requestId: join.requestId,
      data: {
        sessionId: 's1', memberId: 'm1', role: 'follower',
        resumeToken: 'rt1',
      },
    };
    t.emit(ack);
    const d = await p;
    expect(d.memberId).toBe('m1');
    expect(d.role).toBe('follower');
    expect(client.state.role).toBe('follower');
    expect(client.state.sessionId).toBe('s1');
  });


  it('does not advertise member-drive unless the composition opts in', async () => {
    const bare = globalThis.PairClient.createClient({
      url: 'ws://test/ws/reader-sync',
      transportFactory: () => t,
      storage: null,
      persistStorage: null,
      events: null,
    });
    const p = bare.joinGuest(TOKEN);
    helloThen(t);
    const join = t.sent.find((f) => f.type === 'JOIN');
    expect(join).toBeTruthy();
    expect(join.payload).toEqual({guestToken: TOKEN});
    t.emit({
      v: 1, type: 'ACK', request: 'JOIN', requestId: join.requestId,
      data: {sessionId: 's0', memberId: 'm0', role: 'follower'},
    });
    await p;
    bare.destroy();
  });

  it('joinGuest rejects malformed tokens without opening a link', async () => {
    let opened = false;
    const c = globalThis.PairClient.createClient({
      url: 'ws://test/ws/reader-sync',
      transportFactory: () => { opened = true; return fakeTransport(); },
      storage: null, persistStorage: null, events: null,
    });
    await expect(c.joinGuest('short')).rejects.toMatchObject({
      code: 'ERR_MALFORMED',
    });
    await expect(c.joinGuest('has space!!')).rejects.toMatchObject({
      code: 'ERR_MALFORMED',
    });
    await expect(c.joinGuest('x'.repeat(200))).rejects.toMatchObject({
      code: 'ERR_MALFORMED',
    });
    expect(opened).toBe(false);
    c.destroy();
  });

  it('joinGuest on a destroyed client rejects ERR_STATE', async () => {
    client.destroy();
    await expect(client.joinGuest(TOKEN)).rejects.toMatchObject({
      code: 'ERR_STATE',
    });
  });

  it('create() surfaces ACK data.guestToken on state()', async () => {
    const p = client.create();
    helloThen(t);
    const create = t.sent.find((f) => f.type === 'CREATE');
    expect(create).toBeTruthy();
    t.emit({
      v: 1, type: 'ACK', request: 'CREATE', requestId: create.requestId,
      data: {
        sessionId: 's9', memberId: 'm9', role: 'leader',
        code: '123456', guestToken: TOKEN, resumeToken: 'rt9',
      },
    });
    await p;
    expect(client.state.guestToken).toBe(TOKEN);
    expect(client.state.code).toBe('123456');
    // A new entry cycle on the SAME client clears it — _startEntry's
    // _resetSession must drop the grant so a code-joined member doesn't
    // keep exposing the old leader's guest token on state.
    const p2 = client.join('654321');
    expect(client.state.guestToken).toBeNull();   // cleared synchronously
    t.emit({ v: 1, type: 'HELLO' });
    const j2 = t.sent.find((f) => f.type === 'JOIN'
      && f.payload.code === '654321');
    t.emit({
      v: 1, type: 'ACK', request: 'JOIN', requestId: j2.requestId,
      data: { sessionId: 's2', memberId: 'm2', role: 'follower' },
    });
    await p2;
    expect(client.state.guestToken).toBeNull();
  });

  it('joinGuest supersedes an in-flight entry like join() does', async () => {
    const p1 = client.join('123456');
    t.emit({ v: 1, type: 'HELLO' });
    expect(t.sent.some((f) => f.type === 'JOIN'
      && f.payload.code === '123456')).toBe(true);
    // second entry before the first ACKs — first promise rejects
    const p2 = client.joinGuest(TOKEN);
    await expect(p1).rejects.toMatchObject({ code: 'ERR_STATE' });
    // the new link needs its own HELLO before the entry frame goes out
    t.emit({ v: 1, type: 'HELLO' });
    const guestJoin = t.sent.find((f) => f.type === 'JOIN'
      && f.payload.guestToken === TOKEN);
    expect(guestJoin).toBeTruthy();
    t.emit({
      v: 1, type: 'ACK', request: 'JOIN', requestId: guestJoin.requestId,
      data: { sessionId: 's3', memberId: 'm3', role: 'follower' },
    });
    await p2;
  });
});

describe('PairClient.peekActivePair / supportsGuest', () => {
  const REC = {
    sessionId: 's1', memberId: 'm1', role: 'leader',
    resumeToken: 'rt', hb: 1000, tabId: 'tabA',
  };
  const store = (val) => ({
    getItem: (k) => (k === 'bookq.readerSync.persist.v1' ? val : null),
  });
  const rec = (over) => JSON.stringify(Object.assign({}, REC, over));

  it('module declares the guest capability marker', () => {
    expect(globalThis.PairClient.supportsGuest).toBe(true);
  });

  it('fresh heartbeat → live record regardless of owner tab', () => {
    const d = globalThis.PairClient.peekActivePair(
      store(rec()), () => 1000 + 3999, 4000);
    expect(d).toBeTruthy();
    expect(d.sessionId).toBe('s1');
  });

  it('stale heartbeat → null (dead owner is peekPersistedResume territory)', () => {
    expect(globalThis.PairClient.peekActivePair(
      store(rec()), () => 1000 + 4001, 4000)).toBeNull();
  });

  it('missing/malformed/absent store → null, never throws', () => {
    expect(globalThis.PairClient.peekActivePair(store(null), () => 0, 4000))
      .toBeNull();
    expect(globalThis.PairClient.peekActivePair(store('{bad'), () => 0, 4000))
      .toBeNull();
    expect(globalThis.PairClient.peekActivePair(
      store(rec({ hb: 'x' })), () => 0, 4000)).toBeNull();
  });
});

describe('guestToken storage hygiene', () => {
  const memStore = () => {
    const m = {};
    return {
      m,
      getItem: (k) => (k in m ? m[k] : null),
      setItem: (k, v) => { m[k] = v; },
      removeItem: (k) => { delete m[k]; },
    };
  };

  it('CREATE ACK stores guestToken in sessionStorage creds, NOT the persist record', async () => {
    const session = memStore(), persist = memStore();
    const t = fakeTransport();
    const c = globalThis.PairClient.createClient({
      url: 'ws://test/ws/reader-sync',
      transportFactory: () => t,
      storage: session,
      persistStorage: persist,
      events: null,
    });
    const p = c.create();
    helloThen(t);
    const create = t.sent.find((f) => f.type === 'CREATE');
    t.emit({
      v: 1, type: 'ACK', request: 'CREATE', requestId: create.requestId,
      data: {
        sessionId: 's9', memberId: 'm9', role: 'leader',
        code: '123456', guestToken: TOKEN, resumeToken: 'rt9',
      },
    });
    await p;
    // Tab-scoped creds keep the grant + the join code: a same-tab RESUME
    // re-arms the composite `?gt=` QR (the code alone is not bearer
    // material — it is shown openly in the leader panel).
    const creds = JSON.parse(session.m['bookq.readerSync.v1']);
    expect(creds.guestToken).toBe(TOKEN);
    expect(creds.code).toBe('123456');
    // The cross-tab localStorage record must NOT carry the bearer — it is
    // shared across tabs, survives the browser, and is XSS-readable. The
    // code alone is fine there: a foreign tab's adoption degrades to the
    // code-only QR instead of '------' with no QR at all.
    const rec = JSON.parse(persist.m['bookq.readerSync.persist.v1']);
    expect(rec).toBeTruthy();
    expect(rec.guestToken).toBeUndefined();
    expect(rec.code).toBe('123456');
  });

  it('same-tab RESUME restores code + guestToken — composite QR survives a reload', async () => {
    const session = memStore(), persist = memStore();
    session.m['bookq.readerSync.v1'] = JSON.stringify({
      sessionId: 's9', memberId: 'm9', role: 'leader',
      resumeToken: 'rt9', guestToken: TOKEN, code: '123456',
    });
    const t = fakeTransport();
    const c = globalThis.PairClient.createClient({
      url: 'ws://test/ws/reader-sync',
      transportFactory: () => t,
      storage: session, persistStorage: persist, events: null,
    });
    const p = c.resume();
    t.emit({ v: 1, type: 'HELLO' });
    const resume = t.sent.find((f) => f.type === 'RESUME');
    expect(resume).toBeTruthy();
    // Server reality: the RESUME ACK carries neither code nor guestToken.
    t.emit({
      v: 1, type: 'ACK', request: 'RESUME', requestId: resume.requestId,
      data: {
        sessionId: 's9', memberId: 'm9', role: 'leader',
        resumeToken: 'rt9b',
      },
    });
    await p;
    expect(c.state.code).toBe('123456');
    expect(c.state.guestToken).toBe(TOKEN);
  });

  it('foreign-tab adoption restores code WITHOUT guestToken — code-only QR', async () => {
    // The dead owner wrote a persist record: code survives (not bearer
    // material) but the grant token correctly never left sessionStorage.
    const session = memStore(), persist = memStore();
    persist.m['bookq.readerSync.persist.v1'] = JSON.stringify({
      sessionId: 's9', memberId: 'm9', role: 'leader',
      resumeToken: 'rt9', code: '123456',
      hb: Date.now() - 20000,               // > RESUME_HB_STALE_MS, < window
      tabId: 'dead-tab',
    });
    const t = fakeTransport();
    const c = globalThis.PairClient.createClient({
      url: 'ws://test/ws/reader-sync',
      transportFactory: () => t,
      storage: session, persistStorage: persist, events: null,
    });
    const p = c.resume();
    t.emit({ v: 1, type: 'HELLO' });
    const resume = t.sent.find((f) => f.type === 'RESUME');
    expect(resume).toBeTruthy();
    t.emit({
      v: 1, type: 'ACK', request: 'RESUME', requestId: resume.requestId,
      data: {
        sessionId: 's9', memberId: 'm9', role: 'leader',
        resumeToken: 'rt9b',
      },
    });
    await p;
    expect(c.state.code).toBe('123456');      // code-only QR restorable
    expect(c.state.guestToken).toBeNull();    // grant stays with dead tab
  });
});

describe('pairClient data-channel input authentication', () => {
  async function bound(role, events) {
    const t = fakeTransport();
    const client = makeClient(t, events ? events.push.bind(events) : null);
    const p = role === 'leader' ? client.create() : client.join('123456');
    helloThen(t);
    const entry = t.sent.find((f) => f.type === (role === 'leader' ? 'CREATE' : 'JOIN'));
    t.emit({
      v: 1, type: 'ACK', request: entry.type, requestId: entry.requestId,
      data: {
        sessionId: 's1', memberId: 'm-' + role, role: role,
        trust: 'member', resumeToken: 'rt1', features: ['member-drive-v1'],
      },
    });
    await p;
    return { t, client };
  }

  function openDc(client) {
    const dc = fakeTransport();
    client.attachDataChannel(dc);
    if (dc.onopen) dc.onopen();
    return dc;
  }

  function stateFrame(rev, plane) {
    return {
      v: 1, type: 'STATE', sessionId: 's1', revision: rev, workEpoch: 0,
      manifestVersion: 0, phase: 'selecting', spreadId: null, offset: 0,
      leaderSide: 'left', dataPlane: plane || 'server-ws',
      features: ['member-drive-v1'], presence: {},
    };
  }

  function forgedBatch(dc) {
    dc.emit({ v: 1, type: 'UI_SYNC', sessionId: 's1', from: 'follower',
              payload: {view: 'browse', top: 1, viewH: 100} });
    dc.emit(Object.assign(stateFrame(99), {sessionId: 's1'}));
    dc.emit({ v: 1, type: 'END', sessionId: 's1', reason: 'left' });
  }

  it('leader never dispatches forged server frames from the DC, in either plane', async () => {
    const events = [];
    const { t, client } = await bound('leader', events);
    const dc = openDc(client);
    t.emit(stateFrame(1, 'webrtc-dc'));
    expect(client.state.transport).toBe('webrtc-dc');

    // A forged TRANSPORT_STATE downgrade must not re-open the injection path.
    dc.emit({ v: 1, type: 'TRANSPORT_STATE', sessionId: 's1',
              dataPlane: 'server-ws', revision: 2 });
    expect(client.state.transport).toBe('webrtc-dc');

    // And once the genuine server WS did downgrade the plane, the same forged
    // server-originated frames remain inert (the regression window this closes).
    t.emit({ v: 1, type: 'TRANSPORT_STATE', sessionId: 's1',
             dataPlane: 'server-ws', revision: 2 });
    expect(client.state.transport).toBe('server-ws');
    forgedBatch(dc);
    expect(client.state.revision).toBe(1);
    expect(client.state.endReason).toBeNull();
    expect(events.some((e) => e.type === 'ui-sync')).toBe(false);
    client.destroy();
  });

  it('leader relays only whitelisted sequenced requests from DC regardless of plane', async () => {
    const { t, client } = await bound('leader');
    const dc = openDc(client);
    expect(client.state.transport).toBe('server-ws');
    const before = t.sent.length;
    dc.emit({
      v: 1, requestId: 'dc-open', sessionId: 's1', workEpoch: 0,
      baseRevision: 0, type: 'OPEN_WORK',
      payload: {folder: 'w', page: {folder: 'w', name: 'p.png'}},
    });
    dc.emit({
      v: 1, requestId: 'dc-control', sessionId: 's1', workEpoch: 0,
      baseRevision: 0, type: 'SET_ACTIVITY', payload: {state: 'active'},
    });
    const relayed = t.sent.slice(before).find((f) => f.requestId === 'dc-open');
    expect(relayed).toBeTruthy();
    expect(relayed.payload.origin).toBe('follower');
    expect(t.sent.slice(before).some((f) => f.requestId === 'dc-control')).toBe(false);
    client.destroy();
  });

  it('follower drops every inbound DC frame, including plausible server frames', async () => {
    const events = [];
    const { t, client } = await bound('follower', events);
    const dc = openDc(client);
    t.emit(stateFrame(1, 'webrtc-dc'));
    expect(client.state.transport).toBe('webrtc-dc');
    const before = t.sent.length;
    forgedBatch(dc);
    dc.emit({
      v: 1, requestId: 'dc-open', sessionId: 's1', workEpoch: 0,
      baseRevision: 0, type: 'OPEN_WORK',
      payload: {folder: 'w', page: {folder: 'w', name: 'p.png'}},
    });
    dc.emit({ v: 1, type: 'TRANSPORT_STATE', sessionId: 's1',
              dataPlane: 'server-ws', revision: 2 });
    expect(client.state.revision).toBe(1);
    expect(client.state.transport).toBe('webrtc-dc');
    expect(client.state.endReason).toBeNull();
    expect(events.some((e) => e.type === 'ui-sync')).toBe(false);
    expect(t.sent.slice(before).some((f) => f.requestId === 'dc-open')).toBe(false);
    client.destroy();
  });
});

describe('pairClient member-drive capability gate', () => {
  function boundFollower(trust) {
    const t = fakeTransport();
    const client = makeClient(t);
    const p = client.join('123456');
    helloThen(t);
    const join = t.sent.find((f) => f.type === 'JOIN');
    t.emit({
      v: 1, type: 'ACK', request: 'JOIN', requestId: join.requestId,
      data: {
        sessionId: 's1', memberId: 'm-follower', role: 'follower',
        trust: trust, resumeToken: 'rt1', features: ['member-drive-v1'],
      },
    });
    return { t, client, p };
  }

  function stateWith(peerFeatures) {
    return {
      v: 1, type: 'STATE', sessionId: 's1', revision: 2, workEpoch: 0,
      manifestVersion: 0, phase: 'selecting', spreadId: null, offset: 0,
      leaderSide: 'left', dataPlane: 'server-ws', features: ['member-drive-v1'],
      presence: {
        'm-leader': {
          role: 'leader', status: 'active', trust: 'member', ready: false,
          features: peerFeatures,
        },
        'm-follower': {
          role: 'follower', status: 'active', trust: 'member', ready: false,
          features: ['member-drive-v1'],
        },
      },
    };
  }

  it('member follower may OPEN_WORK only when the capability intersection exists', async () => {
    const { t, client, p } = boundFollower('member');
    await p;
    const intent = {
      type: 'openWork', folder: 'work',
      page: { folder: 'work', name: 'p001.png' },
    };
    expect(client.sendIntent(intent)).toBe(false);   // no STATE yet
    t.emit(stateWith(['member-drive-v1']));
    expect(client.state.memberDrive).toBe(true);
    expect(client.state.canDrive).toBe(true);
    expect(client.sendIntent(intent)).toBe(true);
    const open = t.sent.find((f) => f.type === 'OPEN_WORK');
    expect(open).toBeTruthy();
    const downgraded = stateWith([]);
    downgraded.revision = 3;
    t.emit(downgraded);
    expect(client.state.memberDrive).toBe(false);
    expect(client.sendIntent(intent)).toBe(false);
    client.destroy();
  });

  it('public canDrive is legacy-leader or member-drive, never guest', async () => {
    const t = fakeTransport();
    const client = makeClient(t);
    const p = client.create();
    helloThen(t);
    const create = t.sent.find((f) => f.type === 'CREATE');
    t.emit({
      v: 1, type: 'ACK', request: 'CREATE', requestId: create.requestId,
      data: {
        sessionId: 's1', memberId: 'm-leader', role: 'leader',
        trust: 'member', resumeToken: 'rt1', features: ['member-drive-v1'],
      },
    });
    await p;
    const legacy = stateWith([]);
    legacy.presence['m-follower'].features = [];
    t.emit(legacy);
    // The legacy leader remains a driver even with no capability intersection;
    // mode-specific consumers use memberDrive to tell which rule supplied it.
    expect(client.state.memberDrive).toBe(false);
    expect(client.state.canDrive).toBe(true);
    client.destroy();
  });

  it('guest trust cannot drive even when both runtimes advertise the feature', async () => {
    const { t, client, p } = boundFollower('guest');
    await p;
    const st = stateWith(['member-drive-v1']);
    st.presence['m-follower'].trust = 'guest';
    t.emit(st);
    expect(client.state.memberDrive).toBe(true);
    expect(client.state.canDrive).toBe(false);
    expect(client.sendIntent({
      type: 'openWork', folder: 'work',
      page: { folder: 'work', name: 'p001.png' },
    })).toBe(false);
    client.destroy();
  });
});
