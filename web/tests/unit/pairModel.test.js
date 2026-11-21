import { describe, it, expect } from 'vitest';

// viewer/pairModel.js — pure dual-reader spread projection (#76/S01).
// The browser never recomputes pairing; it only projects the server table.
const PM = globalThis.PairModel;

const ref = (folder, name) => ({ folder, name });
const spread = (id, left, right, chapterFolder) => ({ id, left, right, chapterFolder });

describe('PairModel constants & isPageRef', () => {
  it('exposes protocol version 1', () => {
    expect(PM.PROTOCOL_VERSION).toBe(1);
  });

  it('isPageRef accepts {folder,name} strings only', () => {
    expect(PM.isPageRef(ref('a', 'b.png'))).toBe(true);
    expect(PM.isPageRef(null)).toBe(false);
    expect(PM.isPageRef(undefined)).toBe(false);
    expect(PM.isPageRef('x')).toBe(false);
    expect(PM.isPageRef({ folder: 'a' })).toBe(false);
    expect(PM.isPageRef({ folder: 'a', name: 1 })).toBe(false);
    expect(PM.isPageRef({ folder: 1, name: 'b' })).toBe(false);
  });
});

describe('clampIndex', () => {
  const spreads = [spread('a'), spread('b'), spread('c')];

  it('clamps into [0, len-1]', () => {
    expect(PM.clampIndex(spreads, 0)).toBe(0);
    expect(PM.clampIndex(spreads, 5)).toBe(2);
    expect(PM.clampIndex(spreads, -3)).toBe(0);
    expect(PM.clampIndex(spreads, 1)).toBe(1);
  });

  it('returns -1 for empty spreads', () => {
    expect(PM.clampIndex([], 3)).toBe(-1);
  });

  it('rejects non-integer index and non-array spreads', () => {
    expect(() => PM.clampIndex(spreads, 1.5)).toThrowError(/ERR_MALFORMED/);
    expect(() => PM.clampIndex(spreads, '1')).toThrowError(/ERR_MALFORMED/);
    expect(() => PM.clampIndex(null, 0)).toThrowError(/ERR_MALFORMED/);
    expect(() => PM.clampIndex({}, 0)).toThrowError(/ERR_MALFORMED/);
  });
});

describe('spreadIndexById', () => {
  const spreads = [spread('s0'), null, spread('s2'), spread('s0')];

  it('returns first matching index, -1 when absent', () => {
    expect(PM.spreadIndexById(spreads, 's0')).toBe(0);
    expect(PM.spreadIndexById(spreads, 's2')).toBe(2);
    expect(PM.spreadIndexById(spreads, 'nope')).toBe(-1);
  });

  it('rejects non-string id and non-array spreads', () => {
    expect(() => PM.spreadIndexById(spreads, 1)).toThrowError(/ERR_MALFORMED/);
    expect(() => PM.spreadIndexById(null, 's0')).toThrowError(/ERR_MALFORMED/);
  });
});

describe('spreadIndexForPage', () => {
  const spreads = [
    spread('a', ref('f', 'l.png'), ref('f', 'r.png')),
    spread('b', ref('g', 'l.png'), ref('g', 'r.png')),
    null,
  ];

  it('matches left and right page refs', () => {
    expect(PM.spreadIndexForPage(spreads, ref('f', 'l.png'))).toBe(0);
    expect(PM.spreadIndexForPage(spreads, ref('f', 'r.png'))).toBe(0);
    expect(PM.spreadIndexForPage(spreads, ref('g', 'l.png'))).toBe(1);
    expect(PM.spreadIndexForPage(spreads, ref('g', 'r.png'))).toBe(1);
    expect(PM.spreadIndexForPage(spreads, ref('x', 'y.png'))).toBe(-1);
  });

  it('rejects malformed ref and non-array spreads', () => {
    expect(() => PM.spreadIndexForPage(spreads, 'f/l.png')).toThrowError(/ERR_MALFORMED/);
    expect(() => PM.spreadIndexForPage(spreads, { folder: 'f' })).toThrowError(/ERR_MALFORMED/);
    expect(() => PM.spreadIndexForPage(null, ref('f', 'l.png'))).toThrowError(/ERR_MALFORMED/);
  });
});

