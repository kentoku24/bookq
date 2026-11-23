import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

// search/facets.js — facet loading + filter-sheet options. Transport goes
// through window.BookqApi with deps.fetch injection; window.* state is the
// app.js seam. (The /api/v1/search result view was retired — the search
// input now sorts the current folder client-side via browse/matchSort.js.)
const SF = globalThis.SearchFacets;

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status, headers: { 'Content-Type': 'application/json' },
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

function deferred() {
  let resolve, reject;
  const p = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { p, resolve, reject };
}

const FACETS = { artists: ['Artist A', 'Artist B'], tags: ['tag1', 'tag2'] };

beforeEach(() => {
  for (const k of ['Connectivity', 'activeFilters',
    'facets', 'facetLoadGen', 'refreshOfflineMap', 'renderView',
    'FolderVisibility', 'tree', 'offlineMap', 'offlineRecordMap', 'getLeafPathsFor',
    'progressBarHtml', 'BrowseRender', 'StoreAdapter', 'offlineControlsHtml',
    'offlineControlsHtmlCtx', 'buildNodeMap', 'ReadingProgress', 'loadImages',
    'closeSidebar', 'currentFolder', 'currentBrowsePath']) {
    delete window[k];
  }
  window.activeFilters = { artist: [], tag: [], sort: 'relevance' };
  window.facetLoadGen = 0;
  document.body.innerHTML =
    '<div id="tile-grid"></div>' +
    '<div id="filter-chips" class="hidden"></div>' +
    '<div id="facet-artists"><div class="facet-chips"></div></div>' +
    '<div id="facet-tags"><div class="facet-chips"></div></div>' +
    '<button id="filter-btn"></button>' +
    '<input type="radio" name="sort" value="relevance">' +
    '<input type="radio" name="sort" value="date">' +
    '<span id="folder-count"></span>';
});

afterEach(() => {
  vi.restoreAllMocks();
});

// ── loadFacets ──

describe('loadFacets', () => {
  it('fetches facets for path, stores window.facets, renders options', async () => {
    const { fn, calls } = captureFetch(jsonResponse(FACETS));
    await SF.loadFacets('lib/series', { fetch: fn });
    expect(calls[0].url).toContain('/api/v1/facets?');
    expect(calls[0].url).toContain('path=lib%2Fseries');
    expect(window.facets).toEqual(FACETS);
    const chips = document.querySelector('#facet-artists .facet-chips');
    expect(chips.innerHTML).toContain('Artist A');
    expect(chips.innerHTML).toContain('Artist B');
    expect(document.querySelector('#facet-tags .facet-chips').innerHTML).toContain('tag1');
  });

  it('sends include_hidden when FolderVisibility.showAll', async () => {
    window.FolderVisibility = { showAll: true };
    const { fn, calls } = captureFetch(jsonResponse(FACETS));
    await SF.loadFacets({ fetch: fn });
    expect(calls[0].url).toContain('include_hidden=true');
  });

  it('discards a superseded response (generation guard)', async () => {
    const d1 = deferred();
    const { fn } = captureFetch((url, init, n) => (n === 1 ? d1.p : jsonResponse(FACETS)));
    const p1 = SF.loadFacets('a', { fetch: fn });
    await SF.loadFacets('b', { fetch: fn });   // bumps facetLoadGen
    d1.resolve(jsonResponse({ artists: ['STALE'], tags: [] }));
    await p1;
    expect(window.facets).toEqual(FACETS);   // second call won
  });

  it('warns and keeps prior facets on error', async () => {
    window.facets = { artists: ['keep'], tags: [] };
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const { fn } = captureFetch(new Response('{"code":"x"}', { status: 500 }));
    await SF.loadFacets('', { fetch: fn });
    expect(window.facets).toEqual({ artists: ['keep'], tags: [] });
    expect(warn).toHaveBeenCalled();
  });
});

// ── renderFacetOptions ──

describe('renderFacetOptions', () => {
  it('marks active facets, toggles selection on click', () => {
    window.facets = FACETS;
    window.activeFilters = { artist: ['Artist A'], tag: [], sort: 'relevance' };
    SF.renderFacetOptions();
    const chips = [...document.querySelectorAll('#facet-artists .chip')];
    expect(chips).toHaveLength(2);
    const a = chips.find((c) => c.dataset.value === 'Artist A');
    const b = chips.find((c) => c.dataset.value === 'Artist B');
    expect(a.classList.contains('active')).toBe(true);
    expect(a.getAttribute('aria-pressed')).toBe('true');
    expect(b.classList.contains('active')).toBe(false);

    b.click();   // select Artist B
    expect(window.activeFilters.artist).toEqual(['Artist A', 'Artist B']);
    expect(b.classList.contains('active')).toBe(true);
    b.click();   // deselect
    expect(window.activeFilters.artist).toEqual(['Artist A']);
  });

  it('checks the sort radio matching activeFilters.sort; filter-btn reflects filters', () => {
    window.facets = { artists: [], tags: [] };
    window.activeFilters = { artist: [], tag: [], sort: 'date' };
    SF.renderFacetOptions();
    const radios = [...document.querySelectorAll('input[name="sort"]')];
    expect(radios.find((r) => r.value === 'date').checked).toBe(true);
    expect(radios.find((r) => r.value === 'relevance').checked).toBe(false);
    expect(document.getElementById('filter-btn').classList.contains('active')).toBe(false);

    window.activeFilters.artist.push('X');
    SF.renderFacetOptions();
    expect(document.getElementById('filter-btn').classList.contains('active')).toBe(true);
  });

  it('renders placeholders for empty facet lists', () => {
    window.facets = { artists: [], tags: [] };
    SF.renderFacetOptions();
    expect(document.querySelector('#facet-artists .facet-chips').innerHTML).toContain('No artists');
    expect(document.querySelector('#facet-tags .facet-chips').innerHTML).toContain('No tags');
  });
});
