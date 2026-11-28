import { describe, it, expect, afterEach, vi } from 'vitest';

import '../../static/viewer/pairClient.js';

// The controller composes pairClient + pairSync. Member-drive may be
// advertised only when both sides of that composition understand it.
describe('pairController member-drive composition gate', () => {
  const originalCreateClient = globalThis.PairClient.createClient;

  afterEach(() => {
    globalThis.PairClient.createClient = originalCreateClient;
    delete window.PairController;
    delete window.BOOKQ_BROWSE_SYNC_API;
    delete window.BOOKQ_SIDEBAR_CLOSE_API;
    window.sessionStorage.removeItem('bookq.readerSync.v1');
  });

  async function advertisedFeatures(marker, opts) {
    vi.resetModules();
    delete window.PairController;
    if (marker === undefined) delete window.BOOKQ_BROWSE_SYNC_API;
    else window.BOOKQ_BROWSE_SYNC_API = marker;
    // The close-intent API ships with app.js in production; tests opt out
    // explicitly to pin the composition failing closed without it.
    if (!(opts && opts.closeApi === false)) window.BOOKQ_SIDEBAR_CLOSE_API = 1;
    else delete window.BOOKQ_SIDEBAR_CLOSE_API;
    const client = {
      create: vi.fn(() => Promise.resolve({})),
      resume: vi.fn(() => Promise.resolve({})),
      subscribe() { return function () {}; },
    };
    const createClient = vi.fn(() => client);
    globalThis.PairClient.createClient = createClient;
    await import('../../static/viewer/pairController.js');
    await window.PairController.create();
    expect(createClient).toHaveBeenCalledTimes(1);
    return createClient.mock.calls[0][0].features;
  }

  it('does not advertise member-drive with a legacy/unversioned pairSync', async () => {
    expect(await advertisedFeatures(undefined)).toEqual([]);
    expect(await advertisedFeatures(1)).toEqual([]);
  });

  it('advertises member-drive when pairSync exposes the generation-aware API', async () => {
    expect(await advertisedFeatures(2)).toEqual(['member-drive-v1']);
  });

  it('does not advertise member-drive when the close-intent API is absent', async () => {
    // Partial-SWR hazard: legacy app.js has no close-intent marker, so an
    // explicit user close could never emit endBrowse — advertising
    // member-drive would wedge shared browse open server-side.
    expect(await advertisedFeatures(2, {closeApi: false})).toEqual([]);
  });

  it('boot credential resume evaluates the composition after later defer scripts', async () => {
    window.sessionStorage.setItem('bookq.readerSync.v1', JSON.stringify({
      sessionId: 's-boot', memberId: 'm-boot', role: 'leader',
      resumeToken: 'rt-boot',
    }));
    vi.resetModules();
    delete window.PairController;
    delete window.BOOKQ_BROWSE_SYNC_API;
    delete window.BOOKQ_SIDEBAR_CLOSE_API;
    const client = {
      resume: vi.fn(() => Promise.resolve({})),
      subscribe() { return function () {}; },
    };
    const createClient = vi.fn(() => client);
    globalThis.PairClient.createClient = createClient;

    await import('../../static/viewer/pairController.js');
    // pairSync.js is a later defer sibling in index.html. Its marker must be
    // visible before the controller's boot timer constructs the client.
    window.BOOKQ_BROWSE_SYNC_API = 2;
    window.BOOKQ_SIDEBAR_CLOSE_API = 1;
    await new Promise((resolve) => setTimeout(resolve, 10));

    expect(createClient).toHaveBeenCalledTimes(1);
    expect(createClient.mock.calls[0][0].features).toEqual(['member-drive-v1']);
    expect(client.resume).toHaveBeenCalledTimes(1);
  });
});
