import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

// browse/hash.js — hash routing & URL state. location/history are real in
// happy-dom; window.* state is the app.js seam. applyUrlState is the single
// hashchange listener — tests dispatch the event explicitly.
const HR = globalThis.HashRouter;

function setHash(h) {
  history.replaceState(null, '', location.pathname + location.search + h);
}

function viewerState() {
  window.currentFolder = 'works/w1';
  window.images = [{}, {}];
  window.chapterList = [{ name: 'ch', fullPath: 'works/w1', images: [] }];
  window.chapterBoundaries = [0];
  window.currentIdx = 0;
  window.landscapeSpread = true;
  window.spreadOffset = 0;
}

beforeEach(() => {
  for (const k of ['tree', 'treeLoaded', 'currentFolder', 'images', 'chapterList',
    'chapterBoundaries', 'currentIdx', 'currentBrowsePath', 'viewMode', 'urlRestoring',
    'urlRestoreGen', 'loadImages', 'renderView', 'updateBrowseHeader',
    'navigateToFolder', 'redirectHiddenToTop', 'fvGuardPath', 'FolderVisibility',
    'PairedView', 'PairController', 'PairUI', 'BrowseScroll', 'getNodeAt',
    'getParentPath', 'getScrollKey', 'scrollPositions', 'resetStrip', 'teardownViewer',
    'updateFooterBtn', 'getCurrentChapterInfo']) delete window[k];
  window.urlStack = [];
  window.urlStackPos = -1;
  document.body.innerHTML = '<div id="empty" style="display:none"></div><input id="search-input"><div id="sidebar-scroll"></div>';
  setHash('');
});

afterEach(() => setHash(''));

describe('parseViewerHash / viewerHash', () => {
  it('parses #/view/<path>?p=&s=&o=', () => {
    setHash('#/view/a%20b/c?p=3&s=1&o=1');
    expect(HR.parseViewerHash()).toEqual({ folder: 'a b/c', page: 3, s: 1, o: 1 });
  });

  it('defaults page=0, s/o=null; rejects non-viewer hashes', () => {
    setHash('#/view/folder');
    expect(HR.parseViewerHash()).toEqual({ folder: 'folder', page: 0, s: null, o: null });
    setHash('#/browse/x');
    expect(HR.parseViewerHash()).toBeNull();
    setHash('');
    expect(HR.parseViewerHash()).toBeNull();
  });

  it('returns null on malformed percent-encoding', () => {
    setHash('#/view/%E0%A4%A');
    expect(HR.parseViewerHash()).toBeNull();
  });

  it('viewerHash encodes path segments and omits defaults', () => {
    expect(HR.viewerHash('a b/c', 2)).toBe('#/view/a%20b/c?p=2');
    expect(HR.viewerHash('f', 0, { landscapeSpread: true, spreadOffset: 0 })).toBe('#/view/f?p=0');
    expect(HR.viewerHash('f', 1, { landscapeSpread: false })).toBe('#/view/f?p=1&s=1');
    expect(HR.viewerHash('f', 1, { landscapeSpread: true, spreadOffset: 1 })).toBe('#/view/f?p=1&o=1');
    expect(HR.viewerHash('f', 1, null)).toBe('#/view/f?p=1');
  });
});

describe('parseBrowseHash / browseHash', () => {
  it('parses #/browse/<path>?v=list|card', () => {
    setHash('#/browse/a%20b?v=list');
    expect(HR.parseBrowseHash()).toEqual({ path: 'a b', v: 'list' });
    setHash('#/browse/x?v=card');
    expect(HR.parseBrowseHash()).toEqual({ path: 'x', v: 'card' });
    setHash('#/browse/x');
    expect(HR.parseBrowseHash()).toEqual({ path: 'x', v: null });
  });

  it('non-browse hash → null; malformed → {path:"",v:null}', () => {
    setHash('#/view/f');
    expect(HR.parseBrowseHash()).toBeNull();
    setHash('#/browse/%E0%A4%A');
    expect(HR.parseBrowseHash()).toEqual({ path: '', v: null });
  });

  it('browseHash: root+tile → "", encodes path, v suffix only for list/card', () => {
    expect(HR.browseHash('', 'tile')).toBe('');
    expect(HR.browseHash(null, 'tile')).toBe('');
    expect(HR.browseHash('a b', 'tile')).toBe('#/browse/a%20b');
    expect(HR.browseHash('', 'list')).toBe('#/browse/?v=list');
    expect(HR.browseHash('x', 'card')).toBe('#/browse/x?v=card');
    expect(HR.browseHash('x', 'grid')).toBe('#/browse/x');
  });
});

