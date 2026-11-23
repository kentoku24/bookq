import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

// api/v1.js — typed fetch client. opts.fetch injection is the designed test
// seam; Response comes from undici (Node 22 global).
const API = globalThis.BookqApi;

function jsonResponse(body, status = 200, headers = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

function captureFetch(responder) {
  const calls = [];
  const fn = vi.fn(async (url, init) => {
    calls.push({ url: String(url), init });
    return typeof responder === 'function' ? responder(url, init, calls.length) : responder;
  });
  return { fn, calls };
}

beforeEach(() => {
  for (const k of ['Connectivity', 'activeFilters',
    'facets', 'facetLoadGen', 'searchTimer', 'refreshOfflineMap', 'renderView',
    'FolderVisibility', 'tree', 'offlineMap', 'offlineRecordMap', 'getLeafPathsFor',
    'progressBarHtml', 'BrowseRender', 'StoreAdapter', 'offlineControlsHtml',
    'offlineControlsHtmlCtx', 'buildNodeMap', 'ReadingProgress', 'localStorage']) {
    if (k !== 'localStorage') delete window[k];
  }
  document.body.innerHTML =
    '<div id="tile-grid"></div><input id="search-input" value=""><div id="filter-chips" class="hidden"></div>' +
    '<div id="facet-artists"><div class="facet-chips"></div></div><div id="facet-tags"><div class="facet-chips"></div></div>' +
    '<span id="folder-count"></span><button id="filter-btn"></button>';
});

describe('ApiError / error helpers', () => {
  it('ApiError carries status, code, detail', () => {
    const e = new API.ApiError(409, 'conflict', 'owner mismatch');
    expect(e).toBeInstanceOf(Error);
    expect(e.name).toBe('ApiError');
    expect(e.status).toBe(409);
    expect(e.code).toBe('conflict');
    expect(e.detail).toBe('owner mismatch');
    expect(e.message).toContain('409');
    expect(e.message).toContain('conflict');
  });

  it('errorCodeOf/detailOf tolerate non-object bodies', () => {
    expect(API.errorCodeOf(null)).toBeNull();
    expect(API.errorCodeOf('x')).toBeNull();
    expect(API.errorCodeOf({})).toBeNull();
    expect(API.errorCodeOf({ code: 42 })).toBeNull();
    expect(API.errorCodeOf({ code: 'e1' })).toBe('e1');
  });
});

describe('URL builders', () => {
  it('treeUrl encodes path/depth/since params', () => {
    expect(API.treeUrl()).toBe('/api/v1/tree');
    expect(API.treeUrl({ path: 'a b', depth: 2, since: 123 })).toBe('/api/v1/tree?path=a+b&depth=2&since=123');
    expect(API.treeUrl({ path: '' })).toBe('/api/v1/tree');
  });

  it('imagesUrl / putPositionUrl encode folder', () => {
    expect(API.imagesUrl('a b/c')).toBe('/api/v1/images?folder=a%20b%2Fc');
    expect(API.putPositionUrl('x/y')).toBe('/api/v1/reading-positions?folder=x%2Fy');
  });
});

describe('body normalizers', () => {
  it('nodesOf: v1 envelope, legacy array, v0 tree dict, else null', () => {
    expect(API.nodesOf([{ name: 'n' }])).toEqual([{ name: 'n' }]);
    expect(API.nodesOf({ nodes: [{ name: 'n' }] })).toEqual([{ name: 'n' }]);
    expect(API.nodesOf({ tree: [{ name: 'n' }] })).toEqual([{ name: 'n' }]);
    expect(API.nodesOf({ nodes: 'x' })).toBeNull();
    expect(API.nodesOf(null)).toBeNull();
    expect(API.nodesOf(42)).toBeNull();
  });

  it('imagesOf: envelope + legacy array tolerance', () => {
    expect(API.imagesOf({ images: ['a.png'] })).toEqual(['a.png']);
    expect(API.imagesOf(['a.png'])).toEqual(['a.png']);
    expect(API.imagesOf({ images: 'x' })).toBeNull();
    expect(API.imagesOf(null)).toBeNull();
  });

  it('latestPositionOf: positions[0], v0 position, else null', () => {
    const p = { folder: 'f', page: 1 };
    expect(API.latestPositionOf({ positions: [p, { page: 2 }] })).toEqual(p);
    expect(API.latestPositionOf({ positions: [] })).toBeNull();
    expect(API.latestPositionOf({ position: p })).toEqual(p);
    expect(API.latestPositionOf({})).toBeNull();
    expect(API.latestPositionOf(null)).toBeNull();
  });
});

describe('fetchJson', () => {
  it('defaults credentials to same-origin and parses JSON', async () => {
    const { fn, calls } = captureFetch(jsonResponse({ ok: 1 }));
    const out = await API.fetchJson('/x', {}, fn);
    expect(out.status).toBe(200);
    expect(out.body).toEqual({ ok: 1 });
    expect(calls[0].init.credentials).toBe('same-origin');
  });

  it('keeps caller credentials and yields body:null on non-JSON', async () => {
    const { fn, calls } = captureFetch(new Response('not json', { status: 200 }));
    const out = await API.fetchJson('/x', { credentials: 'include' }, fn);
    expect(out.body).toBeNull();
    expect(calls[0].init.credentials).toBe('include');
  });
});

describe('fetchWithBuildingRetry (202 centralization)', () => {
  it('returns non-202 immediately', async () => {
    const { fn, calls } = captureFetch(jsonResponse({ done: true }));
    const res = await API.fetchWithBuildingRetry('/api/v1/tree', fn);
    expect(res.status).toBe(200);
    expect(calls).toHaveLength(1);
  });

  it('retries 202 honoring Retry-After: 0', async () => {
    let n = 0;
    const { fn, calls } = captureFetch(() =>
      ++n < 3 ? jsonResponse({}, 202, { 'Retry-After': '0' }) : jsonResponse({ ok: 1 }));
    const res = await API.fetchWithBuildingRetry('/api/v1/tree', fn, { maxAttempts: 5 });
    expect(res.status).toBe(200);
    expect(calls).toHaveLength(3);
  });

  it('gives up after maxAttempts and returns last 202', async () => {
    const { fn, calls } = captureFetch(() => jsonResponse({}, 202, { 'Retry-After': '0' }));
    const res = await API.fetchWithBuildingRetry('/api/v1/tree', fn, { maxAttempts: 3 });
    expect(res.status).toBe(202);
    expect(calls).toHaveLength(3);
  });

  it('stops early when isCurrent() flips false', async () => {
    let current = true;
    const { fn, calls } = captureFetch(() => jsonResponse({}, 202, { 'Retry-After': '0' }));
    const p = API.fetchWithBuildingRetry('/api/v1/tree', fn, {
      maxAttempts: 10,
      isCurrent: () => { if (calls.length === 1) current = false; return current; },
    });
    const res = await p;
    expect(res.status).toBe(202);
    expect(calls).toHaveLength(1);
  });
});

describe('getTree', () => {
  it('returns typed envelope {kind,nodes,generation}', async () => {
    const { fn, calls } = captureFetch(jsonResponse({ kind: 'tree', nodes: [{ name: 'n' }], generation: 7 }));
    const out = await API.getTree({ path: 'a', depth: 1 }, { fetch: fn });
    expect(out).toEqual({ kind: 'tree', nodes: [{ name: 'n' }], generation: 7 });
    expect(calls[0].url).toBe('/api/v1/tree?path=a&depth=1');
  });

  it('tolerates legacy bare-array body as generation 0', async () => {
    const { fn } = captureFetch(jsonResponse([{ name: 'old' }]));
    const out = await API.getTree(undefined, { fetch: fn });
    expect(out).toEqual({ kind: 'tree', nodes: [{ name: 'old' }], generation: 0 });
  });

  it('throws ApiError with spec code on non-ok and malformed 200', async () => {
    const { fn: f1 } = captureFetch(jsonResponse({ code: 'unauthorized', detail: 'no' }, 401));
    await expect(API.getTree(undefined, { fetch: f1 })).rejects.toMatchObject({ status: 401, code: 'unauthorized' });
    const { fn: f2 } = captureFetch(jsonResponse({ unexpected: true }));
    await expect(API.getTree(undefined, { fetch: f2 })).rejects.toBeInstanceOf(API.ApiError);
  });
});

describe('getImages', () => {
  it('returns images array from envelope or legacy', async () => {
    const { fn: f1, calls } = captureFetch(jsonResponse({ folder: 'f', images: ['a.png', 'b.png'] }));
    expect(await API.getImages('f', { fetch: f1 })).toEqual(['a.png', 'b.png']);
    expect(calls[0].url).toBe('/api/v1/images?folder=f');
    const { fn: f2 } = captureFetch(jsonResponse(['legacy.png']));
    expect(await API.getImages('f', { fetch: f2 })).toEqual(['legacy.png']);
  });

  it('throws on malformed body', async () => {
    const { fn } = captureFetch(jsonResponse({ nope: 1 }));
    await expect(API.getImages('f', { fetch: fn })).rejects.toBeInstanceOf(API.ApiError);
  });
});

describe('search / getFacets', () => {
  it('search passes query params and normalizes response', async () => {
    const { fn, calls } = captureFetch(jsonResponse({ query: 'q', total: 2, results: [{ folder_path: 'a' }] }));
    const out = await API.search({ q: 'hello world', artist: 'a1,a2', sort: 'title' }, { fetch: fn });
    expect(calls[0].url).toContain('/api/v1/search?');
    expect(calls[0].url).toContain('q=hello+world');
    expect(calls[0].url).toContain('artist=a1%2Ca2');
    expect(out.total).toBe(2);
    expect(out.results).toHaveLength(1);
  });

  it('search throws when results missing; defaults query/total', async () => {
    const { fn: f1 } = captureFetch(jsonResponse({ results: [{ x: 1 }] }));
    const out = await API.search({ q: 'x' }, { fetch: f1 });
    expect(out.query).toBe('');
    expect(out.total).toBe(1);
    const { fn: f2 } = captureFetch(jsonResponse({ total: 3 }));
    await expect(API.search({ q: 'x' }, { fetch: f2 })).rejects.toBeInstanceOf(API.ApiError);
  });

  it('getFacets requires artists+tags arrays', async () => {
    const { fn } = captureFetch(jsonResponse({ artists: ['a'], tags: ['t'] }));
    expect(await API.getFacets({ path: 'p' }, { fetch: fn })).toEqual({ artists: ['a'], tags: ['t'] });
    const { fn: f2 } = captureFetch(jsonResponse({ artists: ['a'] }));
    await expect(API.getFacets({}, { fetch: f2 })).rejects.toBeInstanceOf(API.ApiError);
  });
});

describe('positions', () => {
  it('getPositions returns positions array, tolerates v0 {position}', async () => {
    const p = { folder: 'f', page: 3, ts: 1 };
    const { fn: f1 } = captureFetch(jsonResponse({ positions: [p] }));
    expect(await API.getPositions({}, { fetch: f1 })).toEqual([p]);
    const { fn: f2 } = captureFetch(jsonResponse({ position: p }));
    expect(await API.getPositions({}, { fetch: f2 })).toEqual([p]);
  });

  it('putPosition PUTs {page,total,ts} to folder URL; ok|stale succeed', async () => {
    const { fn, calls } = captureFetch(jsonResponse({ status: 'ok' }));
    const out = await API.putPosition({ folder: 'f g', page: 2, total: 10, ts: 99, extra: 'drop' }, { fetch: fn });
    expect(out).toEqual({ status: 'ok' });
    expect(calls[0].url).toBe('/api/v1/reading-positions?folder=f%20g');
    expect(calls[0].init.method).toBe('PUT');
    expect(calls[0].init.keepalive).toBe(true);
    expect(JSON.parse(calls[0].init.body)).toEqual({ page: 2, total: 10, ts: 99 });
    const { fn: f2 } = captureFetch(jsonResponse({ status: 'stale' }));
    expect(await API.putPosition({ folder: 'f', page: 1, total: 2, ts: 1 }, { fetch: f2 })).toEqual({ status: 'stale' });
    const { fn: f3 } = captureFetch(jsonResponse({ status: 'weird' }));
    await expect(API.putPosition({ folder: 'f', page: 1, total: 2, ts: 1 }, { fetch: f3 })).rejects.toBeInstanceOf(API.ApiError);
  });

  it('deletePositions DELETEs and returns ok boolean', async () => {
    const { fn, calls } = captureFetch(jsonResponse({}, 200));
    expect(await API.deletePositions({ folder: 'f' }, { fetch: fn })).toBe(true);
    expect(calls[0].init.method).toBe('DELETE');
    expect(calls[0].url).toBe('/api/v1/reading-positions?folder=f');
    const { fn: f2 } = captureFetch(new Response('', { status: 500 }));
    expect(await API.deletePositions({}, { fetch: f2 })).toBe(false);
  });
});

describe('visibility', () => {
  const snap = { owner: 'a@b.c', revision: 3, hiddenRoots: ['secret'] };

  it('getVisibility validates snapshot shape', async () => {
    const { fn } = captureFetch(jsonResponse(snap));
    expect(await API.getVisibility({ fetch: fn })).toEqual(snap);
    const { fn: f2 } = captureFetch(jsonResponse({ owner: 'a', revision: 'x', hiddenRoots: [] }));
    await expect(API.getVisibility({ fetch: f2 })).rejects.toBeInstanceOf(API.ApiError);
    const { fn: f3 } = captureFetch(jsonResponse({ owner: 'a', revision: 1, hiddenRoots: 'x' }));
    await expect(API.getVisibility({ fetch: f3 })).rejects.toBeInstanceOf(API.ApiError);
  });

  it('putVisibility sends owner header + {root,hidden}; 409 throws ApiError', async () => {
    const { fn, calls } = captureFetch(jsonResponse(snap));
    const out = await API.putVisibility('secret', true, 'a@b.c', { fetch: fn });
    expect(out).toEqual(snap);
    expect(calls[0].init.method).toBe('PUT');
    expect(calls[0].init.headers['X-Bookq-Visibility-Owner']).toBe('a@b.c');
    expect(JSON.parse(calls[0].init.body)).toEqual({ root: 'secret', hidden: true });
    const { fn: f2 } = captureFetch(jsonResponse({ code: 'conflict' }, 409));
    await expect(API.putVisibility('r', false, 'o', { fetch: f2 }))
      .rejects.toMatchObject({ status: 409 });
  });
});
