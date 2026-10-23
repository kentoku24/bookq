/* bookq viewer/ring — experimental three-spread ring renderer (?reader=ring).
 * Plain global: window.RingReader.create(config).
 *
 * Three permanent full-viewport .ring-slot roots live inside the shared
 * #img-track. Each holds one .spread-container with two persistent page cells
 * (≤6 mounted images total). Slots are bound to the s-1/s/s+1 spread window;
 * an adjacent turn rebinds only the outgoing offscreen slot — the current and
 * incoming spreads keep their DOM and <img> identity. Positioning uses a
 * fixed viewport stride ((anchor - s) * viewW), not the strip's variable
 * widths. Prototype limitation: no chapter-divider labels on the strip
 * (the toolbar chapter name/counter still works).
 */
(function () {
  var SLOT_COUNT = 3;
  var GAP_INNER = 5;           // matches #img-track .spread-container gap

  function create(config) {
    var track = config.track;
    var cache = window.ReaderImageCache.create({
      urlFor: config.urlFor,
      onAspect: onAspect,
      onChange: function () {},
      maxEntries: config.maxEntries,
      maxBytes: config.maxBytes,
    });
    var slots = null;          // [{root, box, cells:[c0,c1], spread, pages}]
    var curSpread = -1;        // spread the last ensure target belongs to (priority)
    var boundCenter = -1;      // spread the mounted window is bound around
    var boundModel = null;     // identity of the spreads array at bind time
    var layoutDirty = false;
    var queuedPrefetch = null;
    var dragRaf = 0;
    var pendingDrag = null;

    function onAspect(idx, img) {
      var changed = config.onAspect(idx, img);
      if (!changed) return;
      // A re-measured page resizes its spread. Geometry writes mid-drag or
      // mid-slide would move a live animation's rest position — defer them.
      if (config.isMoving()) layoutDirty = true;
      else layout();
    }

    function setStyle(el, prop, value) {
      if (el.style[prop] !== value) el.style[prop] = value;
    }

    function ensureSlots() {
      if (slots) return;
      slots = [];
      for (var i = 0; i < SLOT_COUNT; i++) {
        var root = document.createElement("div");
        root.className = "ring-slot";
        var box = document.createElement("div");
        box.className = "spread-container";
        box.style.gap = GAP_INNER + "px";
        var cells = [document.createElement("div"), document.createElement("div")];
        cells[0].className = "page-cell";
        cells[1].className = "page-cell";
        cells[1].style.display = "none";
        box.appendChild(cells[0]);
        box.appendChild(cells[1]);
        root.appendChild(box);
        root.style.display = "none";
        track.appendChild(root);
        slots.push({ root: root, box: box, cells: cells, spread: -1, pages: null });
      }
    }
    function slotFor(s) {
      if (!slots) return null;
      for (var sl of slots) if (sl.spread === s) return sl;
      return null;
    }
    function freeSlot() {
      for (var sl of slots) if (sl.spread < 0) return sl;
      return null;
    }
    function unbind(sl) {
      sl.spread = -1;
      sl.pages = null;
      for (var c of sl.cells) while (c.firstChild) c.removeChild(c.firstChild);
      sl.root.style.display = "none";
    }
    function mountPages(sl, pages) {
      // The spread the reader is looking at fetches high; mounted neighbours
      // stay low — promotion happens only when a spread becomes current.
      var pri = sl.spread === curSpread ? "high" : "low";
      for (var i = 0; i < 2; i++) {
        var cell = sl.cells[i];
        if (i < pages.length) {
          var img = cache.ensure(pages[i], pri);
          if (img && cell.firstChild !== img) {
            while (cell.firstChild) cell.removeChild(cell.firstChild);
            cell.appendChild(img);
          }
        } else {
          while (cell.firstChild) cell.removeChild(cell.firstChild);
        }
      }
      sl.pages = pages;
    }
    // Position one bound slot. Reads only config getters — no layout reads.
    function positionSlot(sl) {
      var s = sl.spread;
      var spreads = config.getSpreads();
      if (s < 0 || !spreads[s]) { unbind(sl); return; }
      var view = config.getView();
      var anchor = config.getAnchor();
      var m = config.getMetrics(s);
      setStyle(sl.root, "display", "");
      setStyle(sl.root, "left", (anchor - s) * view.width + "px");
      setStyle(sl.box, "width", m.width + "px");
      setStyle(sl.box, "height", m.height + "px");
      setStyle(sl.box, "left", (view.width - m.width) / 2 + "px");
      if (sl.pages !== spreads[s].pages) mountPages(sl, spreads[s].pages);
      for (var i = 0; i < 2; i++) {
        var cell = sl.cells[i];
        if (i < sl.pages.length) {
          setStyle(cell, "display", "");
          setStyle(cell, "width", m.pageWidths[i] + "px");
          setStyle(cell, "height", m.height + "px");
        } else {
          setStyle(cell, "display", "none");
        }
      }
    }
    function layout() {
      if (config.isMoving()) { layoutDirty = true; return; }
      layoutDirty = false;
      if (slots) for (var sl of slots) positionSlot(sl);
      flushPrefetch();
    }

    function ensureWindow(idx) {
      var spreads = config.getSpreads();
      if (!spreads.length) return;
      var s = config.spreadOf(idx);
      // Fast path: already bound around this spread on the same model — a
      // repeat call (deferred window job, second layout pass) must not
      // re-pin, re-ensure, re-tune or re-publish. boundCenter is deliberately
      // NOT curSpread: ensureImage moves curSpread with the requested target
      // without meaning the window moved.
      if (slots && s === boundCenter && spreads === boundModel) return;
      ensureSlots();
      curSpread = s;
      var want = [], wantSet = {}, pinIdx = [];
      for (var d = -1; d <= 1; d++) {
        var t = s + d;
        if (t >= 0 && t < spreads.length) { want.push(t); wantSet[t] = true; }
      }
      // Pin the whole desired window BEFORE allocating any cache entry —
      // a hard-budget trim must never evict the freshly requested target
      // behind entries from the old window.
      for (var w of want) for (var p of spreads[w].pages) pinIdx.push(p);
      cache.pin(pinIdx);
      for (var sl of slots) if (sl.spread >= 0 && !wantSet[sl.spread]) unbind(sl);
      var moving = config.isMoving();
      // Bind in fetch order — current spread first so its images start
      // high-priority ahead of the neighbours (next, then previous).
      for (var t2 of [s, s + 1, s - 1]) {
        if (!wantSet[t2]) continue;
        var sl2 = slotFor(t2);
        if (!sl2) {
          sl2 = freeSlot();
          if (!sl2) continue;
          sl2.spread = t2;
          // A freshly bound slot is always offscreen — position it eagerly so
          // it can never peek into view at a stale offset mid-animation.
          positionSlot(sl2);
        } else if (!moving || sl2.pages !== spreads[t2].pages) {
          positionSlot(sl2);
        }
      }
      // Retune priorities on already-mounted pages — mountPages only runs
      // when the page set changed, so promotion/demotion (a neighbour
      // becoming current, or vice versa) needs its own pass. ensure() on an
      // existing entry mutates fetchPriority only: no remount, no refetch.
      for (var t3 of want) {
        var pri3 = t3 === s ? "high" : "low";
        for (var p3 of spreads[t3].pages) cache.ensure(p3, pri3);
      }
      publish();
      if (!moving) flushPrefetch();
      boundCenter = s;
      boundModel = spreads;
    }

    function ensureImage(idx) {
      var spreads = config.getSpreads();
      if (!spreads.length) return null;
      var s = config.spreadOf(idx);
      curSpread = s;             // ensureImage is always the requested target
      var mounted = !!(spreads[s] && spreads[s].pages.indexOf(idx) >= 0);
      var img = cache.ensure(idx, "high");
      var reparented = false;
      if (mounted && img) {
        var sl = slotFor(s);
        if (sl) {
          var cell = sl.cells[spreads[s].pages.indexOf(idx)];
          if (cell.firstChild !== img) {
            while (cell.firstChild) cell.removeChild(cell.firstChild);
            cell.appendChild(img);
            reparented = true;
          }
          cell.style.display = "";
        }
      }
      // The mounted maps only change when a page actually moved cells —
      // republishing the same bindings is pure churn on the ack path.
      if (reparented) publish();
      return img;
    }

    // Warm spreads two away from idx, through the bounded cache — never
    // mounted. Queued while moving so a far fetch can't steal the frame a
    // turn is being acknowledged on; layout() flushes the queue at rest.
    function prefetch(idx) {
      if (config.isMoving()) { queuedPrefetch = idx; return; }
      queuedPrefetch = null;
      var spreads = config.getSpreads();
      if (!spreads.length) return;
      var s = config.spreadOf(idx);
      for (var t of [s + 2, s - 2]) {
        if (t < 0 || t >= spreads.length) continue;
        // Admission-gated: a far fetch the budget would evict on arrival is
        // skipped entirely — no allocate/decode/evict churn. Entries that
        // already exist pass through (touch + priority only).
        for (var p of spreads[t].pages) {
          if (cache.canPrefetch(p)) cache.ensure(p, "low");
        }
      }
      cache.trim();
    }
    function flushPrefetch() {
      if (queuedPrefetch === null) return;
      var idx = queuedPrefetch;
      queuedPrefetch = null;
      prefetch(idx);
    }

    // Mounted state only — detached cache images must not leak into the
    // stripImgs/stripCells/spreadEls maps shared with the legacy code paths.
    function publish() {
      var imgs = {}, cells = {}, boxes = {};
      var spreads = config.getSpreads();
      for (var sl of slots || []) {
        var s = sl.spread;
        if (s < 0 || !spreads[s]) continue;
        boxes[s] = sl.box;
        var pages = spreads[s].pages;
        for (var i = 0; i < pages.length; i++) {
          cells[pages[i]] = sl.cells[i];
          if (sl.cells[i].firstChild) imgs[pages[i]] = sl.cells[i].firstChild;
        }
      }
      config.publish({ imgs: imgs, cells: cells, spreads: boxes });
    }

    function applyTransform(x) {
      track.style.transition = "none";
      track.style.transform = "translateX(" + x + "px)";
    }
    function drag(offset) {
      pendingDrag = offset;
      if (dragRaf) return;
      dragRaf = requestAnimationFrame(function () {
        dragRaf = 0;
        if (pendingDrag === null) return;
        var v = pendingDrag;
        pendingDrag = null;
        applyTransform(v);
      });
    }
    function flushDrag() {
      if (dragRaf) { cancelAnimationFrame(dragRaf); dragRaf = 0; }
      if (pendingDrag !== null) { var v = pendingDrag; pendingDrag = null; applyTransform(v); }
    }
    function cancelDrag() {
      if (dragRaf) { cancelAnimationFrame(dragRaf); dragRaf = 0; }
      pendingDrag = null;
    }
    function trim() { cache.trim(); }
    function reset() {
      cancelDrag();
      queuedPrefetch = null;
      layoutDirty = false;
      curSpread = -1;
      boundCenter = -1;
      boundModel = null;
      cache.clear();
      if (slots) {
        for (var sl of slots) sl.root.remove();
        slots = null;
      }
      publish();   // empty maps: the legacy pool-release loop sees no cache imgs
    }
    function debug() {
      var mounted = [], mountedImages = 0;
      for (var sl of slots || []) {
        if (sl.spread < 0) continue;
        mounted.push(sl.spread);
        for (var c of sl.cells) if (c.firstChild) mountedImages++;
      }
      mounted.sort(function (a, b) { return a - b; });
      return {
        slotCount: slots ? slots.length : 0,
        spreads: mounted,
        mountedImages: mountedImages,
        layoutDirty: layoutDirty,
        cache: cache.debug(),
      };
    }

    return {
      ensureImage: ensureImage, ensureWindow: ensureWindow, layout: layout,
      prefetch: prefetch, trim: trim, drag: drag, flushDrag: flushDrag,
      cancelDrag: cancelDrag, reset: reset, debug: debug,
    };
  }
  window.RingReader = { create: create };
})();