describe('currentViewerState', () => {
  it('returns null without folder/images/chapterList', () => {
    expect(HR.currentViewerState()).toBeNull();
    viewerState();
    delete window.chapterList;
    expect(HR.currentViewerState()).toBeNull();
  });

  it('projects chapter + local page + layout flags', () => {
    viewerState();
    window.chapterBoundaries = [0, 1];
    window.chapterList = [
      { name: 'ch0', fullPath: 'works/w1' },
      { name: 'ch1', fullPath: 'works/w1/ch1' },
    ];
    window.currentIdx = 1;
    expect(HR.currentViewerState()).toEqual({
      folder: 'works/w1/ch1', page: 0,
      landscapeSpread: true, spreadOffset: 0,
    });
    window.landscapeSpread = false;
    window.spreadOffset = 1;
    expect(HR.currentViewerState()).toMatchObject({ landscapeSpread: false, spreadOffset: 1 });
  });
});

describe('pushViewerState / replaceViewerState / clearViewerState', () => {
  it('pushViewerState writes #/view/ URL via pushState (slashes literal)', () => {
    viewerState();
    HR.pushViewerState();
    expect(location.hash).toBe('#/view/works/w1?p=0');
    expect(window.urlStack.length).toBeGreaterThan(0);
  });

  it('replaceViewerState uses replaceState (no stack growth)', () => {
    viewerState();
    HR.replaceViewerState();
    expect(location.hash).toBe('#/view/works/w1?p=0');
  });

  it('no-ops when already at target', () => {
    viewerState();
    HR.pushViewerState();
    const calls = [];
    const orig = history.pushState.bind(history);
    history.pushState = (...a) => { calls.push(a); return orig(...a); };
    try { HR.pushViewerState(); } finally { history.pushState = orig; }
    expect(calls).toHaveLength(0);
  });

  it('clearViewerState strips the hash', () => {
    setHash('#/view/x?p=1');
    HR.clearViewerState();
    expect(location.hash).toBe('');
  });
});

describe('urlStack', () => {
  it('push/replace/reconcile keep pos in range', () => {
    window.urlStackPush('/a');
    window.urlStackPush('/b');
    expect(window.urlStack).toEqual(['/a', '/b']);
    expect(window.urlStackPos).toBe(1);
    window.urlStackReplace('/b2');
    expect(window.urlStack[1]).toBe('/b2');
    // truncate forward entries on push
    window.urlStackPush('/a0');
    history.replaceState(null, '', '/b2');
    HR.reconcileUrlStack();
    expect(window.urlStackPos).toBe(1);
  });
});

describe('browse URL writers', () => {
  it('writeBrowseStateWith pushes #/browse/ hash for non-root path', () => {
    window.currentBrowsePath = 'series/v1';
    window.viewMode = 'tile';
    HR.writeBrowseState();
    expect(location.hash).toBe('#/browse/series/v1');
  });

  it('writeBrowseStateWithReplace uses replaceState — same URL, no push', () => {
    window.currentBrowsePath = 'x';
    window.viewMode = 'list';
    HR.writeBrowseStateWithReplace('x');
    expect(location.hash).toBe('#/browse/x?v=list');
  });

  it('pushBrowseUrl navigates history.go to a backward ancestor ignoring ?v=', () => {
    // A backward entry pushed while viewMode=list carries "?v=list"; arriving
    // now in tile mode must still key-match it on path alone and go(-1) —
    // the stale query is corrected by applyUrlState after the pop.
    history.replaceState(null, '', '/');
    window.urlStackPush('/#/browse/series?v=list');
    window.urlStackPush('/');
    window.urlStackPos = 1;
    const goSpy = vi.spyOn(history, 'go').mockImplementation(() => {});
    try {
      window.pushBrowseUrl('/#/browse/series');
      expect(goSpy).toHaveBeenCalledWith(-1);
      expect(window.urlStackPos).toBe(0);
    } finally { goSpy.mockRestore(); }
  });
});