describe('turnIndex', () => {
  // ch1 = indices 0..2, ch2 = indices 3..4
  const spreads = [
    spread('a', null, null, 'ch1'),
    spread('b', null, null, 'ch1'),
    spread('c', null, null, 'ch1'),
    spread('d', null, null, 'ch2'),
    spread('e', null, null, 'ch2'),
  ];

  it('returns -1 for empty spreads', () => {
    expect(PM.turnIndex([], 0, 1, true)).toBe(-1);
  });

  it('moves ±1 within the same chapter when crossChapter=false', () => {
    expect(PM.turnIndex(spreads, 0, 1, false)).toBe(1);
    expect(PM.turnIndex(spreads, 1, -1, false)).toBe(0);
    expect(PM.turnIndex(spreads, 0, -1, false)).toBe(0);   // clamped at chapter start
    expect(PM.turnIndex(spreads, 2, 1, false)).toBe(2);   // ch1 end — cannot enter ch2
    expect(PM.turnIndex(spreads, 3, -1, false)).toBe(3);  // ch2 start
    expect(PM.turnIndex(spreads, 4, 1, false)).toBe(4);   // ch2 end
  });

  it('crosses chapter boundaries when crossChapter=true', () => {
    expect(PM.turnIndex(spreads, 2, 1, true)).toBe(3);
    expect(PM.turnIndex(spreads, 3, -1, true)).toBe(2);
    expect(PM.turnIndex(spreads, 0, -1, true)).toBe(0);   // clamped at array start
    expect(PM.turnIndex(spreads, 4, 1, true)).toBe(4);   // clamped at array end
  });

  it('rejects out-of-range index and delta not ±1', () => {
    expect(() => PM.turnIndex(spreads, 5, 1, true)).toThrowError(/ERR_RANGE/);
    expect(() => PM.turnIndex(spreads, -1, 1, true)).toThrowError(/ERR_RANGE/);
    expect(() => PM.turnIndex(spreads, 0, 2, true)).toThrowError(/ERR_RANGE/);
    expect(() => PM.turnIndex(spreads, 0, 0, true)).toThrowError(/ERR_RANGE/);
    expect(() => PM.turnIndex(null, 0, 1, true)).toThrowError(/ERR_MALFORMED/);
  });
});

describe('projectSpread', () => {
  const s = spread('s', ref('f', 'l.png'), ref('f', 'r.png'));

  it('leader renders leaderSide, follower the opposite', () => {
    expect(PM.projectSpread(s, 'leader', 'right')).toEqual({ side: 'right', ref: ref('f', 'r.png') });
    expect(PM.projectSpread(s, 'leader', 'left')).toEqual({ side: 'left', ref: ref('f', 'l.png') });
    expect(PM.projectSpread(s, 'follower', 'right')).toEqual({ side: 'left', ref: ref('f', 'l.png') });
    expect(PM.projectSpread(s, 'follower', 'left')).toEqual({ side: 'right', ref: ref('f', 'r.png') });
  });

  it('blank side projects ref:null', () => {
    const blank = spread('s', ref('f', 'l.png'), null);
    expect(PM.projectSpread(blank, 'leader', 'right')).toEqual({ side: 'right', ref: null });
    expect(PM.projectSpread(blank, 'leader', 'left')).toEqual({ side: 'left', ref: ref('f', 'l.png') });
    const blankL = spread('s', null, ref('f', 'r.png'));
    expect(PM.projectSpread(blankL, 'follower', 'right')).toEqual({ side: 'left', ref: null });
  });

  it('rejects bad role, leaderSide and non-object spread', () => {
    expect(() => PM.projectSpread(s, 'spectator', 'right')).toThrowError(/ERR_MALFORMED/);
    expect(() => PM.projectSpread(s, 'leader', 'middle')).toThrowError(/ERR_RANGE/);
    expect(() => PM.projectSpread(null, 'leader', 'right')).toThrowError(/ERR_MALFORMED/);
    expect(() => PM.projectSpread('x', 'leader', 'right')).toThrowError(/ERR_MALFORMED/);
  });
});
