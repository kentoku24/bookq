/* bookq viewer/ringCache — bounded image+decode cache for the experimental
 * ring reader (?reader=ring). Plain global: window.ReaderImageCache.create().
 *
 * Entries are keyed by GLOBAL image index and own their <img> element so a
 * mounted page can be re-bound between slots without a second fetch/decode.
 * Byte accounting is an ESTIMATE (naturalWidth*naturalHeight*4): an LRU
 * budget on estimated bytes — the pinned window may exceed it, and it is not
 * an actual decoder/GPU memory cap.
 */
(function () {
  function create(opts) {
    var urlFor = opts.urlFor;
    var onAspect = opts.onAspect || function () {};
    var onChange = opts.onChange || function () {};
    var maxEntries = opts.maxEntries || 12;
    var maxBytes = opts.maxBytes || 64 * 1024 * 1024;

    var entries = new Map();   // idx -> entry; Map order doubles as LRU order
    var pinned = new Set();    // indices the host wants kept (set before ensure)
    var epoch = 0;             // bumped by clear(): kills pending callbacks

    function byteTotal() {
      var t = 0;
      for (var e of entries.values()) t += e.bytes;
      return t;
    }
    function pinnedBytes() {
      var t = 0;
      for (var e of entries.values()) if (e.pinned) t += e.bytes;
      return t;
    }
    function release(entry) {
      // Detach + abort the fetch. Listeners stay on the node — their
      // epoch/identity guard turns every late completion into a no-op.
      var img = entry.img;
      try { img.removeAttribute("src"); } catch (_) {}
      if (img.parentNode) img.parentNode.removeChild(img);
      img.classList.remove("loaded");
      entries.delete(entry.idx);
    }
    function trim() {
      // Evict oldest-first (Map insertion order = LRU) until both budgets
      // hold. Pinned entries are NEVER evicted — even when pinned bytes alone
      // exceed maxBytes — so an oversized window can never thrash.
      for (var kv of entries) {
        if (entries.size <= maxEntries && byteTotal() <= maxBytes) break;
        if (kv[1].pinned) continue;
        release(kv[1]);
      }
    }
    function touch(entry) {
      entries.delete(entry.idx);
      entries.set(entry.idx, entry);
    }

    function ensure(idx, priority) {
      var e = entries.get(idx);
      if (e) {
        // Only an explicit priority retunes the request — an absent one must
        // never downgrade an entry that is already fetching high. And only
        // write the attribute when it actually changes.
        if ((priority === "high" || priority === "low") && e.img.fetchPriority !== priority) e.img.fetchPriority = priority;
        touch(e);
        return e.img;
      }
      var url = urlFor(idx);
      if (!url) return null;   // chapter-boundary spacers have no URL
      var img = new Image();
      img.decoding = "async";
      img.draggable = false;
      img.alt = "";
      img.style.setProperty("--i", String(idx));
      img.fetchPriority = priority === "high" ? "high" : "low";
      e = { idx: idx, img: img, bytes: 0, pinned: pinned.has(idx), ready: false, decoded: false, failed: false };
      entries.set(idx, e);     // in-flight entries count toward the entry cap
      var gen = epoch;
      img.addEventListener("load", function () {
        if (gen !== epoch || entries.get(idx) !== e) return;  // stale book/entry
        e.bytes = (img.naturalWidth || 0) * (img.naturalHeight || 0) * 4;
        e.ready = true;
        img.classList.add("loaded");
        onAspect(idx, img);
        // Warm the decode off the critical turn path. decode() also awaits an
        // in-flight fetch, so this schedules work rather than duplicating it;
        // a rejection (e.g. evicted mid-decode) is not a load failure. No
        // decode() API → decoded stays false ("unknown", not "decoded").
        if (typeof img.decode === "function") {
          try {
            var p = img.decode();
            if (p && p.then) p.then(function () {
              if (gen !== epoch || entries.get(idx) !== e) return;
              e.decoded = true;
            }, function () { /* decode rejection is not a network error */ });
          } catch (_) {}
        }
        trim();
        onChange(idx);
      });
      img.addEventListener("error", function () {
        if (gen !== epoch || entries.get(idx) !== e) return;
        // Same terminal-state convention as ensureStripImage: `loaded` marks
        // the failure so the app's current-page path (spinner off + ⚠) works.
        // Readiness stays honest though — a failed entry is neither ready
        // nor decoded; `failed` carries the network outcome.
        img.classList.add("loaded");
        e.failed = true;
        onChange(idx);
      });
      img.src = url;
      trim();
      return img;
    }

    // Declare the keep-set for the next ensure() calls. Entries outside the
    // set become unpinned and LRU-evictable immediately; not-yet-allocated
    // indices are born pinned.
    function pin(indices) {
      pinned = new Set(indices);
      for (var e of entries.values()) e.pinned = pinned.has(e.idx);
      trim();
    }

    function clear() {
      epoch++;                 // invalidate every outstanding completion
      for (var e of entries.values()) {
        try { e.img.removeAttribute("src"); } catch (_) {}
        if (e.img.parentNode) e.img.parentNode.removeChild(e.img);
      }
      entries.clear();
      pinned = new Set();
    }

    function debug() {
      var ready = [], decoded = [], failed = [];
      for (var e of entries.values()) {
        if (e.ready) ready.push(e.idx);
        if (e.decoded) decoded.push(e.idx);
        if (e.failed) failed.push(e.idx);
      }
      return {
        entries: entries.size,
        indices: Array.from(entries.keys()),
        bytes: byteTotal(),
        pinnedBytes: pinnedBytes(),
        maxEntries: maxEntries,
        maxBytes: maxBytes,
        ready: ready,
        decoded: decoded,
        failed: failed,
      };
    }

    // Admission estimate for a NEW far-prefetch entry — not a fetch decision.
    // Free when the entry already exists; refuse while any pinned page is
    // still cold (no honest byte baseline yet), when the pin set alone fills
    // the entry cap, or when pinned bytes + the largest known entry would
    // exceed the budget (the new entry would be trimmed back immediately).
    // trim() stays authoritative — this only avoids allocate→evict churn.
    function canPrefetch(idx) {
      if (entries.has(idx)) return true;
      if (pinned.size >= maxEntries) return false;
      for (var p of pinned) {
        var e = entries.get(p);
        if (!e || !e.ready) return false;
      }
      var largest = 0;
      for (var e2 of entries.values()) {
        if (e2.ready && e2.bytes > largest) largest = e2.bytes;
      }
      if (largest <= 0) return false;
      return pinnedBytes() + largest <= maxBytes;
    }

    return { pin: pin, ensure: ensure, canPrefetch: canPrefetch, trim: trim, clear: clear, debug: debug };
  }
  window.ReaderImageCache = { create: create };
})();
