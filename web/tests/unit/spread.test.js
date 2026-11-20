import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import soloSpreads from '../../../api/vectors/solo-spreads.json';
import chapterPadding from '../../../api/vectors/chapter-padding.json';

// viewer/spread.js — spread model + strip math. window.* is the state seam
// app.js owns; tests install it exactly like the Playwright conformance
// harness does (tests/test_conformance_vectors_js.py).
const SV = globalThis.SpreadView;

function resetViewerState() {
  for (const k of [
    'images', 'spreads', 'idxToSpread', 'pageMode', 'spreadOffset',
    'chapterBoundaries', 'anchorSpread', 'spreadSizeCache', 'pageAR',
    'lastAR', 'viewW', 'viewH', 'currentIdx', 'currentFolder', 'chapterList',
    'realImageCount', 'globalToRealIdx', 'stripImgs', 'stripCells',
    'spreadEls', 'sliding', 'snapGen', 'urlSyncPaused', 'renderPagesForSpread',
    'landscapeSpread', 'chapterLabels', 'getUrl', 'PREFETCH_AHEAD',
    'PREFETCH_BEHIND', 'EVICT_AHEAD', 'EVICT_BEHIND', 'updateSpreadToggleLabel',
  ]) delete window[k];
}

beforeEach(() => {
  resetViewerState();
  document.body.innerHTML = '<div id="img-track"></div><input id="page-slider"><span id="counter"></span><span id="folder-name"></span><div id="img-container"></div><div id="spinner"></div>';
});

afterEach(() => {
  resetViewerState();
  vi.useRealTimers();
});

// ── Conformance vectors (ported from tests/test_conformance_vectors_js.py) ──

describe('rebuildSpreads conformance vectors', () => {
  it('matches api/vectors/solo-spreads.json', () => {
    expect(soloSpreads.languages).toContain('js');
    for (const c of soloSpreads.cases) {
      window.images = c.images;
      window.chapterBoundaries = c.chapterBoundaries;
      window.pageMode = c.pageMode;
      window.spreadOffset = c.spreadOffset;
      window.anchorSpread = 0;
      window.spreadSizeCache = {};
      window.rebuildSpreads();
      const got = {
        spreads: window.spreads.map((s) => ({ pages: s.pages, chapterStart: !!s.chapterStart })),
        idxToSpread: Array.from(window.idxToSpread, (v) => (v === undefined ? -1 : v)),
      };
      expect(got.spreads.map((s) => s.pages), c.name).toEqual(c.expectSpreads);
      expect(got.spreads.map((s) => s.chapterStart), c.name).toEqual(c.expectChapterStart);
      expect(got.idxToSpread, c.name).toEqual(c.expectIdxToSpread);
    }
  });
});

describe('padChapterSpacers + rebuildSpreads conformance vectors', () => {
  it('matches api/vectors/chapter-padding.json', () => {
    expect(chapterPadding.languages).toContain('js');
    for (const c of chapterPadding.cases) {
      const images = [];
      const boundaries = [];
      for (const ch of c.chapters) {
        boundaries.push(images.length);
        for (const p of ch.pages) images.push({ folder: ch.folder, name: p });
      }
      window.padChapterSpacers(images, boundaries, c.pageMode, c.spreadOffset);
      window.images = images;
      window.chapterBoundaries = boundaries;
      window.pageMode = c.pageMode;
      window.spreadOffset = c.spreadOffset;
      window.anchorSpread = 0;
      window.spreadSizeCache = {};
      window.rebuildSpreads();
      const got = {
        images: images.map((im) => [im.folder, im.name, !!im.isSpacer]),
        boundaries,
        spreads: window.spreads.map((s) => s.pages),
        chapterStart: window.spreads.map((s) => !!s.chapterStart),
        idxToSpread: Array.from(window.idxToSpread, (v) => (v === undefined ? -1 : v)),
      };
      expect(got.images, c.name).toEqual(c.expectImages.map((e) => [e.folder, e.name, !!e.isSpacer]));
      expect(got.boundaries, c.name).toEqual(c.expectBoundaries);
      expect(got.spreads, c.name).toEqual(c.expectSpreads);
      expect(got.chapterStart, c.name).toEqual(c.expectChapterStart);
      expect(got.idxToSpread, c.name).toEqual(c.expectIdxToSpread);
    }
  });
});

