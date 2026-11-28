import { describe, it, expect, beforeAll, afterEach, vi } from 'vitest';

// Pure decision logic inside pairController, exercised through the
// test-mode seam. These cover the mixed-shell legacy fallback and the
// shared-browse echo-hold predicate without standing up a DOM pair.
describe('pairController drive authority and echo-hold decisions', () => {
  let seam;

  beforeAll(async () => {
    window.__BOOKQ_TEST_MODE__ = 'true';
    vi.resetModules();
    const client = {
      create: vi.fn(() => Promise.resolve({})),
      resume: vi.fn(() => Promise.resolve({})),
      subscribe() { return function () {}; },
    };
    globalThis.PairClient = {createClient: vi.fn(() => client)};
    await import('../../static/viewer/pairController.js');
    seam = window.__pairCtlTest;
    expect(seam).toBeTruthy();
  });

  afterEach(() => {
    window.sessionStorage.removeItem('bookq.readerSync.v1');
  });

  it('driveAllowed honors an explicit boolean canDrive in both directions', () => {
    expect(seam.driveAllowed({canDrive: true, role: 'follower'})).toBe(true);
    expect(seam.driveAllowed({canDrive: false, role: 'leader'})).toBe(false);
  });

  it('driveAllowed falls back to the legacy role rule when canDrive is absent', () => {
    // A pre-trust pairClient (stale SWR shell, or a rolled-back server that
    // never emits trust) exposes no canDrive at all. Dead-ending every
    // drive action there would strand the whole pair behind a missing
    // field, so the legacy surface falls back to role-based authority.
    expect(seam.driveAllowed({role: 'leader'})).toBe(true);
    expect(seam.driveAllowed({role: 'follower'})).toBe(false);
    // An explicit false is the new client genuinely denying drive — it
    // must never fall through to the role rule.
    expect(seam.driveAllowed({canDrive: false, role: 'leader'})).toBe(false);
    expect(seam.driveAllowed(null)).toBe(false);
    expect(seam.driveAllowed(undefined)).toBe(false);
  });

  it('echo-hold follows the snap browse bit once the open roundtrip settles', () => {
    // member-drive, settled open: bit true holds even without an issuance.
    expect(seam.mayHoldSharedBrowseEcho('follower', true, 'member', true, false)).toBe(true);
    // settled closed: nothing holds.
    expect(seam.mayHoldSharedBrowseEcho('follower', true, 'member', false, false)).toBe(false);
  });

  it('echo-hold covers the open roundtrip window through selIssued', () => {
    // Between emitting SET_ACTIVITY selecting and the browse:true STATE
    // landing, every snap still carries the bit false. The issuance is the
    // only fact proving this member is opening the shared list — without
    // it a no-op echo in that window slams the sidebar shut.
    expect(seam.mayHoldSharedBrowseEcho('follower', true, 'member', false, true)).toBe(true);
  });

  it('echo-hold keeps guests and legacy followers out in every window', () => {
    expect(seam.mayHoldSharedBrowseEcho('leader', true, 'guest', true, true)).toBe(false);
    expect(seam.mayHoldSharedBrowseEcho('follower', false, 'member', true, true)).toBe(false);
    // legacy leader holds regardless of bits — the role itself is the authority.
    expect(seam.mayHoldSharedBrowseEcho('leader', false, 'member', false, false)).toBe(true);
  });
});
