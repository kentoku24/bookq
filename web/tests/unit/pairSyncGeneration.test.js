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