// ── rebuildSpreads edge cases ──

describe('rebuildSpreads', () => {
  it('single mode makes one spread per non-spacer page', () => {
    window.images = [{ folder: 'f', name: '1' }, { folder: 'f', name: '2' }, { isSpacer: true, name: '__spacer__' }, { folder: 'f', name: '3' }];
    window.pageMode = 'single';
    window.rebuildSpreads();
    expect(window.spreads.map((s) => s.pages)).toEqual([[0], [1], [3]]);
    expect(window.idxToSpread[2]).toBeUndefined();   // spacer keeps a hole
  });

  it('spread mode pairs pages honoring spreadOffset lead-in', () => {
    window.images = [0, 1, 2, 3, 4].map((i) => ({ folder: 'f', name: String(i) }));
    window.pageMode = 'spread';
    window.spreadOffset = 1;
    window.rebuildSpreads();
    expect(window.spreads.map((s) => s.pages)).toEqual([[0], [1, 2], [3, 4]]);
  });

  it('marks chapterStart when first page is a chapter boundary', () => {
    window.images = [0, 1, 2, 3].map((i) => ({ folder: 'f', name: String(i) }));
    window.pageMode = 'single';
    window.chapterBoundaries = [0, 2];
    window.rebuildSpreads();
    // spread k is chapterStart when its FIRST page index is a boundary —
    // boundary 2 lands on spread index 2 (single mode: spread==page)
    expect(window.spreads.map((s) => !!s.chapterStart)).toEqual([false, false, true, false]);
  });

  it('clamps anchorSpread into range', () => {
    window.images = [0, 1].map((i) => ({ folder: 'f', name: String(i) }));
    window.pageMode = 'single';
    window.anchorSpread = 99;
    window.rebuildSpreads();
    expect(window.anchorSpread).toBe(1);
  });
});

// ── index helpers ──

describe('spreadOf / arOf / slider helpers', () => {
  beforeEach(() => {
    window.images = [0, 1, 2, 3].map((i) => ({ folder: 'f', name: String(i) }));
    window.pageMode = 'spread';
    window.spreadOffset = 0;
    window.rebuildSpreads();   // spreads: [0,1],[2,3]
  });

  it('spreadOf maps page index → spread index (default 0)', () => {
    expect(SV.spreadOf(0)).toBe(0);
    expect(SV.spreadOf(1)).toBe(0);
    expect(SV.spreadOf(2)).toBe(1);
    expect(SV.spreadOf(99)).toBe(0);   // unmapped → 0
  });

  it('arOf prefers pageAR, falls back to lastAR then 0.7', () => {
    window.pageAR = { 0: 1.5 };
    window.lastAR = 0.85;
    expect(SV.arOf(0)).toBe(1.5);
    expect(SV.arOf(1)).toBe(0.85);
    delete window.lastAR;
    expect(SV.arOf(1)).toBe(0.7);
  });

  it('sliderMax/idxToSlider/sliderToIdx translate spread ↔ page', () => {
    expect(SV.sliderMax()).toBe(1);
    expect(SV.idxToSlider(3)).toBe(1);
    expect(SV.sliderToIdx(0)).toBe(0);
    expect(SV.sliderToIdx(1)).toBe(2);
    expect(SV.sliderToIdx(99)).toBe(2);   // clamped to last spread's first page
    window.spreads = [];
    expect(SV.sliderMax()).toBe(0);
    expect(SV.sliderToIdx(0)).toBe(0);
  });
});

// ── metrics math ──

