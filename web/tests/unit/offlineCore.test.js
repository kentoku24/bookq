import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import encodePathVectors from '../../../api/vectors/encode-path.json';

// offline-core.js — the single source for URL builders, cache naming,
// size formatting, dedup sums, tree predicates and progress keys.
// Loaded by setup.js onto globalThis.OfflineCore (also window.OfflineCore).
const OC = globalThis.OfflineCore;

describe('encodePath / imageUrl conformance vectors', () => {
  it('matches api/vectors/encode-path.json', () => {
    expect(encodePathVectors.languages).toContain('js');
    for (const c of encodePathVectors.cases) {
      expect(OC.encodePath(c.path), c.name).toBe(c.expect);
      if (c.folder !== undefined) {
        expect(OC.imageUrl(c.folder, c.file), c.name).toBe(c.expectImageUrl);
      }
    }
  });
});

describe('URL builders', () => {
  it('imageUrl keeps slashes as separators and encodes segments', () => {
    expect(OC.imageUrl('a b', 'c+d.png')).toBe('/image/a%20b/c%2Bd.png');
    expect(OC.imageUrl('作品/第1巻', '第1話.png')).toBe(
      '/image/' + encodeURIComponent('作品') + '/' + encodeURIComponent('第1巻') + '/' + encodeURIComponent('第1話.png'));
  });

  it('imageUrl root folder emits no double slash', () => {
    expect(OC.imageUrl('', 'root.png')).toBe('/image/root.png');
    expect(OC.imageUrl(null, 'root.png')).toBe('/image/root.png');
    expect(OC.imageUrl('f', 'n.png')).toBe('/image/f/n.png');
  });

  it('thumbUrl targets /thumb/ and clamps width to 200|400|800', () => {
    expect(OC.thumbUrl('f', 't.png', 200)).toBe('/thumb/f/t.png?w=200');
    expect(OC.thumbUrl('f', 't.png', 800)).toBe('/thumb/f/t.png?w=800');
    expect(OC.thumbUrl('f', 't.png', 400)).toBe('/thumb/f/t.png?w=400');
    expect(OC.thumbUrl('f', 't.png', 999)).toBe('/thumb/f/t.png?w=400');
    expect(OC.thumbUrl('f', 't.png', 0)).toBe('/thumb/f/t.png?w=400');
    expect(OC.thumbUrl('f', 't.png')).toBe('/thumb/f/t.png?w=400');
    expect(OC.thumbUrl('', 't.png', 200)).toBe('/thumb/t.png?w=200');
    expect(OC.thumbUrl('a b', 't.png', 200)).toBe('/thumb/a%20b/t.png?w=200');
  });

  it('viewUrl targets /view/ with the same encoding', () => {
    expect(OC.viewUrl('f', 'n.png')).toBe('/view/f/n.png');
    expect(OC.viewUrl('', 'n.png')).toBe('/view/n.png');
    expect(OC.viewUrl('a/b', 'x y.png')).toBe('/view/a/b/x%20y.png');
  });

  it('readerUrl follows readerUseView flag', () => {
    const prev = OC.readerUseView;
    try {
      OC.readerUseView = false;
      expect(OC.readerUrl('f', 'n.png')).toBe('/image/f/n.png');
      OC.readerUseView = true;
      expect(OC.readerUrl('f', 'n.png')).toBe('/view/f/n.png');
    } finally {
      OC.readerUseView = prev;
    }
  });
});

describe('coverWidth', () => {
  afterEach(() => { window.devicePixelRatio = 1; });

  it('dpr>=2 → 400, else 200', () => {
    window.devicePixelRatio = 3;
    expect(OC.coverWidth()).toBe(400);
    window.devicePixelRatio = 2;
    expect(OC.coverWidth()).toBe(400);
    window.devicePixelRatio = 1.5;
    expect(OC.coverWidth()).toBe(200);
    window.devicePixelRatio = 1;
    expect(OC.coverWidth()).toBe(200);
  });
});

describe('cacheNameFor', () => {
  it('encodes the whole folder as one segment', () => {
    expect(OC.cacheNameFor('a/b')).toBe('bookq-work-a%2Fb-v1');
    expect(OC.cacheNameFor('')).toBe('bookq-work--v1');
    expect(OC.cacheNameFor('x/y')).toContain('%2F');
    expect(decodeURIComponent(OC.cacheNameFor('a/b').slice(11, -3))).toBe('a/b');
    expect(OC.cacheNameFor('夏 の 本')).toBe('bookq-work-' + encodeURIComponent('夏 の 本') + '-v1');
  });
});