describe('applyUrlState (hashchange listener)', () => {
  function fireHash() {
    window.dispatchEvent(new Event('hashchange'));
  }

  it('does nothing until treeLoaded', () => {
    viewerState();
    const load = vi.fn();
    window.loadImages = load;
    setHash('#/view/works%2Fw1?p=0');
    fireHash();
    expect(load).not.toHaveBeenCalled();
  });

  it('consumes #/join/<code> once via replaceState', () => {
    window.treeLoaded = true;
    const join = vi.fn(() => Promise.resolve());
    window.PairController = { state: { status: 'idle' }, join };
    setHash('#/join/123456');
    fireHash();
    expect(join).toHaveBeenCalledWith('123456');
    expect(location.hash).toBe('');   // consumed — never reaches parsers
  });

  it('browse hash restores browse path; bare hash resets to top', () => {
    window.treeLoaded = true;
    window.viewMode = 'tile';
    window.getNodeAt = (p) => ({ name: p });   // 'series' resolves
    const header = vi.fn();
    const render = vi.fn();
    window.updateBrowseHeader = header;
    window.renderView = render;
    // 本番の updateBrowseHeader は BrowseMatchSort.syncLevel を呼び、
    // レベル変化でクエリをクリアする — stub でも同じ配線を再現する。
    const BMS = globalThis.BrowseMatchSort;
    window.currentBrowsePath = 'old-level';
    BMS.setQuery('vol');
    window.updateBrowseHeader = () => {
      header();
      BMS.syncLevel(window.currentBrowsePath);
    };
    try {
      setHash('#/browse/series');
      fireHash();
      // restoreBrowse observable effects: path recorded, header+grid refresh
      expect(window.currentBrowsePath).toBe('series');
      expect(header).toHaveBeenCalled();
      expect(render).toHaveBeenCalled();
      // レベルが変わったのでクエリはクリア済み
      expect(BMS.isActive()).toBe(false);

      // viewer state present + empty hash → resetToTop clears browse path
      viewerState();
      setHash('');
      fireHash();
      expect(window.currentBrowsePath).toBeNull();
      expect(window.currentFolder).toBeNull();
    } finally {
      delete window.getNodeAt;
    }
  });

  it('corrects viewMode from ?v= even on a no-op-looking hash', () => {
    window.treeLoaded = true;
    window.viewMode = 'tile';
    window.currentBrowsePath = 'series';
    setHash('#/browse/series?v=list');
    fireHash();
    expect(window.viewMode).toBe('list');
  });
});