describe('spreadMetrics / gapBefore / spreadLeft / pageOffsetPx', () => {
  beforeEach(() => {
    window.viewW = 1000;
    window.viewH = 500;
    window.images = [0, 1, 2, 3].map((i) => ({ folder: 'f', name: String(i) }));
    window.pageMode = 'spread';
    window.pageAR = { 0: 1, 1: 1, 2: 1, 3: 1 };   // square pages
    window.lastAR = 1;
    window.rebuildSpreads();   // spreads: [0,1],[2,3]
    window.anchorSpread = 0;
  });

  it('spreadMetrics: arSum*h + inner gaps, capped at viewW', () => {
    const m = SV.spreadMetrics(0);
    // ars=[1,1] → arSum=2, gaps=5, h=500 → w=1005 > 1000 → h=(1000-5)/2=497.5
    expect(m.width).toBe(1000);
    expect(m.height).toBe(497.5);
    expect(m.pageWidths).toEqual([497.5, 497.5]);
  });

  it('spreadMetrics honors renderPagesForSpread projection', () => {
    window.renderPagesForSpread = () => [0];   // solo projection of spread 0
    SV.invalidateSpreadSizes();
    const m = SV.spreadMetrics(0);
    // single page ar=1 → w=500 ≤ 1000 → h stays 500, w=500
    expect(m.width).toBe(500);
    expect(m.height).toBe(500);
    expect(m.pageWidths).toEqual([500]);
  });

  it('spreadMetrics caches per spread and missing spread → zeros', () => {
    const m1 = SV.spreadMetrics(0);
    expect(SV.spreadMetrics(0)).toBe(m1);       // same cached object
    expect(SV.spreadMetrics(9)).toEqual({ width: 0, height: 0, pageWidths: [] });
    SV.invalidateSpreadSizes();
    expect(SV.spreadMetrics(0)).not.toBe(m1);
  });

  it('gapBefore: chapterStart → 20 floor, else GAP_OUTER/leftover math', () => {
    window.chapterBoundaries = [0, 2];
    window.rebuildSpreads();
    // spread 1 starts a chapter → base 20; neighbor leftovers widen it
    const g = SV.gapBefore(1);
    expect(g).toBeGreaterThanOrEqual(20);
    // non-chapter spread → at least GAP_OUTER(10) unless leftovers force more
    window.chapterBoundaries = [0];
    window.rebuildSpreads();
    SV.invalidateSpreadSizes();
    expect(SV.gapBefore(1)).toBeGreaterThanOrEqual(10);
  });

  it('spreadLeft accumulates metrics+gaps away from anchor', () => {
    window.images = [0, 1, 2, 3, 4, 5].map((i) => ({ folder: 'f', name: String(i) }));
    window.rebuildSpreads();   // 3 spreads
    window.anchorSpread = 1;
    const right = SV.spreadLeft(2);
    const m2 = SV.spreadMetrics(2);
    const g2 = SV.gapBefore(2);
    expect(right).toBe(-(m2.width + g2));
    const left = SV.spreadLeft(0);
    const m1 = SV.spreadMetrics(1);
    const g1 = SV.gapBefore(1);
    expect(left).toBe(m1.width + g1);
    expect(SV.spreadLeft(1)).toBe(0);
  });

  it('pageOffsetPx centers the spread in the viewport', () => {
    window.anchorSpread = 0;
    const off = SV.pageOffsetPx(0);
    expect(off).toBe((window.viewW - SV.spreadMetrics(0).width) / 2 - SV.spreadLeft(0));
  });
});

// ── recordAR ──

describe('recordAR', () => {
  it('records aspect ratio, updates lastAR, invalidates spread cache', () => {
    window.images = [{ folder: 'f', name: '1' }];
    window.pageMode = 'single';
    window.rebuildSpreads();
    window.spreadSizeCache = { 0: { width: 1, height: 1, pageWidths: [1] } };
    const img = { naturalWidth: 800, naturalHeight: 1200 };
    expect(window.recordAR(0, img)).toBe(true);
    expect(window.pageAR[0]).toBeCloseTo(800 / 1200);
    expect(window.lastAR).toBeCloseTo(800 / 1200);
    expect(window.spreadSizeCache[0]).toBeUndefined();
    // same ar again → false, no rewrite
    expect(window.recordAR(0, img)).toBe(false);
    // missing dims → false
    expect(window.recordAR(0, { naturalWidth: 0, naturalHeight: 0 })).toBe(false);
  });
});

