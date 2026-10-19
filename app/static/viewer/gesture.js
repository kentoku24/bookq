/* bookq viewer/gesture — pinch recognizer + native-zoom guards.
 * Plain global script. Loaded after viewer/spread.js, before viewer/zoom.js.
 * Spec: docs/superpowers/specs/2026-09-21-viewer-zoom-overview-design.md §5.1
 *
 * Touch classification: a touch that starts within PALM_EDGE px of any screen
 * edge is a "palm" until it moves ARM_PX; activeTouches() hides palms so a
 * thumb resting on the bezel never kills the 1-finger swipe (app.js uses this
 * for its own count checks) and never counts as a pinch finger.
 * A pinch arms only when two active touches have BOTH moved ARM_PX from
 * their start — then onStart fires once; onMove per 2-finger move; onEnd when
 * fewer than two active touches remain (remaining = the one still down).
 */
(function () {
  'use strict';
  var PALM_EDGE = 20, ARM_PX = 10;

  var starts = {};   // identifier -> {x, y, palm}
  function noteStart(t) {
    var w = window.innerWidth, h = window.innerHeight;
    var palm = t.clientX < PALM_EDGE || t.clientY < PALM_EDGE || t.clientX > w - PALM_EDGE || t.clientY > h - PALM_EDGE;
    starts[t.identifier] = { x: t.clientX, y: t.clientY, palm: palm };
  }
  function noteMove(t) {
    var s = starts[t.identifier]; if (!s) { noteStart(t); s = starts[t.identifier]; }
    if (s.palm && Math.hypot(t.clientX - s.x, t.clientY - s.y) >= ARM_PX) s.palm = false;
  }
  function forget(t) { delete starts[t.identifier]; }
  function movedEnough(t) {
    var s = starts[t.identifier]; if (!s) return false;
    return Math.hypot(t.clientX - s.x, t.clientY - s.y) >= ARM_PX;
  }
  function activeTouches(e) {
    var out = [];
    for (var i = 0; i < e.touches.length; i++) {
      var t = e.touches[i];
      if (!starts[t.identifier]) noteStart(t);
      if (!starts[t.identifier].palm) out.push(t);
    }
    return out;
  }
  // Document-level bookkeeping so activeTouches() is correct for ANY listener,
  // including app.js handlers registered on #img-container.
  document.addEventListener('touchstart', function (e) { for (var i = 0; i < e.changedTouches.length; i++) noteStart(e.changedTouches[i]); }, { capture: true, passive: true });
  document.addEventListener('touchmove', function (e) { for (var i = 0; i < e.changedTouches.length; i++) noteMove(e.changedTouches[i]); }, { capture: true, passive: true });
  document.addEventListener('touchend', function (e) { for (var i = 0; i < e.changedTouches.length; i++) forget(e.changedTouches[i]); }, { capture: true, passive: true });
  document.addEventListener('touchcancel', function (e) { for (var i = 0; i < e.changedTouches.length; i++) forget(e.changedTouches[i]); }, { capture: true, passive: true });

  // Where a still-down touch landed (viewport px), or null once it is released.
  // app.js arms a late swipe from here when a lone edge-started touch stops
  // being a palm, so the 10px of promotion travel is not charged to the reader.
  function startOf(identifier) {
    var s = starts[identifier];
    return s ? { x: s.x, y: s.y } : null;
  }
  // Shared helpers — one copy for app.js, zoom.js and overview.js.
  function findTouch(list, id) { for (var i = 0; i < list.length; i++) if (list[i].identifier === id) return list[i]; return null; }
  function reducedMotion() { try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) { return false; } }

  function dist(a, b) { return Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) || 0.0001; }
  function mid(a, b) { return { x: (a.clientX + b.clientX) / 2, y: (a.clientY + b.clientY) / 2 }; }

  var pinching = false;
  function attach(el, h) {
    var armed = false, dist0 = 0, lastRatio = 1;
    function reset() { armed = false; dist0 = 0; lastRatio = 1; pinching = false; }
    // arming happens on move
    el.addEventListener('touchmove', function (e) {
      var act = activeTouches(e);
      if (act.length < 2) {
        if (armed) {
          var rem = act.length === 1 ? act[0] : null;
          h.onEnd({ ratio: lastRatio, remaining: rem });
          reset();
        }
        return;
      }
      var a = act[0], b = act[1];
      if (!armed) {
        if (!(movedEnough(a) && movedEnough(b))) return;
        armed = true; pinching = true;
        // dist0/mid0 are measured from where the fingers LANDED, not from the
        // frame that crossed ARM_PX. Arming costs 10px of travel per finger;
        // charging that to the user would make the first ~25% of a pinch
        // invisible and the whole gesture feel like it starts late.
        var sa = starts[a.identifier], sb = starts[b.identifier];
        var p0 = sa && sb ? { ax: sa.x, ay: sa.y, bx: sb.x, by: sb.y }
                          : { ax: a.clientX, ay: a.clientY, bx: b.clientX, by: b.clientY };
        dist0 = Math.hypot(p0.ax - p0.bx, p0.ay - p0.by) || 0.0001;
        lastRatio = dist(a, b) / dist0;
        h.onStart({ dist0: dist0, mid0: { x: (p0.ax + p0.bx) / 2, y: (p0.ay + p0.by) / 2 }, touches: [a, b] });
        return;
      }
      lastRatio = dist(a, b) / dist0;
      h.onMove({ ratio: lastRatio, mid: mid(a, b), touches: [a, b] });
    }, { passive: true });
    function end(e) {
      if (!armed) return;
      var act = activeTouches(e);
      if (act.length >= 2) return;
      var rem = act.length === 1 ? act[0] : null;
      h.onEnd({ ratio: lastRatio, remaining: rem });
      reset();
    }
    el.addEventListener('touchend', end, { passive: true });
    el.addEventListener('touchcancel', end, { passive: true });
  }

  // Native-zoom suppression (spec §5.1 items 2-4). All gated on the viewer
  // subtree and on an open book so the browse UI keeps accessibility zoom.
  //
  // These guards deliberately read RAW e.touches.length, not activeTouches():
  // Safari can start a native pinch-zoom / tab-overview gesture from raw
  // contacts even when one of them is a resting palm, so gating on active
  // (non-palm) touches would let native zoom leak through. preventDefault()
  // here has no default action to cancel on #img-container (touch-action:
  // none), so this doesn't affect the 1-finger swipe.
  var nativeGuardsInstalled = false;
  function installNativeGuards(viewerEl, isActive) {
    if (nativeGuardsInstalled) return;
    nativeGuardsInstalled = true;
    function inViewer(e) { return !!(viewerEl && e.target && viewerEl.contains(e.target)) && isActive(); }
    ['gesturestart', 'gesturechange', 'gestureend'].forEach(function (t) {
      viewerEl.addEventListener(t, function (e) { if (inViewer(e) && e.cancelable) e.preventDefault(); }, { passive: false, capture: true });
    });
    // Capture phase on `viewerEl`, like the other guards: overview.js calls
    // stopPropagation() on every touch event at #overview, so a bubble-phase
    // listener on #viewer never sees a pinch inside the grid — and in Safari
    // that pinch then zooms the page natively (pinch-in opens the tab
    // switcher). A natively zoomed page pans on horizontal swipe, which is
    // what "the left column is cut off in the overview" looks like.
    // The listeners must NOT be on `document`: a non-passive touch listener
    // anywhere above the scroller makes every browse-grid drag block on the
    // main thread (real-finger scroll jank on low-end Android; fps ~3 on the
    // CP08-J1). #sidebar/#tile-grid live outside #viewer so scoping the
    // guards here leaves tile scrolling fully compositor-driven.
    viewerEl.addEventListener('touchmove', function (e) {
      if (e.touches.length >= 2 && inViewer(e) && e.cancelable) e.preventDefault();
    }, { passive: false, capture: true });
    viewerEl.addEventListener('touchstart', function (e) {
      if (e.touches.length >= 2 && inViewer(e) && e.cancelable) e.preventDefault();
    }, { passive: false, capture: true });
  }

  window.ViewerGesture = {
    activeTouches: activeTouches, attach: attach, installNativeGuards: installNativeGuards,
    startOf: startOf, findTouch: findTouch, reducedMotion: reducedMotion,
    isPinching: function () { return pinching; },
    _debug: { palmIds: function () { return Object.keys(starts).filter(function (k) { return starts[k].palm; }).map(Number); } }
  };
  var viewer = document.getElementById('viewer');
  if (viewer) installNativeGuards(viewer, function () { return !!(window.images && window.images.length); });
})();
