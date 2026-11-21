import { describe, it, expect, beforeEach, afterEach } from 'vitest';

// thumbs/resolveCover.js — cover fallback chain shared by browse/search
// renders. thumb → thumbs[0] → null; single-cover uses device coverWidth.
const RC = globalThis.ThumbResolve;

const node = (over) => Object.assign({ _fullPath: 'folder/a', name: 'a', type: 'folder' }, over);

describe('resolveCover', () => {
  it('returns null for missing node or missing _fullPath', () => {
    expect(RC.resolveCover(null)).toBeNull();
    expect(RC.resolveCover(undefined)).toBeNull();
    expect(RC.resolveCover({ thumb: 't.jpg' })).toBeNull();
    expect(RC.resolveCover(node({ _fullPath: '' }))).toBeNull();
  });

  it('prefers node.thumb over node.thumbs[0]', () => {
    const n = node({ thumb: 'main.jpg', thumbs: ['other.jpg'] });
    expect(RC.resolveCover(n, 200)).toBe('/thumb/folder/a/main.jpg?w=200');
  });

  it('falls back to thumbs[0] when no thumb', () => {
    expect(RC.resolveCover(node({ thumbs: ['first.jpg', 'second.jpg'] }), 200))
      .toBe('/thumb/folder/a/first.jpg?w=200');
    expect(RC.resolveCover(node({ thumbs: [] }), 200)).toBeNull();
    expect(RC.resolveCover(node({ thumbs: [null, 'x.jpg'] }), 200)).toBeNull();
  });

  it('clamps width: only 200/800 pass through, else 400', () => {
    const n = node({ thumb: 't.jpg' });
    expect(RC.resolveCover(n, 200)).toContain('?w=200');
    expect(RC.resolveCover(n, 800)).toContain('?w=800');
    expect(RC.resolveCover(n, 400)).toContain('?w=400');
    expect(RC.resolveCover(n, 320)).toContain('?w=400');
    expect(RC.resolveCover(n, -1)).toContain('?w=400');
    expect(RC.resolveCover(n)).toContain('?w=400');   // default
  });

  it('encodes each path segment', () => {
    const n = node({ _fullPath: 'a b/第1巻', thumb: 'cover 1.jpg' });
    expect(RC.resolveCover(n, 200)).toBe(
      '/thumb/' + encodeURIComponent('a b') + '/' + encodeURIComponent('第1巻') + '/' + encodeURIComponent('cover 1.jpg') + '?w=200');
  });
});

describe('resolveCoverSingle', () => {
  it('uses ONLY node.thumb at the device cover width', () => {
    const dpr = window.devicePixelRatio;
    try {
      window.devicePixelRatio = 2;   // coverWidth → 400
      expect(RC.resolveCoverSingle(node({ thumb: 't.jpg' }))).toBe('/thumb/folder/a/t.jpg?w=400');
      window.devicePixelRatio = 1;   // coverWidth → 200
      expect(RC.resolveCoverSingle(node({ thumb: 't.jpg' }))).toBe('/thumb/folder/a/t.jpg?w=200');
      // thumbs[] alone does NOT qualify — single cover is thumb-only
      expect(RC.resolveCoverSingle(node({ thumbs: ['t.jpg'] }))).toBeNull();
    } finally {
      window.devicePixelRatio = dpr;
    }
  });

  it('returns null without thumb or folder', () => {
    expect(RC.resolveCoverSingle(null)).toBeNull();
    expect(RC.resolveCoverSingle(node({}))).toBeNull();
    expect(RC.resolveCoverSingle(node({ _fullPath: '', thumb: 't.jpg' }))).toBeNull();
  });
});

describe('resolveCoverHtml', () => {
  it('emits an img tag with escaped url and caller class', () => {
    const html = RC.resolveCoverHtml(node({ thumb: 't.jpg' }), 'card-img');
    expect(html).toContain('<img class="card-img"');
    expect(html).toContain('src="/thumb/folder/a/t.jpg?w=400"');
    expect(html).toContain('loading="lazy"');
    expect(html).toContain('decoding="async"');
  });

  it('defaults cssClass to tile-img', () => {
    expect(RC.resolveCoverHtml(node({ thumb: 't.jpg' }))).toContain('class="tile-img"');
  });

  it('placeholder variants per cssClass', () => {
    const noThumb = node({});
    expect(RC.resolveCoverHtml(noThumb, 'thumb')).toBe('');
    expect(RC.resolveCoverHtml(noThumb, 'card-img')).toContain('aspect-ratio:16/9');
    expect(RC.resolveCoverHtml(noThumb, 'tile-img')).toContain('tile-placeholder');
    expect(RC.resolveCoverHtml(noThumb)).toContain('tile-placeholder');
    expect(RC.resolveCoverHtml(noThumb, 'list-thumb')).toContain('tile-placeholder');
  });
});

describe('OfflineCore-less fallback (_mb base)', () => {
  let savedOC;
  beforeEach(() => {
    savedOC = globalThis.OfflineCore;
    delete globalThis.OfflineCore;
    delete window.OfflineCore;
  });
  afterEach(() => {
    globalThis.OfflineCore = savedOC;
    window.OfflineCore = savedOC;
  });

  it('builds /thumb/ URLs without OfflineCore (BookqApi.base="")', () => {
    expect(RC.resolveCover(node({ thumb: 't.jpg' }), 200)).toBe('/thumb/folder/a/t.jpg?w=200');
    expect(RC.resolveCoverSingle(node({ thumb: 't.jpg' }))).toMatch(/^\/thumb\/folder\/a\/t\.jpg\?w=\d+$/);
  });
});
