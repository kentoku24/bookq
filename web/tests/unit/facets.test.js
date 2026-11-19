import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

// search/facets.js — search orchestration + facet/filter UI. Transport goes
// through window.BookqApi with deps.fetch injection; window.* state is the
// app.js seam.
const SF = globalThis.SearchFacets;
const API = globalThis.BookqApi;

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
const RESULTS = {
  query: 'q', total: 1,
  results: [{
    folder_path: 'lib/work1', folder_name: 'Work 1', title_en: 'Title EN',
    title_jp: '題名', has_metadata: true, artist: ['Artist A'], tags: ['tag1'],
  }],
};

beforeEach(() => {
  for (const k of ['Connectivity', 'searchResults', 'searchMode', 'activeFilters',
    'facets', 'facetLoadGen', 'searchTimer', 'refreshOfflineMap', 'renderView',
    'FolderVisibility', 'tree', 'offlineMap', 'offlineRecordMap', 'getLeafPathsFor',
    'progressBarHtml', 'BrowseRender', 'StoreAdapter', 'offlineControlsHtml',
    'offlineControlsHtmlCtx', 'buildNodeMap', 'ReadingProgress', 'loadImages',
    'closeSidebar', 'currentFolder', 'currentBrowsePath', 'searchGen']) {
    delete window[k];
  }
  window.activeFilters = { artist: [], tag: [], sort: 'relevance' };
  window.facetLoadGen = 0;
  document.body.innerHTML =
    '<div id="tile-grid"></div><input id="search-input" value="">' +
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
  clearTimeout(window.searchTimer);
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

// ── doSearch ──

describe('doSearch', () => {
  it('empty query resets search mode/results and refreshes view', async () => {
    window.searchMode = true;
    window.searchResults = [{ folder_path: 'x' }];
    const render = vi.fn();
    window.renderView = render;
    const refresh = vi.fn(async () => {});
    window.refreshOfflineMap = refresh;
    await SF.doSearch({ fetch: vi.fn() });
    expect(window.searchMode).toBe(false);
    expect(window.searchResults).toEqual([]);
    expect(render).toHaveBeenCalled();
    expect(refresh).toHaveBeenCalled();
  });

  it('sends q + joined filters + sort; stores results, sets searchMode', async () => {
    document.getElementById('search-input').value = '  hello  ';
    window.activeFilters = { artist: ['A1', 'A2'], tag: ['t'], sort: 'date' };
    const render = vi.fn();
    window.renderView = render;
    const { fn, calls } = captureFetch(jsonResponse(RESULTS));
    await SF.doSearch({ fetch: fn });
    const url = calls[0].url;
    expect(url).toContain('/api/v1/search?');
    expect(url).toContain('q=hello');
    expect(url).toContain('artist=A1%2CA2');
    expect(url).toContain('tag=t');
    expect(url).toContain('sort=date');
    expect(window.searchResults).toEqual(RESULTS.results);
    expect(window.searchMode).toBe(true);
    expect(render).toHaveBeenCalled();
  });

  it('include_hidden only in show-all mode', async () => {
    document.getElementById('search-input').value = 'q';
    const { fn: f1, calls: c1 } = captureFetch(jsonResponse(RESULTS));
    await SF.doSearch({ fetch: f1 });
    expect(c1[0].url).not.toContain('include_hidden');
    window.FolderVisibility = { showAll: true };
    const { fn: f2, calls: c2 } = captureFetch(jsonResponse(RESULTS));
    await SF.doSearch({ fetch: f2 });
    expect(c2[0].url).toContain('include_hidden=true');
  });

  it('invalidateSearch discards an in-flight response', async () => {
    document.getElementById('search-input').value = 'q';
    const d = deferred();
    const { fn } = captureFetch(d.p);
    const p = SF.doSearch({ fetch: fn });
    SF.invalidateSearch();
    d.resolve(jsonResponse(RESULTS));
    await p;
    expect(window.searchResults).toBeUndefined();
    expect(window.searchMode).toBeUndefined();
  });
});

// ── updateFilterChips ──

describe('updateFilterChips', () => {
  it('renders a removable chip per active artist/tag', () => {
    window.activeFilters = { artist: ['A&B'], tag: ['t1'], sort: 'relevance' };
    SF.updateFilterChips();
    const el = document.getElementById('filter-chips');
    expect(el.classList.contains('hidden')).toBe(false);
    expect(el.innerHTML).toContain('A&amp;B');
    expect(el.innerHTML).toContain('data-filter="artist"');
    expect(el.innerHTML).toContain('data-filter="tag"');
    expect(el.querySelectorAll('.chip-remove')).toHaveLength(2);
  });

  it('hides the bar when no filters are active', () => {
    SF.updateFilterChips();
    const el = document.getElementById('filter-chips');
    expect(el.classList.contains('hidden')).toBe(true);
  });

  it('chip-remove click drops the filter and reschedules doSearch', async () => {
    window.activeFilters = { artist: ['A1', 'A2'], tag: [], sort: 'relevance' };
    SF.updateFilterChips();
    const btn = document.querySelector('.chip-remove[data-value="A1"]');
    btn.click();
    expect(window.activeFilters.artist).toEqual(['A2']);
    // rerendered: A1 chip gone
    expect(document.getElementById('filter-chips').innerHTML).not.toContain('>A1<');
    // doSearch is scheduled on window.searchTimer (300ms debounce)
    expect(window.searchTimer).toBeTruthy();
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

// ── search result renderers ──

function stubRenderDeps() {
  window.BrowseRender = { nameHtml: (n) => `<span class="nn">${n}</span>` };
  window.tree = [{ _fullPath: 'lib/work1', name: 'work1', thumb: 'cover.jpg', type: 'folder' }];
}

describe('renderSearchTiles', () => {
  beforeEach(() => {
    stubRenderDeps();
    window.searchResults = RESULTS.results;
    window.searchMode = true;
  });

  it('renders a tile per result with cover, name, metadata badges, count', () => {
    SF.renderSearchTiles();
    const grid = document.getElementById('tile-grid');
    expect(grid.innerHTML).toContain('data-path="lib/work1"');
    expect(grid.innerHTML).toContain('/thumb/lib/work1/cover.jpg');
    expect(grid.innerHTML).toContain('metadata-badge artist');
    expect(grid.innerHTML).toContain('Artist A');
    expect(grid.innerHTML).toContain('metadata-badge tag');
    expect(document.getElementById('folder-count').textContent).toBe('1 件');
  });

  it('empty results → empty-state message', () => {
    window.searchResults = [];
    SF.renderSearchTiles();
    expect(document.getElementById('tile-grid').innerHTML).toContain('一致する結果がありません');
    expect(document.getElementById('folder-count').textContent).toBe('0 件');
  });

  it('FolderVisibility.isVisible filters hidden paths out of the grid', () => {
    window.FolderVisibility = {
      showAll: false,
      isVisible: (p) => p !== 'lib/work1',
      uiPresentation: () => null,
      blockedGridHtml: () => null,
    };
    SF.renderSearchTiles();
    expect(document.getElementById('tile-grid').innerHTML).not.toContain('lib/work1');
    expect(document.getElementById('folder-count').textContent).toBe('0 件');
  });

  it('blockedGridHtml short-circuits rendering while visibility is pending', () => {
    window.FolderVisibility = {
      showAll: false,
      isVisible: () => true,
      uiPresentation: () => null,
      blockedGridHtml: () => '<div class="vis-blocked">loading</div>',
    };
    SF.renderSearchTiles();
    expect(document.getElementById('tile-grid').innerHTML).toBe('<div class="vis-blocked">loading</div>');
  });

  it('clicking a result tile navigates: currentFolder + loadImages + search reset', () => {
    const load = vi.fn();
    window.loadImages = load;
    SF.renderSearchTiles();
    const tile = document.querySelector('#tile-grid .tile');
    tile.click();
    expect(window.currentFolder).toBe('lib/work1');
    expect(load).toHaveBeenCalledWith('lib/work1');
    expect(window.searchMode).toBe(false);
    expect(window.searchResults).toEqual([]);
    expect(document.getElementById('search-input').value).toBe('');
  });
});

describe('renderSearchList / renderSearchCards', () => {
  beforeEach(() => {
    stubRenderDeps();
    window.searchResults = RESULTS.results;
    window.searchMode = true;
  });

  it('list view renders .item rows with escaped name and metadata', () => {
    SF.renderSearchList();
    const html = document.getElementById('tile-grid').innerHTML;
    expect(html).toContain('class="item folder search-result');
    expect(html).toContain('data-path="lib/work1"');
    expect(html).toContain('>Work 1<');
    expect(html).toContain('metadata-badge artist');
    expect(document.getElementById('folder-count').textContent).toBe('1 件');
  });

  it('card view prefers title_en over folder_name for the card title', () => {
    SF.renderSearchCards();
    const html = document.getElementById('tile-grid').innerHTML;
    expect(html).toContain('class="card"');
    expect(html).toContain('card-title');
    expect(html).toContain('Title EN');
    expect(html).toContain('tag1');
    expect(document.getElementById('folder-count').textContent).toBe('1 件');
  });

  it('card view falls back to title_jp then folder_name', () => {
    window.searchResults = [{ ...RESULTS.results[0], title_en: '', title_jp: '和題' }];
    SF.renderSearchCards();
    expect(document.getElementById('tile-grid').innerHTML).toContain('和題');
    window.searchResults = [{ ...RESULTS.results[0], title_en: '', title_jp: '' }];
    SF.renderSearchCards();
    expect(document.getElementById('tile-grid').innerHTML).toContain('Work 1');
  });
});
