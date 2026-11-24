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

function makeClient(t) {
  return globalThis.PairClient.createClient({
    url: 'ws://test/ws/reader-sync',
    transportFactory: () => t,
    storage: null,
    persistStorage: null,
    events: null,
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
    expect(join.payload).toEqual({ guestToken: TOKEN });
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
