import { describe, it, expect, beforeAll } from 'vitest';

// pairSync.js is a plain IIFE with a test-only seam. It is not part of the
// shared setup because its singleton subscriptions depend on a PairController
// composition; install the minimal DOM/controller shell before importing it.
describe('pairSync browse generations', () => {
  let hook;

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

  beforeAll(async () => {
    document.body.innerHTML =
      '<div id="sidebar" class="open">' +
      '  <div id="sidebar-scroll"><div id="tile-grid"></div></div>' +
      '</div>';
    window.__BOOKQ_TEST_MODE__ = 'true';
    window.PairController = {
      state: null,
      subscribe() { return function () {}; },
      onEvent() { return function () {}; },
      sendUiSync() { return true; },
    };
    await import('../../static/browse/pairSync.js');
    hook = window.__pairBrowseSync;
    expect(hook).toBeTruthy();
  });

  it('a generation change reseeds opener/unlock state even without a disarm edge', () => {
    hook.onState(state());
    expect(hook.armed).toBe(true);
    expect(hook.unlocked).toBe(true);   // generation-1 opener may seed

    // The close→reopen happened while this client was offline, so both the
    // old and new observable states are open. Only the generation boundary
    // may reset the old seed; the follower is not the new opener and must
    // wait for the opener's first remote frame before sending again.
    hook.onState(state({
      browseGeneration: 2,
      browseOpener: 'm-leader', browseOpenerRole: 'leader',
    }));
    expect(hook.armed).toBe(true);
    expect(hook.unlocked).toBe(false);
  });

  it('explicit closed browse metadata is authoritative across memberDrive loss', () => {
    hook.onState(state({
      memberDrive: false,
      browse: false,
      browseGeneration: 2,
      browseOpener: null,
      browseOpenerRole: null,
    }));
    expect(hook.armed).toBe(false);
  });
});