// ── strip image lifecycle ──

describe('ensureStripImage / ensureWindow / evictStrip', () => {
  beforeEach(() => {
    window.images = [0, 1, 2].map((i) => ({ folder: 'f', name: `${i}.png` }));
    window.pageMode = 'single';
    window.rebuildSpreads();
  });

  it('ensureStripImage creates cell+img, registers maps, places into spread el', () => {
    const img = SV.ensureStripImage(0);
    expect(img).toBeTruthy();
    expect(img.src.endsWith('/image/f/0.png')).toBe(true);
    expect(window.stripImgs[0]).toBe(img);
    expect(window.stripCells[0]).toBeTruthy();
    expect(window.spreadEls[0]).toBeTruthy();
    expect(window.spreadEls[0].children.length).toBe(1);
    // idempotent
    expect(SV.ensureStripImage(0)).toBe(img);
  });

  it('returns null for spacer and out-of-range', () => {
    window.images[1] = { isSpacer: true, name: '__spacer__' };
    expect(SV.ensureStripImage(1)).toBeNull();
    expect(SV.ensureStripImage(99)).toBeNull();
    expect(SV.ensureStripImage(-1)).toBeNull();
  });

  it('ensureWindow covers prefetch ahead/behind, skips spacers', () => {
    window.PREFETCH_AHEAD = 1;
    window.PREFETCH_BEHIND = 0;
    window.images.push({ isSpacer: true, name: '__spacer__' });   // idx 3
    window.images.push({ folder: 'f', name: '4.png' });           // idx 4
    SV.ensureWindow(1);
    expect(Object.keys(window.stripImgs).map(Number).sort()).toEqual([0, 1, 2]);
  });

  it('evictStrip drops imgs outside the keep window', () => {
    window.PREFETCH_AHEAD = 10;
    window.PREFETCH_BEHIND = 10;
    for (let i = 0; i < 5; i++) SV.ensureStripImage(i);
    expect(Object.keys(window.stripImgs)).toHaveLength(3);   // only 0..2 exist
    // now with wider images
    window.images = Array.from({ length: 10 }, (_, i) => ({ folder: 'f', name: `${i}.png` }));
    window.rebuildSpreads();
    window.EVICT_AHEAD = 1;
    window.EVICT_BEHIND = 1;
    for (let i = 0; i < 10; i++) SV.ensureStripImage(i);
    SV.evictStrip(5);
    const kept = Object.keys(window.stripImgs).map(Number).sort();
    expect(kept).toEqual([4, 5, 6]);
    // emptied spread containers are removed too
    expect(Object.values(window.spreadEls).every((el) => el.children.length > 0)).toBe(true);
  });
});

// ── updateOrientation ──

describe('updateOrientation', () => {
  it('sets pageMode/body class from viewport + landscapeSpread', () => {
    const iw = window.innerWidth, ih = window.innerHeight;
    try {
      // happy-dom lets us redefine innerWidth/Height
      Object.defineProperty(window, 'innerWidth', { value: 1200, configurable: true });
      Object.defineProperty(window, 'innerHeight', { value: 800, configurable: true });
      window.landscapeSpread = true;
      window.updateOrientation();
      expect(window.viewW).toBe(1200);
      expect(window.viewH).toBe(800);
      expect(window.pageMode).toBe('spread');
      expect(document.body.className).toBe('landscape');

      window.landscapeSpread = false;
      window.updateOrientation();
      expect(window.pageMode).toBe('single');
      expect(document.body.className).toBe('landscape-single');

      Object.defineProperty(window, 'innerWidth', { value: 800, configurable: true });
      Object.defineProperty(window, 'innerHeight', { value: 1200, configurable: true });
      window.updateOrientation();
      expect(window.pageMode).toBe('single');
      expect(document.body.className).toBe('portrait');
    } finally {
      Object.defineProperty(window, 'innerWidth', { value: iw, configurable: true });
      Object.defineProperty(window, 'innerHeight', { value: ih, configurable: true });
    }
  });
});
