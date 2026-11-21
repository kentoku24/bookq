/* bookq viewer/overview — series-wide RTL page grid. Spec §5.3.
 *
 * Loaded BEFORE app.js (the defer order in index.html is a contract, mirrored
 * in sw.js SHELL_ASSETS) so this file's document `keydown` listener runs first
 * and its Escape branch can stopImmediatePropagation() before app.js's Escape
 * handler closes the sidebar behind us. `G`, Escape, #overview-btn and the
 * #menu-btn pre-close all live here for the same reason.
 *
 * Geometry: every cell is 3:4. The width comes from the CSS grid
 * (auto-fill minmax(150px,1fr)), so it is MEASURED from a real cell rather
 * than re-derived here — the arithmetic fallback only runs while the root is
 * still display:none and nothing can be measured.
 *
 * Windowing: a book over HUGE real pages builds ALL cells (so the scrollbar
 * and every offsetTop are real) but only gives an <img> to the ±IMG_WINDOW
 * positions around the page being read; slideWindow() moves that band as the
 * grid scrolls.
 */
(function () {
  'use strict';
  var IMG_WINDOW = 200, HUGE = 600, INERTIA_MS = 100, SUPPRESS_MS = 400, CELL_AR = 3 / 4;
  var GAP = 8, MIN_COL = 150, SECTION_PAD = 16;   // must match style.css
  var FLY_MS = 220, XFADE_FADE_MS = 200, XFADE_MAX_MS = 3000, PINCH_OUT = 1.3;
  var root = null, body = null, header = null, headerBar = null, chip = null, closeBtn = null;
  var open = false, prebuiltFor = null, lastScrollTs = 0, curCenter = null, ro = null;
  var win = null, slideRaf = 0, lastMid = null, lastRatio = 1;

  function q(sel) { return document.querySelector(sel); }
  function images() { return window.images || []; }
  function chapters() { return window.chapterList || []; }
  // Paired (S06): window.currentIdx is the shared spread ANCHOR, but the page
  // this device actually displays may be the spread's other page. app.js's
  // pairedDisplayIdx hook resolves it (own page, or the anchor when blank);
  // solo mode is the identity — .cur must track the DISPLAYED PageRef.
  function curReadingIdx() {
    return (typeof window.pairedDisplayIdx === 'function')
      ? window.pairedDisplayIdx(window.currentIdx || 0)
      : (window.currentIdx || 0);
  }
  function ensureEls() {
    if (!root) {
      root = q('#overview');
      if (!root) return false;
      body = root.querySelector('.ov-body');
      headerBar = root.querySelector('.ov-header');
      header = root.querySelector('.ov-folder');
      chip = root.querySelector('.ov-chip');
    }
    if (!closeBtn) closeBtn = q('#overview-close');
    return !!root;
  }

  // Global indices of the real (non-spacer) pages, in reading order.
  function realList() {
    var out = [], im = images();
    for (var i = 0; i < im.length; i++) if (!im[i].isSpacer) out.push(i);
    return out;
  }
  // Position of `g` in `reals`, or of the nearest real page after it (then
  // before it). `reals` is ascending, so a forward scan is enough.
  function nearestRealPos(reals, g) {
    if (!reals.length) return 0;
    for (var i = 0; i < reals.length; i++) if (reals[i] >= g) return i;
    return reals.length - 1;
  }
  function chapterOf(g) {
    if (window.getCurrentChapterInfo) return window.getCurrentChapterInfo(g);
    return { chapterIdx: 0, chapterName: '', localIdx: g };
  }
  // scan_tree's single-child flattening rewrites a chapter's `name` to
  // "parent/child" (see CLAUDE.md), so the raw chapterName is a path. Headings
  // and the chip want the leaf only.
  function leafName(s) { return String(s || '').split('/').pop(); }
  function thumbSrc(im) { return window.OfflineCore.thumbUrl(im.folder, im.name, 400); }
  function fullSrc(im) { return window.OfflineCore.imageUrl(im.folder, im.name); }
  function folderLabel() {
    if (window.currentFolder) return window.currentFolder;
    var el = q('#folder-name');
    return el ? el.textContent : '';
  }

  // The <img> is created with its URL parked in data-src, never a live `src`:
  // the PREBUILT grid is display:none (no layout/paint cost during reading),
  // and lazy loading keys on scrollport intersection, not visibility, so a
  // live src there would fetch (and make the server resize) two screens of
  // thumbnails on every book open and again on every page turn that moves
  // `.cur` — bandwidth the staged page prefetch exists to protect. hydrate()
  // assigns the src when the grid actually shows (show(), slideWindow()).
  function makeImg(im) {
    var img = document.createElement('img');
    img.loading = 'lazy'; img.decoding = 'async'; img.alt = ''; img.draggable = false;
    img.dataset.src = thumbSrc(im);
    img.dataset.fallback = fullSrc(im);
    return img;
  }
  function hydrate(img) {
    var src = img.dataset.src;
    if (!src) return;
    delete img.dataset.src;
    // One shot: a missing/failed thumbnail falls back to the full image, and
    // the listener is dropped so a failing full image cannot loop.
    img.addEventListener('error', function onErr() {
      img.removeEventListener('error', onErr);
      img.src = img.dataset.fallback;
    });
    img.src = src;
  }
  function hydrateAll() {
    var pending = body.querySelectorAll('img[data-src]');
    for (var i = 0; i < pending.length; i++) hydrate(pending[i]);
  }

  // Arithmetic fallback only — see the header comment.
  function estimateCellWidth() {
    var W = (q('#viewer') || document.body).clientWidth || window.innerWidth;
    var inner = W - SECTION_PAD;
    var cols = Math.max(1, Math.floor((inner + GAP) / (MIN_COL + GAP)));
    return (inner - GAP * (cols - 1)) / cols;
  }
  function cellWidth() {
    var c = root && root.querySelector('.ov-cell');
    if (c) { var w = c.getBoundingClientRect().width; if (w > 1) return w; }
    return estimateCellWidth();
  }
  // One rect read, then a pure write loop — never a per-cell measure.
  function applyHeights() {
    var h = Math.round(cellWidth() / CELL_AR) + 'px';
    var cells = root.querySelectorAll('.ov-cell');
    for (var i = 0; i < cells.length; i++) cells[i].style.height = h;
  }

  function updateChip(g) {
    if (!chip) return;
    var info = chapterOf(g), reals = realList(), pos = nearestRealPos(reals, g), chs = chapters();
    var ch = chs[info.chapterIdx];
    var chTotal = (ch && ch.images) ? ch.images.length : reals.length;
    chip.textContent = (chs.length > 1 ? leafName(info.chapterName) + ' ' : '')
      + (info.localIdx + 1) + '/' + chTotal
      + (chs.length > 1 ? ' ・ 通算 ' + (pos + 1) + '/' + reals.length : '');
  }

  function build(curIdx) {
    if (!ensureEls() || !images().length) return false;
    body.innerHTML = '';
    var requested = curIdx;   // what the caller asked for, spacer or not
    var reals = realList(), huge = reals.length > HUGE;
    // currentIdx can sit on a chapter spacer (no cell of its own): anchor on
    // the next real page, or the previous one at the very end of the book.
    var curPos = nearestRealPos(reals, curIdx);
    curIdx = reals.length ? reals[curPos] : curIdx;   // highlight a real cell, never the spacer
    var lo = huge ? Math.max(0, curPos - IMG_WINDOW) : 0;
    var hi = huge ? Math.min(reals.length - 1, curPos + IMG_WINDOW) : reals.length - 1;
    win = { lo: reals[lo], hi: reals[hi] };
    var multi = chapters().length > 1;
    var frag = document.createDocumentFragment(), sec = null, grid = null, lastCh = -1;
    for (var pos = 0; pos < reals.length; pos++) {
      var g = reals[pos], im = images()[g], info = chapterOf(g);
      if (!sec || (multi && info.chapterIdx !== lastCh)) {
        sec = document.createElement('section');
        sec.className = 'ov-chapter';
        if (multi) {
          var hd = document.createElement('div');
          hd.className = 'ov-heading';
          hd.textContent = leafName(info.chapterName);
          sec.appendChild(hd);
        }
        grid = document.createElement('div');
        grid.className = 'ov-grid';
        sec.appendChild(grid);
        frag.appendChild(sec);
        lastCh = info.chapterIdx;
      }
      var cell = document.createElement('button');
      cell.type = 'button';
      cell.className = 'ov-cell' + (g === curIdx ? ' cur' : '');
      cell.dataset.gidx = String(g);
      if (pos >= lo && pos <= hi) cell.appendChild(makeImg(im));
      var num = document.createElement('span');
      num.className = 'ov-num';
      num.textContent = String(info.localIdx + 1);
      cell.appendChild(num);
      grid.appendChild(cell);
    }
    body.appendChild(frag);
    applyHeights();
    if (header) header.textContent = folderLabel();
    updateChip(curIdx);
    prebuiltFor = requested;
    return true;
  }

  function scrollCurToThird() {
    var cur = root.querySelector('.ov-cell.cur');
    if (!cur) return;
    root.scrollTop = Math.max(0, cur.offsetTop - root.clientHeight / 3 + cur.offsetHeight / 2);
    var r = cur.getBoundingClientRect();
    curCenter = { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  }

  // Refresh the grid position + pinch-drift target for the page being read.
  // Runs on overview open (show()) and at pinch-gesture start (Zoom onStart),
  // NOT on every page turn: measuring needs a laid-out grid, and laying out
  // thousands of cells every turn costs hundreds of ms on a real device (#25).
  // Temporarily unhides a prebuilt-hidden grid to measure, then re-hides it;
  // an already-visible grid is measured in place. Between syncs the `.cur`
  // classes still follow turns via markCurrent below, so the only staleness
  // window is scroll position + curCenter, both refreshed before any consumer
  // can observe them.
  function syncPosition() {
    if (!ensureEls() || !body.children.length) return;
    var wasHidden = root.hidden;
    root.hidden = false;
    scrollCurToThird();
    if (wasHidden && !open) root.hidden = true;
  }

  // Keep a PREBUILT (not yet open) grid's `.cur` cell on the page being read,
  // without rebuilding it: app.js's snapTo calls this at the end of every
  // page turn. Class swaps and text updates work on a display:none tree, so
  // no unhiding here — position (scroll + Zoom.driftTarget's curCenter) is
  // synced lazily via syncPosition(), never here.
  function markCurrent(gidx) {
    if (open || !ensureEls() || !body.children.length) return;
    var reals = realList();
    if (!reals.length) return;
    var g = reals[nearestRealPos(reals, gidx)];   // a spacer has no cell of its own
    var next = body.querySelector('.ov-cell[data-gidx="' + g + '"]');
    if (!next) return;
    var prev = body.querySelector('.ov-cell.cur');
    if (prev === next) return;
    if (prev) prev.classList.remove('cur');
    next.classList.add('cur');
    updateChip(g);
    // Only claim the grid as prebuilt-for-this-page while the ±IMG_WINDOW band
    // still covers it; past that the cell has no <img> and show() must rebuild.
    if (!win || (g >= win.lo && g <= win.hi)) prebuiltFor = gidx;
  }

  // Huge books only: move the ±IMG_WINDOW <img> band to follow the scroll.
  // Reads offsetTop of every 8th cell (not a rect loop) to locate the middle.
  function slideWindow() {
    // Only the OPEN grid windows its images. markCurrent() sets scrollTop on the
    // prebuilt grid, which fires `scroll` — rebuilding the <img> band there would
    // be a whole DOM sweep for something nobody is looking at.
    if (!open || !win) return;
    var cells = root.querySelectorAll('.ov-cell');
    if (cells.length <= HUGE) return;
    var mid = root.scrollTop + root.clientHeight / 2, best = 0, bestD = Infinity;
    for (var i = 0; i < cells.length; i += 8) {
      var d = Math.abs(cells[i].offsetTop - mid);
      if (d < bestD) { bestD = d; best = i; }
    }
    var lo = Math.max(0, best - IMG_WINDOW), hi = Math.min(cells.length - 1, best + IMG_WINDOW);
    if (win.loPos === lo && win.hiPos === hi) return;
    for (var j = 0; j < cells.length; j++) {
      var had = cells[j].querySelector('img');
      if (j >= lo && j <= hi) {
        if (!had) {
          var fresh = makeImg(images()[+cells[j].dataset.gidx]);
          cells[j].insertBefore(fresh, cells[j].firstChild);
          hydrate(fresh);   // slideWindow only runs on the OPEN grid
        }
      } else if (had) { had.remove(); }
    }
    win = { lo: +cells[lo].dataset.gidx, hi: +cells[hi].dataset.gidx, loPos: lo, hiPos: hi };
  }
  function scheduleSlide() {
    if (slideRaf) return;
    slideRaf = requestAnimationFrame(function () { slideRaf = 0; slideWindow(); });
  }

  function prebuild() {
    if (open || !ensureEls() || !images().length) return;
    var run = function () {
      if (open || !images().length) return;
      // Already built for the page being read: the close() at loadImages'
      // head re-arms a prebuild while `images` still holds the previous book,
      // and the success-path prebuild() then queues a second one — without
      // this check a 700-page book is built twice back-to-back. close()
      // empties the grid and nulls prebuiltFor, so a stale grid can never
      // pass here.
      if (body.children.length && prebuiltFor === curReadingIdx()) return;
      // Stay hidden (display:none): a thousand-page grid kept laid out behind
      // the reader makes every forced full-document layout — the per-turn
      // track reflow, markCurrent reads — pay for thousands of cells on a
      // real device (#25). Geometry is measured on demand: show() and
      // syncPosition() unhide before measuring. applyHeights() below falls
      // back to estimateCellWidth() while hidden (same constants as the
      // grid CSS by convention); show() re-applies real heights on open.
      root.classList.add('prebuilt');
      root.inert = true;
      if (build(curReadingIdx())) scrollCurToThird();
      else { root.classList.remove('prebuilt'); }
    };
    if (window.requestIdleCallback) window.requestIdleCallback(run, { timeout: 1000 });
    else setTimeout(run, 300);
  }

  var reducedMotion = window.ViewerGesture.reducedMotion;   // gesture.js loads first (defer-order contract)

  // The <img> the reader is looking at. spread.js keeps window.stripImgs
  // keyed by global index while the page is inside the prefetch window; a page
  // outside it (or a spacer) falls back to the first visible strip image.
  function currentPageImg() {
    var pool = window.stripImgs || {}, el = pool[curReadingIdx()];
    if (el && el.isConnected) return el;
    var imgs = document.querySelectorAll('#img-track img');
    for (var i = 0; i < imgs.length; i++) {
      var r = imgs[i].getBoundingClientRect();
      if (r.width > 2 && r.height > 2) return imgs[i];
    }
    return null;
  }

  // The source half of the FLIP, measured BEFORE show() calls Zoom.reset(false):
  // the reader is looking at the shrunk, drifted page, and the clone has to
  // start from there — after the reset the rect is back at the resting 1.0 box
  // and the flight would begin with a visible jump.
  function flySource() {
    if (reducedMotion()) return null;
    var srcEl = currentPageImg();
    if (!srcEl) return null;
    var r = srcEl.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return null;
    return { left: r.left, top: r.top, width: r.width, height: r.height, url: srcEl.currentSrc || srcEl.src };
  }

  // FLIP entry: the page being read flies into its cell over the overview's own
  // dark background — `.ov-header`/`.ov-body` are faded, NOT the root, so the
  // full-size page underneath never shows through behind the shrinking clone.
  // Exactly one rect pair is measured, on the fire frame, never in a touchmove.
  function flyIn(src) {
    var cur = root.querySelector('.ov-cell.cur');
    if (!cur || !src) return false;
    var dst = cur.getBoundingClientRect();
    if (dst.width < 2 || dst.height < 2) return false;
    var fly = document.createElement('img');
    fly.id = 'ov-fly'; fly.alt = ''; fly.src = src.url;
    // Under <body>, i.e. the root stacking context: any z-index >= 2 clears
    // everything inside #viewer, toolbar included — which is what the flight
    // wants. (#overview's own 12 is in #viewer and never competes with this.)
    fly.style.cssText = 'position:fixed;z-index:2;pointer-events:none;object-fit:contain;'
      + 'left:' + src.left + 'px;top:' + src.top + 'px;width:' + src.width + 'px;height:' + src.height + 'px;'
      + 'transform-origin:center center';
    document.body.appendChild(fly);
    if (headerBar) headerBar.style.opacity = '0';
    body.style.opacity = '0';
    root.style.pointerEvents = 'none';   // same invariant as the `prebuilt` class
    // A fresh element carries no transition; force the start frame anyway so
    // setting transition and transform in one tick still animates.
    void fly.offsetWidth;
    fly.style.transition = 'transform ' + FLY_MS + 'ms ease';
    fly.style.transform = 'translate('
      + ((dst.left + dst.width / 2) - (src.left + src.width / 2)) + 'px,'
      + ((dst.top + dst.height / 2) - (src.top + src.height / 2)) + 'px) scale('
      + (dst.width / src.width) + ',' + (dst.height / src.height) + ')';
    var done = false;
    function finish() {
      if (done) return;
      done = true;
      if (fly.parentNode) fly.parentNode.removeChild(fly);
      if (headerBar) headerBar.style.opacity = '';
      body.style.opacity = '';
      root.style.pointerEvents = '';
      window.Overview._finishFlight = null;
    }
    window.Overview._finishFlight = finish;
    // transitionend is the normal path; the timeout reclaims the clone when the
    // transition never starts (background tab, an interrupted compositor frame).
    fly.addEventListener('transitionend', finish);
    setTimeout(finish, FLY_MS + 80);
    return true;
  }

  // The CSS already makes the toolbar and the spread controls transparent and
  // click-through while the grid is up; `inert` is what also takes them out of
  // the tab order and the accessibility tree, so Tab cannot walk into invisible
  // buttons behind the overlay. Nothing else in the app sets inert on these two.
  function setChromeInert(on) {
    ['#toolbar', '#spread-controls'].forEach(function (sel) {
      var el = q(sel);
      if (el) el.inert = !!on;
    });
  }

  function show(opts) {
    if (open || !ensureEls() || !images().length) return;
    var cur = curReadingIdx();
    var flySrc = (opts && opts.fly) ? flySource() : null;   // before Zoom.reset
    root.classList.remove('prebuilt');
    root.inert = false;
    root.hidden = false;
    if (closeBtn) closeBtn.hidden = false;
    if (prebuiltFor !== cur || !body.children.length) build(cur);
    // The prebuilt grid was built hidden: heights came from the arithmetic
    // fallback. Re-apply with a real cell now that geometry is measurable.
    applyHeights();
    hydrateAll();   // the grid is about to be seen: start the thumbnail fetches now, not at prebuild
    open = true;
    document.body.classList.add('overview-open');
    setChromeInert(true);
    if (window.Zoom) window.Zoom.reset(false);
    syncPosition();
    if (flySrc) flyIn(flySrc);
    var cell = root.querySelector('.ov-cell.cur');
    if (cell) cell.focus({ preventScroll: true });
  }

  function close() {
    if (!ensureEls()) return false;
    var wasOpen = open;
    open = false;
    document.body.classList.remove('overview-open');
    setChromeInert(false);
    root.hidden = true;
    root.classList.remove('prebuilt');
    root.inert = false;
    if (closeBtn) closeBtn.hidden = true;
    if (window.Overview._finishFlight) { try { window.Overview._finishFlight(); } catch (e) { console.warn(e); } }
    body.innerHTML = '';
    prebuiltFor = null; curCenter = null; win = null;
    // close() empties the grid, so the next pinch-in would have to BUILD inside
    // a touchmove and Zoom.driftTarget() would have no cell to aim at until it
    // did. Re-arm the prebuild instead — it is idle-scheduled, refuses while the
    // grid is open, and re-reads images() when it runs, so the teardown paths
    // (loadImages head, _teardownViewer) still end with an empty grid: by the
    // time the idle callback fires, window.images is [] there.
    if (wasOpen && images().length) prebuild();
    return wasOpen;
  }

  // Crossfade exit: a full-viewport 800px thumbnail covers the viewer until the
  // real page image has actually decoded, so the reader never sees the blank
  // strip. Removed on the real image's load, on its own error, or at
  // XFADE_MAX_MS — whichever comes first.
  // Under prefers-reduced-motion the COVER still goes up — skipping it entirely
  // left the reader staring at the blank strip (a black frame), which is a
  // bigger insult than the fade it was trying to avoid. Only the 200ms opacity
  // transition is dropped: the cover is removed instantly on load/error/timeout.
  function crossfade(gidx) {
    var im = images()[gidx];
    if (!im) return;
    var instant = reducedMotion();
    var stale = document.getElementById('ov-xfade');   // single instance
    if (stale && stale.parentNode) stale.parentNode.removeChild(stale);
    var host = document.getElementById('img-container');
    if (!host) return;
    var x = document.createElement('img');
    x.id = 'ov-xfade'; x.alt = '';
    x.src = window.OfflineCore.thumbUrl(im.folder, im.name, 800);
    // INSIDE #img-container, not under <body>: #img-container has no z-index of
    // its own, so this competes directly with #toolbar (z-index 5, a sibling in
    // #viewer). 3 clears #zoom-layer and leaves the toolbar visible. Appending
    // to <body> instead would put it in the ROOT stacking context, where any
    // z-index >= 2 hides the whole viewer chrome for up to XFADE_MAX_MS.
    x.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;object-fit:contain;'
      + 'z-index:3;pointer-events:none;background:#0d0d0d;'
      + (instant ? '' : 'transition:opacity ' + XFADE_FADE_MS + 'ms');
    host.appendChild(x);
    var gone = false, poll = 0, cap = 0;
    function remove() {
      if (gone) return;
      gone = true;
      if (poll) clearInterval(poll);
      if (cap) clearTimeout(cap);
      if (instant) { if (x.parentNode) x.parentNode.removeChild(x); return; }
      x.style.opacity = '0';
      setTimeout(function () { if (x.parentNode) x.parentNode.removeChild(x); }, XFADE_FADE_MS + 20);
    }
    // The real <img> is created lazily by spread.js after viewerSnapTo, so it
    // may not exist yet — poll instead of attaching a listener up front.
    // Terminal states: decoded (naturalWidth > 0), or FAILED — spread.js marks
    // both outcomes with the `loaded` class, and a broken page must not hold the
    // thumbnail up for the whole XFADE_MAX_MS. `complete` alone is not a signal:
    // it is already true before the fetch task starts (app.js H8).
    var watched = null;
    poll = setInterval(function () {
      var el = (window.stripImgs || {})[gidx];
      if (!el) return;
      if (el !== watched) { watched = el; el.addEventListener('error', remove, { once: true }); }
      if (el.complete && (el.naturalWidth > 0 || el.classList.contains('loaded'))) remove();
    }, 50);
    cap = setTimeout(remove, XFADE_MAX_MS);
    x.addEventListener('error', remove);
  }

  // `typeof … === 'function'`, not a bare truthiness check: a Service Worker
  // StaleWhileRevalidate can hand a browser THIS module next to a cached, older
  // app.js/navigation.js that never published the port or suppressClickFor.
  // Throwing there would leave the grid up with no way out; closing is the
  // honest degradation. (The same guards are forbidden inside app.js — see
  // test_app_js_has_no_module_fallback_bodies — but belong here.)
  function exitTo(gidx) {
    close();
    // The touchend that picked the cell also synthesises a click on whatever
    // is under the finger in the viewer we just uncovered — swallow it.
    if (typeof window.suppressClickFor === 'function') window.suppressClickFor(SUPPRESS_MS);
    var im = images()[gidx];
    if (!im || im.isSpacer) return;
    // Picking a cell is a user movement request — a goto carrying a typed
    // PageRef, never a local index. The default handler performs the snap +
    // crossfade; a comm handler receives the same intent instead.
    if (typeof window.ViewerNavigation !== 'object' || typeof window.ViewerNavigation.request !== 'function') return;
    window.ViewerNavigation.request({type: 'goto', page: {folder: im.folder, name: im.name}, via: 'overview'});
  }

  function relayout() {
    if (!ensureEls() || root.hidden) return;
    applyHeights();
    scrollCurToThird();
  }

  function init() {
    if (!ensureEls()) return;
    root.addEventListener('scroll', function () {
      lastScrollTs = performance.now();
      scheduleSlide();
    }, { passive: true });

    // tap (< 8px, single finger) → jump to the page. A tap landing within
    // INERTIA_MS of a scroll is the reader stopping momentum, not a choice.
    var tp = null;
    root.addEventListener('touchstart', function (e) {
      e.stopPropagation();
      if (e.touches.length !== 1) { tp = null; return; }
      var t = e.touches[0];
      tp = { x: t.clientX, y: t.clientY, id: t.identifier, dead: false };
    }, { passive: true });
    root.addEventListener('touchmove', function (e) {
      e.stopPropagation();
      if (!tp) return;
      if (e.touches.length !== 1) { tp.dead = true; return; }
      var t = e.touches[0];
      if (Math.hypot(t.clientX - tp.x, t.clientY - tp.y) >= 8) tp.dead = true;
    }, { passive: true });
    root.addEventListener('touchend', function (e) {
      e.stopPropagation();
      if (!tp || tp.dead || e.touches.length) { tp = null; return; }
      var pt = tp; tp = null;
      if (!open) return;
      if (performance.now() - lastScrollTs < INERTIA_MS) return;
      var target = document.elementFromPoint(pt.x, pt.y);
      var cell = target && target.closest ? target.closest('.ov-cell') : null;
      if (cell) exitTo(+cell.dataset.gidx);
    }, { passive: true });
    root.addEventListener('touchcancel', function (e) { e.stopPropagation(); tp = null; }, { passive: true });

    // Mouse / keyboard activation. The synthetic click Chromium emits after a
    // touch tap lands here too, but `open` is already false by then.
    root.addEventListener('click', function (e) {
      if (!open) return;
      var cell = e.target.closest ? e.target.closest('.ov-cell') : null;
      if (cell && performance.now() - lastScrollTs >= INERTIA_MS) exitTo(+cell.dataset.gidx);
    });

    if (closeBtn) closeBtn.addEventListener('click', function () { close(); });

    document.addEventListener('keydown', function (e) {
      // Escape is checked BEFORE the INPUT guard: #page-slider is an <input>
      // and may hold focus, and Esc must still dismiss the overview from there.
      if (e.key === 'Escape' && open) {
        // app.js's Escape handler closes the sidebar; it must not also run.
        e.stopImmediatePropagation();
        e.preventDefault();
        close();
        return;
      }
      var tag = e.target && e.target.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA') return;   // G/F are typed characters
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === 'g' || e.key === 'G') {
        if (open) close();
        else if (images().length) show();
        return;
      }
      // F is the keyboard twin of #menu-btn and gets the same pre-close: the
      // sidebar is fixed at z-index 10 on <body>, so it would slide OVER the
      // grid, and the #viewer width change would then yank the grid's scroll
      // position through our ResizeObserver.
      if ((e.key === 'f' || e.key === 'F') && open) close();
    });

    // Open only, never toggle: `body.overview-open` puts the whole toolbar at
    // opacity 0 / pointer-events none, so the button is unreachable while the
    // grid is up and a `close()` branch here could never run. G and the ✕
    // button are the documented ways out; the title says so.
    var btn = q('#overview-btn');
    if (btn) btn.addEventListener('click', function () { show(); });
    // Capture: the sidebar toggle behind it still runs, but the overview is
    // gone by the time it does.
    var menu = q('#menu-btn');
    if (menu) menu.addEventListener('click', function () { if (open) close(); }, true);

    // Pinch-out inside the grid = leave the overview at the cell under the
    // fingers. Attached to `root`, so the ENTRY pinch cannot reach it: touch
    // targets are fixed at touchstart, and that gesture started on
    // #img-container, which keeps receiving it however wide it is spread.
    window.ViewerGesture.attach(root, {
      onStart: function () { lastMid = null; lastRatio = 1; },
      onMove: function (g) { lastMid = g.mid; lastRatio = g.ratio; },
      onEnd: function (g) {
        var ratio = (g && typeof g.ratio === 'number') ? g.ratio : lastRatio;
        if (!open || !(ratio > PINCH_OUT)) return;
        var target = lastMid ? document.elementFromPoint(lastMid.x, lastMid.y) : null;
        var cell = target && target.closest ? target.closest('.ov-cell') : null;
        exitTo(cell ? +cell.dataset.gidx : (window.currentIdx || 0));
      }
    });

    // zoom.js is the state machine; these two callbacks are everything it
    // knows about the overview.
    window.Zoom.onOverviewFire = function () {
      if (open || !images().length) return;
      try { if ('vibrate' in navigator && !reducedMotion()) navigator.vibrate(10); } catch (e) { console.warn(e); }
      show({ fly: true });
    };
    // VIEWPORT coordinates: zoom.js converts to its container-local frame with
    // the #img-container rect it captured at gesture start.
    window.Zoom.driftTarget = function () { return curCenter; };

    var viewer = q('#viewer');
    if (viewer && window.ResizeObserver) {
      ro = new ResizeObserver(function () { relayout(); });
      ro.observe(viewer);
    }
  }

  window.Overview = {
    open: show,
    close: close,
    isOpen: function () { return open; },
    prebuild: prebuild,
    markCurrent: markCurrent,
    syncPosition: syncPosition,
    relayout: relayout,
    // Exported for app.js's default goto handler: the overview exit pairs
    // its page snap with this thumbnail crossfade.
    crossfade: crossfade,
    cellCenter: function () { return curCenter; },
    _finishFlight: null,
    _debug: {
      imgWindow: function () { return win ? { lo: win.lo, hi: win.hi } : null; },
      lastScrollTs: function () { return lastScrollTs; },
      flightActive: function () { return !!window.Overview._finishFlight; }
    }
  };
  // Test-mode only: the inertia guard is `now - lastScrollTs < INERTIA_MS`, and
  // a test that wants to be INSIDE the guard should say so rather than race a
  // real 100ms window. A far-future stamp is unconditionally "still scrolling".
  if (window.__BOOKQ_TEST_MODE__ === 'true') {
    window.Overview._debug.setLastScrollTs = function (ts) { lastScrollTs = ts; };
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