describe('escapeHtml', () => {
  it('escapes all five chars, & first', () => {
    expect(OC.escapeHtml('<a href="x">&\'')).toBe('&lt;a href=&quot;x&quot;&gt;&amp;&#39;');
    expect(OC.escapeHtml('&lt;')).toBe('&amp;lt;');   // & escaped before others
    expect(OC.escapeHtml(42)).toBe('42');
    expect(OC.escapeHtml(null)).toBe('null');
  });
});

describe('folder predicates', () => {
  it('folderHasOnlyImages requires ≥1 child, all images', () => {
    expect(OC.folderHasOnlyImages({ children: [{ type: 'image' }] })).toBe(true);
    expect(OC.folderHasOnlyImages({ children: [{ type: 'image' }, { type: 'folder' }] })).toBe(false);
    expect(OC.folderHasOnlyImages({ children: [] })).toBe(false);
    expect(OC.folderHasOnlyImages({})).toBe(false);
    expect(OC.folderHasOnlyImages(null)).toBe(false);
  });

  it('folderHasImages needs ≥1 direct image child', () => {
    expect(OC.folderHasImages({ children: [{ type: 'image' }, { type: 'folder' }] })).toBe(true);
    expect(OC.folderHasImages({ children: [{ type: 'folder' }] })).toBe(false);
    expect(OC.folderHasImages({ children: [] })).toBe(false);
    expect(OC.folderHasImages(null)).toBe(false);
  });
});

describe('fmt', () => {
  it('MB below 1e9, GB at/above — 1 decimal', () => {
    expect(OC.fmt(50_000_000)).toBe('50.0 MB');
    expect(OC.fmt(1_500_000_000)).toBe('1.5 GB');
    expect(OC.fmt(1_000_000_000)).toBe('1.0 GB');
    expect(OC.fmt(999_999_999)).toBe('1000.0 MB');
    expect(OC.fmt(1_000_000_000 - 1)).toBe('1000.0 MB');
    expect(OC.fmt(0)).toBe('0.0 MB');
    expect(OC.fmt(null)).toBe('0.0 MB');
    expect(OC.fmt(undefined)).toBe('0.0 MB');
    expect(OC.fmt('abc')).toBe('0.0 MB');
    expect(OC.fmt(-5)).toBe('-0.0 MB');
  });
});

describe('dedupedBytes / rawBytes', () => {
  it('dedupedBytes skips records that have a descendant record', () => {
    const records = [
      { folder: 'a', bytes: 100 },
      { folder: 'a/b', bytes: 60 },
      { folder: 'c', bytes: 30 },
    ];
    expect(OC.rawBytes(records)).toBe(190);
    expect(OC.dedupedBytes(records)).toBe(90);   // 60+30 — 'a' is a parent of 'a/b'
    expect(OC.dedupedBytes([{ folder: 'x', bytes: 10 }, { folder: 'y', bytes: 20 }])).toBe(30);
    expect(OC.dedupedBytes([])).toBe(0);
    expect(OC.rawBytes([])).toBe(0);
  });

  it('treats prefix-without-slash as NOT a descendant', () => {
    // 'a/b' must not be considered a child of 'a' — needs 'a/' prefix.
    expect(OC.dedupedBytes([{ folder: 'a', bytes: 1 }, { folder: 'ab', bytes: 2 }])).toBe(3);
    expect(OC.dedupedBytes([{ folder: 'a', bytes: 1 }, { folder: 'a/', bytes: 2 }])).toBe(2);
  });

  it('missing bytes count as 0', () => {
    expect(OC.rawBytes([{ folder: 'a' }, { folder: 'b', bytes: 5 }])).toBe(5);
  });
});