describe('applyUrlState — #/join/<code>?gt=<token> deep link', () => {
  function fireHash() {
    window.dispatchEvent(new Event('hashchange'));
  }
  const TOKEN = 'g'.repeat(32);
  const GH = '#/join/123456?gt=' + TOKEN;
  const flush = () => new Promise((r) => setTimeout(r, 0));

  beforeEach(() => {
    window.treeLoaded = true;
    window.showErrorToast = vi.fn();
    globalThis.BookqApiEndpoints = { guestRedeem: '/api/v1/guest/redeem' };
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    delete globalThis.BookqApiEndpoints;
  });

  it('redeems the token then joinGuest + opens the pair panel', async () => {
    const fetchMock = vi.fn(() => Promise.resolve({ ok: true }));
    vi.stubGlobal('fetch', fetchMock);
    const joinGuest = vi.fn(() => Promise.resolve());
    const open = vi.fn();
    window.PairController = { state: { status: 'idle' }, joinGuest };
    window.PairUI = { open };
    setHash(GH);
    fireHash();
    expect(location.hash).toBe('');   // consumed once via replaceState
    await flush();
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/guest/redeem',
      expect.objectContaining({ method: 'POST' }));
    expect(JSON.parse(fetchMock.mock.calls[0][1].body))
      .toEqual({ token: TOKEN });
    expect(joinGuest).toHaveBeenCalledWith(TOKEN);
    expect(open).toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it('dead grant → error toast, no join', async () => {
    const fetchMock = vi.fn(() => Promise.resolve({ ok: false }));
    vi.stubGlobal('fetch', fetchMock);
    const joinGuest = vi.fn(() => Promise.resolve());
    window.PairController = { state: { status: 'idle' }, joinGuest };
    window.PairUI = { open: vi.fn() };
    setHash(GH);
    fireHash();
    await flush();
    expect(joinGuest).not.toHaveBeenCalled();
    expect(window.showErrorToast).toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it('redeem fetch failure → error toast, no join', async () => {
    const fetchMock = vi.fn(() => Promise.reject(new Error('net')));
    vi.stubGlobal('fetch', fetchMock);
    const joinGuest = vi.fn(() => Promise.resolve());
    window.PairController = { state: { status: 'idle' }, joinGuest };
    setHash(GH);
    fireHash();
    await flush();
    expect(joinGuest).not.toHaveBeenCalled();
    expect(window.showErrorToast).toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it('active session is never torn down — panel opens, NO redeem', async () => {
    const fetchMock = vi.fn(() => Promise.resolve({ ok: true }));
    vi.stubGlobal('fetch', fetchMock);
    const joinGuest = vi.fn(() => Promise.resolve());
    const open = vi.fn();
    window.PairController = {
      state: { status: 'connected' }, joinGuest,
    };
    window.PairUI = { open };
    setHash(GH);
    fireHash();
    await flush();
    // The guard runs BEFORE redeem: clicking another invite must not swap
    // the guest cookie out from under the running pair.
    expect(fetchMock).not.toHaveBeenCalled();
    expect(joinGuest).not.toHaveBeenCalled();
    expect(open).toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it('stale shell without joinGuest — no redeem, reload toast', async () => {
    const fetchMock = vi.fn(() => Promise.resolve({ ok: true }));
    vi.stubGlobal('fetch', fetchMock);
    window.PairController = { state: { status: 'idle' } };   // no joinGuest
    setHash(GH);
    fireHash();
    await flush();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(window.showErrorToast).toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it('malformed guest hash falls through — no redeem call', async () => {
    const fetchMock = vi.fn(() => Promise.resolve({ ok: true }));
    vi.stubGlobal('fetch', fetchMock);
    window.PairController = { state: { status: 'idle' }, joinGuest: vi.fn() };
    setHash('#/join/123456?gt=short');
    fireHash();
    await flush();
    expect(fetchMock).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});

describe('applyUrlState — dead links & guards (UX/risk)', () => {
  function fireHash() {
    window.dispatchEvent(new Event('hashchange'));
  }

  it('dead browse path clears the hash back to canonical root', () => {
    window.treeLoaded = true;
    window.viewMode = 'tile';
    window.getNodeAt = () => null;            // path does not resolve
    window.updateBrowseHeader = vi.fn();
    window.renderView = vi.fn();
    setHash('#/browse/ghost-folder?v=list');
    fireHash();
    // "a dead or empty folder clears the hash" — unconditional, ?v= too
    expect(location.hash).toBe('');
    expect(window.currentBrowsePath).toBeNull();
  });

  it('viewer hash loads the DECODED folder via loadImages', () => {
    window.treeLoaded = true;
    window.PairedView = undefined;
    const load = vi.fn();
    window.loadImages = load;
    window.FolderVisibility = { guardPath: () => 'ok', showAll: false };
    setHash('#/view/a%20b%2Fc?p=2');
    fireHash();
    expect(load).toHaveBeenCalledWith('a b/c');
  });

  it('hidden folder guard redirects to top instead of loading', () => {
    window.treeLoaded = true;
    const load = vi.fn();
    const redirect = vi.fn();
    window.loadImages = load;
    window.redirectHiddenToTop = redirect;
    window.FolderVisibility = { guardPath: () => 'hidden', showAll: false };
    setHash('#/view/secret/p=0'.replace('p=0','') + '?p=0');
    setHash('#/view/secret?p=0');
    fireHash();
    expect(redirect).toHaveBeenCalled();
    expect(load).not.toHaveBeenCalled();
  });
});
