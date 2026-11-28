import { describe, it, expect, beforeAll, beforeEach, vi } from 'vitest';

// pairSync.js is a plain IIFE with a test-only seam. It is not part of the
// shared setup because its singleton subscriptions depend on a PairController
// composition; install the minimal DOM/controller shell before importing it.
describe('pairSync browse generations', () => {
  let hook;
  let sendUiSync;

  function state(over) {
    return Object.assign({
      sessionId: 's1', status: 'active', phase: 'selecting',
      role: 'follower', memberId: 'm-follower',
      memberDrive: true, browse: true, browseGeneration: 1,
      browseOpener: 'm-follower', browseOpenerRole: 'follower',
      presence: {
        'm-leader': {role: 'leader', status: 'active'},
        'm-follower': {role: 'follower', status: 'active'},
      },
    }, over || {});
  }

  function drive(st) {
    window.PairController.state = st;
    hook.onState(st);
  }

  function scroller() {
    return document.getElementById('sidebar-scroll');
  }

  function localScroll(top) {
    const sc = scroller();
    sc.scrollTop = top;
    sc.dispatchEvent(new Event('scroll'));
  }

  function sentPayloads() {
    return sendUiSync.mock.calls.map((c) => c[0]);
  }

  beforeAll(async () => {
    document.body.innerHTML =
      '<div id="sidebar" class="open">' +
      '  <div id="sidebar-scroll"><div id="tile-grid"></div></div>' +
      '</div>';
    window.__BOOKQ_TEST_MODE__ = 'true';
    sendUiSync = vi.fn(() => true);
    window.PairController = {
      state: null,
      subscribe() { return function () {}; },
      onEvent() { return function () {}; },
      sendUiSync,
    };
    await import('../../static/browse/pairSync.js');
    hook = window.__pairBrowseSync;
    expect(hook).toBeTruthy();
  });

  beforeEach(() => {
    hook.reset();
    sendUiSync.mockClear();
    window.PairController.state = null;
    window.currentBrowsePath = null;
    window.viewMode = 'tile';
    document.getElementById('sidebar').classList.add('open');
    scroller().scrollTop = 0;
  });

  it('withholds sends after a generation boundary until the opener frame arrives', () => {
    drive(state());
    expect(sendUiSync).toHaveBeenCalledTimes(1);   // gen-1 opener seed

    // The close→reopen happened while this client was offline, so both the
    // old and new observable states are open. The follower is not the gen-2
    // opener: even a real local scroll must not emit its stale seed.
    sendUiSync.mockClear();
    drive(state({
      browseGeneration: 2,
      browseOpener: 'm-leader', browseOpenerRole: 'leader',
    }));
    localScroll(30);
    hook.flush();   // bypass the send throttle; only the unlock gate may stop it
    expect(sendUiSync).not.toHaveBeenCalled();

    // The opener's first frame unlocks this follower. A subsequent local
    // scroll is the behavior that proves sending resumed.
    hook.applyRemote({view: 'browse', path: null, top: 0, viewH: 100, mode: 'tile'});
    localScroll(42);
    hook.flush();   // deterministic edge of the normal send throttle
    expect(sendUiSync).toHaveBeenCalledTimes(1);
    expect(sentPayloads()[0]).toMatchObject({
      view: 'browse', path: null, top: 42, mode: 'tile',
    });
  });

  it('treats member-drive metadata loss on an open state as a generation boundary', () => {
    drive(state());
    expect(sendUiSync).toHaveBeenCalledTimes(1);   // gen-1 opener seed
    sendUiSync.mockClear();

    // Offline close→legacy reopen: a legacy open STATE carries no browse or
    // generation metadata at all. In mixed semantics this follower is not an
    // opener, so the old gen-1 seed must not survive the fallback.
    drive(state({
      memberDrive: false,
      browse: undefined,
      browseGeneration: undefined,
      browseOpener: undefined,
      browseOpenerRole: undefined,
    }));
    localScroll(35);
    hook.flush();
    expect(sendUiSync).not.toHaveBeenCalled();
    // A1 regression invariant: the legacy fallback starts locked until an
    // opener frame arrives. The send assertions above are the observable
    // consequence; this direct check pins the boundary reset itself.
    expect(hook.unlocked).toBe(false);
  });

  it('tags outgoing mirror traffic with the observed generation', () => {
    drive(state());
    expect(sendUiSync).toHaveBeenCalledTimes(1);
    // The opener seed is causally bound to generation 1 — the tag is what
    // lets the server and the receiver drop it if a close→reopen boundary
    // lands while the frame is in flight.
    expect(sentPayloads()[0].browseGeneration).toBe(1);

    sendUiSync.mockClear();
    drive(state({
      browseGeneration: 2,
      browseOpener: 'm-leader', browseOpenerRole: 'leader',
    }));
    hook.applyRemote({view: 'browse', path: null, top: 0, viewH: 100, mode: 'tile'});
    localScroll(42);
    hook.flush();
    expect(sendUiSync).toHaveBeenCalledTimes(1);
    expect(sentPayloads()[0].browseGeneration).toBe(2);
  });

  it('drops inbound frames tagged with a foreign generation before they apply or unlock', () => {
    // Follower, gen-2, not the opener — locked until the opener's first seed.
    drive(state({
      browseGeneration: 2,
      browseOpener: 'm-leader', browseOpenerRole: 'leader',
    }));
    expect(hook.unlocked).toBe(false);

    // A stale gen-1 echo crossing the boundary must neither unlock nor
    // apply — it is never the new opener's first seed. ctlEvent is the real
    // entry point (the controller event bus), not the applyRemote seam.
    // The observable consequence of staying locked: even a real local
    // scroll emits nothing.
    hook.ctlEvent({type: 'ui-sync', payload: {
      view: 'browse', path: null, top: 55, viewH: 100, mode: 'tile',
      browseGeneration: 1,
    }});
    expect(hook.unlocked).toBe(false);
    localScroll(30);
    hook.flush();
    expect(sendUiSync).not.toHaveBeenCalled();

    // The genuine gen-2 opener frame unlocks and applies — proof is that a
    // subsequent local scroll resumes sending.
    hook.ctlEvent({type: 'ui-sync', payload: {
      view: 'browse', path: null, top: 55, viewH: 100, mode: 'tile',
      browseGeneration: 2,
    }});
    expect(hook.unlocked).toBe(true);
    localScroll(42);
    hook.flush();
    expect(sendUiSync).toHaveBeenCalledTimes(1);

    // Untagged frames (legacy/mixed peer) cannot be verified and pass through.
    hook.reset();
    sendUiSync.mockClear();
    drive(state({
      browseGeneration: 2,
      browseOpener: 'm-leader', browseOpenerRole: 'leader',
    }));
    expect(hook.unlocked).toBe(false);
    hook.ctlEvent({type: 'ui-sync', payload: {
      view: 'browse', path: null, top: 20, viewH: 100, mode: 'tile',
    }});
    expect(hook.unlocked).toBe(true);
  });

  it('treats generation emergence while continuously armed as a boundary', () => {
    // Legacy/mixed open: no generation metadata anywhere. The legacy leader
    // unlocks by role and seeds freely.
    drive(state({
      role: 'leader', memberId: 'm-leader',
      memberDrive: false,
      browse: undefined, browseGeneration: undefined,
      browseOpener: undefined, browseOpenerRole: undefined,
    }));
    expect(sendUiSync).toHaveBeenCalledTimes(1);   // legacy opener seed
    sendUiSync.mockClear();

    // The pair upgrades in place (both members re-advertise) — generation
    // metadata appears without any close/open edge, and the serialized
    // opener of the member-drive generation is the FOLLOWER. The leader's
    // legacy unlock must not survive into the new generation.
    drive(state({
      role: 'leader', memberId: 'm-leader',
      browseGeneration: 1,
      browseOpener: 'm-follower', browseOpenerRole: 'follower',
    }));
    localScroll(30);
    hook.flush();
    expect(sendUiSync).not.toHaveBeenCalled();

    // The follower's opener frame unlocks the leader into the new generation.
    hook.ctlEvent({type: 'ui-sync', payload: {
      view: 'browse', path: null, top: 5, viewH: 100, mode: 'tile',
      browseGeneration: 1,
    }});
    localScroll(42);
    hook.flush();
    expect(sendUiSync).toHaveBeenCalledTimes(1);
    expect(sentPayloads()[0].browseGeneration).toBe(1);
  });

  it('explicit closed browse metadata disarms sending across memberDrive loss', () => {
    drive(state());
    sendUiSync.mockClear();

    drive(state({
      memberDrive: false,
      browse: false,
      browseGeneration: 2,
      browseOpener: null,
      browseOpenerRole: null,
    }));
    localScroll(35);
    hook.flush();
    expect(sendUiSync).not.toHaveBeenCalled();
  });
});