describe('OfflineThumb.collect / urls / collectThumbUrls', () => {
  const dpr = window.devicePixelRatio;
  beforeEach(() => { window.devicePixelRatio = 2; });   // coverWidth → 400
  afterEach(() => { window.devicePixelRatio = dpr; });

  const nodes = [
    { _fullPath: 'w1', name: 'w1', type: 'folder', thumb: 'cover.jpg', children: [] },
    {
      _fullPath: 'w2', name: 'w2', type: 'folder',
      thumbs: ['t1.jpg', 't2.jpg', 't3.jpg'],
      children: [{ _fullPath: 'w2/sub', name: 'sub', type: 'folder', thumb: 's.jpg', children: [] }],
    },
    { _fullPath: 'w3', name: 'w3', type: 'folder', children: [] },   // no thumbs
  ];

  it('collects single covers at coverWidth plus multi thumbs at w=200', () => {
    const got = OC.OfflineThumb.collect(nodes);
    const urls = got.map((e) => e.url);
    expect(urls).toContain('/thumb/w1/cover.jpg?w=400');
    expect(urls).toContain('/thumb/w2/t1.jpg?w=200');
    expect(urls).toContain('/thumb/w2/t2.jpg?w=200');
    expect(urls).toContain('/thumb/w2/t3.jpg?w=200');
    expect(urls).toContain('/thumb/w2/sub/s.jpg?w=400');
    expect(urls).toHaveLength(5);
    // entries carry folder+thumb for cache bookkeeping
    expect(got[0]).toMatchObject({ folder: 'w1', thumb: 'cover.jpg' });
  });

  it('singleOnly emits ONLY the single cover per node', () => {
    const got = OC.OfflineThumb.collect(nodes, { singleOnly: true });
    const urls = got.map((e) => e.url);
    expect(urls).toEqual(['/thumb/w1/cover.jpg?w=400', '/thumb/w2/sub/s.jpg?w=400']);
    // w2 has no .thumb (only .thumbs) → nothing for it
    expect(OC.OfflineThumb.urls(nodes, { singleOnly: true })).toEqual(urls);
    expect(OC.collectThumbUrls(nodes, { singleOnly: true })).toEqual(got);
  });

  it('caps multi thumbs at MAX_PER_NODE=9 and dedups by URL', () => {
    const many = { _fullPath: 'w', thumbs: Array.from({ length: 12 }, (_, i) => `t${i}.jpg`), children: [] };
    const got = OC.OfflineThumb.collect([many]);
    expect(got).toHaveLength(9);
    const dup = { _fullPath: 'w', thumb: 'c.jpg', children: [] };
    const got2 = OC.OfflineThumb.collect([dup, { ...dup }]);
    expect(got2).toHaveLength(1);   // same URL → deduped
  });
});

describe('isDataCacheName', () => {
  it('matches the three data prefixes only', () => {
    expect(OC.isDataCacheName('bookq-work-abc-v1')).toBe(true);
    expect(OC.isDataCacheName('bookq-api-x')).toBe(true);
    expect(OC.isDataCacheName('bookq-images-x')).toBe(true);
    expect(OC.isDataCacheName('bookq-shell-abc')).toBe(false);
    expect(OC.isDataCacheName('other')).toBe(false);
  });
});

describe('progress keys (#375)', () => {
  beforeEach(() => { localStorage.clear(); delete globalThis.__BOOKQ_PROGRESS_USER__; });
  afterEach(() => { delete globalThis.__BOOKQ_PROGRESS_USER__; });

  it('progressKey scopes by injected user, legacy key without', () => {
    expect(OC.progressKey()).toBe('bookq_progress');
    globalThis.__BOOKQ_PROGRESS_USER__ = 'a@b.c';
    expect(OC.progressKey()).toBe('bookq_progress:a@b.c');
  });

  it('progressStorageKeys finds legacy + all scoped keys', () => {
    localStorage.setItem('bookq_progress', '{}');
    localStorage.setItem('bookq_progress:a@b.c', '{}');
    localStorage.setItem('bookq_progress:x@y.z', '{}');
    localStorage.setItem('other', '1');
    const keys = OC.progressStorageKeys().sort();
    expect(keys).toEqual(['bookq_progress', 'bookq_progress:a@b.c', 'bookq_progress:x@y.z']);
  });

  it('migrateProgress moves legacy key into the scoped key once', () => {
    globalThis.__BOOKQ_PROGRESS_USER__ = 'a@b.c';
    localStorage.setItem('bookq_progress', '{"p":1}');
    OC.migrateProgress();
    expect(localStorage.getItem('bookq_progress:a@b.c')).toBe('{"p":1}');
    expect(localStorage.getItem('bookq_progress')).toBeNull();
    // idempotent: second run does not resurrect
    OC.migrateProgress();
    expect(localStorage.getItem('bookq_progress:a@b.c')).toBe('{"p":1}');
    expect(localStorage.getItem('bookq_progress')).toBeNull();
  });

  it('migrateProgress does not overwrite an existing scoped key', () => {
    globalThis.__BOOKQ_PROGRESS_USER__ = 'a@b.c';
    localStorage.setItem('bookq_progress', '{"old":1}');
    localStorage.setItem('bookq_progress:a@b.c', '{"new":2}');
    OC.migrateProgress();
    expect(localStorage.getItem('bookq_progress:a@b.c')).toBe('{"new":2}');
    expect(localStorage.getItem('bookq_progress')).toBeNull();
  });

  it('migrateProgress is a no-op without a user context', () => {
    localStorage.setItem('bookq_progress', '{"p":1}');
    OC.migrateProgress();
    expect(localStorage.getItem('bookq_progress')).toBe('{"p":1}');
  });
});
