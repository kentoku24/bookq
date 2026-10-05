/* bookq viewer/zoom — absolute zoom state machine on #zoom-layer.
 * Spec §5.2. Never touches #img-track's transition/transform.
 * target = startZoom * ratio. Bands (absolute): >1 zoomed (midpoint anchor),
 * 0.5<t<1 shrink (scale follows, position drifts to the overview cell),
 * <=0.5 fire overview once (re-arm above 0.7 while fingers stay down).
 *
 * Coordinate frame: #zoom-layer is inset:0 inside #img-container with
 * transform-origin 0 0, so its transform lives in CONTAINER-LOCAL pixels.
 * ViewerGesture hands us clientX/Y (viewport), and #img-container is NOT at
 * the viewport origin (#viewer.sidebar-open sets left:280px), so every
 * midpoint is converted with the container rect captured at gesture start.
 */
(function () {
  'use strict';
  var K = { MIN: 0.35, MAX: 4, FIRE: 0.5, REARM: 0.7, SNAP: 1.15, BOUNCE_MS: 190, SLIDE_MS: 300, EASE: 'cubic-bezier(0.25,0.1,0.25,1.0)' };
  var layer = null, zoom = 1, tx = 0, ty = 0, band = 'rest';
  var startZoom = 1, startTx = 0, startTy = 0, mid0 = null, fired = false, bounceTimer = 0;
  var origin = { left: 0, top: 0 };   // container rect at gesture start (viewport → local)

  function el() { return layer || (layer = document.getElementById('zoom-layer')); }
  function apply(s, x, y) {
    zoom = s; tx = x; ty = y;
    band = s > 1 ? 'zoomed' : (s < 1 ? 'shrink' : 'rest');
    var L = el(); if (!L) return;
    L.style.transform = (s === 1 && x === 0 && y === 0) ? '' : 'translate(' + x + 'px,' + y + 'px) scale(' + s + ')';
    document.body.classList.toggle('zoomed', s > 1);
  }
  function killTransition() { var L = el(); if (L) L.style.transition = ''; if (bounceTimer) { clearTimeout(bounceTimer); bounceTimer = 0; } }
  // gesture.js is a hard dependency loaded immediately before this file (the
  // defer order is a contract, mirrored in sw.js SHELL_ASSETS), so these are
  // never absent here.
  var reducedMotion = window.ViewerGesture.reducedMotion;
  var findTouch = window.ViewerGesture.findTouch;
  // A settled layer carries `transition: none` from the stylesheet; setting the
  // transition and the transform in the same tick then starts no transition at
  // all (same hazard applyTrackTransform works around in app.js).
  function reflow(L) { try { void L.offsetWidth; } catch (e) { console.warn(e); } }
  function localRect() {
    var c = document.getElementById('img-container');
    if (c) { var r = c.getBoundingClientRect(); return { left: r.left, top: r.top }; }
    return { left: 0, top: 0 };
  }

  function reset(animate) {
    killTransition();
    pan = null;
    var L = el();
    if (animate && !reducedMotion() && L && zoom !== 1) {
      L.style.transition = 'transform ' + K.SLIDE_MS + 'ms ' + K.EASE;
      reflow(L);
      apply(1, 0, 0);
      bounceTimer = setTimeout(function () { bounceTimer = 0; if (L) L.style.transition = ''; }, K.SLIDE_MS + 50);
    } else {
      apply(1, 0, 0);
    }
  }
  function bounceBack() {
    var L = el();
    if (!L || reducedMotion()) { apply(1, 0, 0); return; }
    L.style.transition = 'transform ' + K.BOUNCE_MS + 'ms ease';
    reflow(L);
    apply(1, 0, 0);
    bounceTimer = setTimeout(function () { bounceTimer = 0; L.style.transition = ''; }, K.BOUNCE_MS + 70);
  }
  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

  // ── 1-finger pan while zoomed (spec §5.2) ──
  var EDGE_PX = 80, RUBBER = 0.35;
  var pan = null;
  // The current spread's resting box, in the layer's UNSCALED local frame,
  // plus the container's size. Measured at gesture start only — a touchmove
  // must never hit the layout engine. Deriving it from a real client rect
  // (instead of re-deriving spread.js's placement rules: portrait centres
  // vertically, landscape bottom-aligns, horizontal follows spreadLeft) keeps
  // this correct for every layout without duplicating that arithmetic.
  var box = null;
  function measureBox() {
    var c = document.getElementById('img-container');
    var cr = c ? c.getBoundingClientRect() : null;
    var W = cr ? cr.width : (window.viewW || window.innerWidth);
    var H = cr ? cr.height : (window.viewH || window.innerHeight);
    var els = window.spreadEls || {};
    // spreadEls/anchorSpread are unconditional window accessors in app.js.
    // No querySelector fallback on purpose: the first .spread-container in DOM
    // order is a NEIGHBOUR of the anchor spread whenever the strip window
    // starts before the current page, and clamping to that box is worse than
    // falling through to the whole-container box below.
    var sc = els[window.anchorSpread || 0];
    if (!sc || !cr || !zoom) { box = { x: 0, y: 0, w: W, h: H, W: W, H: H }; return box; }
    var r = sc.getBoundingClientRect();
    if (!r.width || !r.height) { box = { x: 0, y: 0, w: W, h: H, W: W, H: H }; return box; }
    box = {
      x: (r.left - cr.left - tx) / zoom, y: (r.top - cr.top - ty) / zoom,
      w: r.width / zoom, h: r.height / zoom, W: W, H: H
    };
    return box;
  }
  // Keep the scaled spread covering the viewport. An axis that cannot cover it
  // (spread smaller than the viewport at this scale) is centred instead — that
  // is also what "no room" means for the edge turn.
  function clampPan(nx, ny, s) {
    var b = box || measureBox();
    s = s || zoom;
    var sw = b.w * s, sh = b.h * s;
    var out = { x: nx, y: ny, roomX: true, roomY: true };
    if (sw <= b.W) { out.x = (b.W - sw) / 2 - b.x * s; out.roomX = false; }
    else out.x = clamp(nx, b.W - (b.x * s + sw), -b.x * s);
    if (sh <= b.H) { out.y = (b.H - sh) / 2 - b.y * s; out.roomY = false; }
    else out.y = clamp(ny, b.H - (b.y * s + sh), -b.y * s);
    return out;
  }
  // -1: no room left toward +x (the spread's left edge is already at the
  //     viewport's left) → a finger moving RIGHT turns to the NEXT page.
  // +1: no room toward -x → a finger moving LEFT turns to the PREVIOUS page.
  //  2: the axis is centred (spread narrower than the viewport) → both count.
  //  0: room both ways → this drag can never turn a page, however far it runs.
  function noRoomDirAtStart() {
    var b = box || measureBox(), s = zoom, sw = b.w * s;
    if (sw <= b.W) return 2;
    var maxX = -b.x * s, minX = b.W - (b.x * s + sw), eps = 0.5;
    if (tx >= maxX - eps) return -1;
    if (tx <= minX + eps) return 1;
    return 0;
  }
  function panStartFrom(t) {
    if (!t || zoom <= 1) { pan = null; return; }
    measureBox();
    pan = { id: t.identifier, x0: t.clientX, y0: t.clientY, tx0: tx, ty0: ty,
            noRoom: noRoomDirAtStart(), decided: false, horizontal: false,
            vibrated: false, armed: false, armedDir: 0 };
  }
  // Counts ONLY the pans seeded by a touchstart of their own — the 2→1 handoff
  // goes through onEnd → panStartFrom and deliberately does not bump it, so a
  // test can tell the two apart.
  var panStartsFromTouch = 0;

  var panEl = document.getElementById('img-container');
  if (panEl) {
    panEl.addEventListener('touchstart', function (e) {
      if (zoom <= 1 || window.ViewerGesture.isPinching()) { pan = null; return; }
      var act = window.ViewerGesture.activeTouches(e);
      if (act.length !== 1) { pan = null; return; }
      killTransition();
      panStartFrom(act[0]);
      if (pan) panStartsFromTouch++;
    }, { passive: true });
    panEl.addEventListener('touchmove', function (e) {
      if (zoom <= 1) return;
      var act = window.ViewerGesture.activeTouches(e);
      if (act.length !== 1) { pan = null; return; }
      // A lone touch that landed within PALM_EDGE of the screen was a palm at
      // its touchstart (no pan was seeded); once it has moved ARM_PX it is the
      // pan finger, promoted inside this touchmove with no touchstart of its
      // own. Seed the pan from where it is now — like the 2→1 handoff.
      if (!pan) {
        if (e.touches.length !== 1 || window.ViewerGesture.isPinching()) return;
        panStartFrom(act[0]);
        return;
      }
      var t = findTouch(e.touches, pan.id); if (!t) return;
      var dx = t.clientX - pan.x0, dy = t.clientY - pan.y0;
      if (!pan.decided) {
        if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
        pan.decided = true; pan.horizontal = Math.abs(dx) > Math.abs(dy);
      }
      var c = clampPan(pan.tx0 + dx, pan.ty0 + dy);
      // The edge turn is decided by where the drag STARTED. A drag that began
      // with room and merely ran into the clamp keeps panning — it must not
      // coast into a page turn.
      var edgeDir = 0;
      if (pan.horizontal && (pan.noRoom === 2 || (pan.noRoom === -1 && dx > 0) || (pan.noRoom === 1 && dx < 0))) {
        edgeDir = dx > 0 ? 1 : -1;
      }
      if (edgeDir !== 0) {
        c.x += (dx > 0 ? 1 : -1) * Math.abs(dx) * RUBBER;   // rubber band past the clamp
        if (Math.abs(dx) >= EDGE_PX) {
          pan.armed = true; pan.armedDir = edgeDir;
          if (!pan.vibrated) {
            pan.vibrated = true;   // once per drag, at the commit threshold only
            try { if (!reducedMotion() && 'vibrate' in navigator) navigator.vibrate(10); } catch (err) { console.warn(err); }
          }
        }
      }
      // Disarm OUTSIDE the edge branch. Dragging back past the threshold — or
      // reversing the finger far enough to flip dx's sign, which makes edgeDir
      // 0 and skips the branch entirely — is the user cancelling the turn, and
      // must not still fire one on release.
      if (edgeDir === 0 || Math.abs(dx) < EDGE_PX) pan.armed = false;
      apply(zoom, c.x, c.y);
    }, { passive: true });
    var panEnd = function (e) {
      if (!pan) return;
      if (!findTouch(e.changedTouches, pan.id)) return;   // a palm lifted, the pan finger is still down
      var p = pan; pan = null;
      // `typeof … === 'function'` everywhere app.js is called from a module:
      // a Service Worker StaleWhileRevalidate can serve a NEW module next to an
      // OLD app.js that has not published these names yet, and a hard throw
      // there would leave the viewer wedged. (app.js itself must not guard the
      // modules — see test_app_js_has_no_module_fallback_bodies.)
      if (p.decided && typeof window.suppressClickFor === 'function') window.suppressClickFor(400);
      if (p.armed) {
        if (p.armedDir > 0 && typeof window.viewerGoNext === 'function') window.viewerGoNext();
        else if (p.armedDir < 0 && typeof window.viewerGoPrev === 'function') window.viewerGoPrev();
        // The turn's snapTo calls Zoom.reset(true), which leaves zoom at 1 —
        // done. At the first/last spread goNext/goPrev return WITHOUT turning
        // (and without snapTo), so the layer is still zoomed and still sitting
        // on the rubber band: fall through and settle it like any other pan.
        if (zoom <= 1) return;
      }
      var c = clampPan(tx, ty); apply(zoom, c.x, c.y);   // settle the rubber band
    };
    panEl.addEventListener('touchend', panEnd, { passive: true });
    panEl.addEventListener('touchcancel', panEnd, { passive: true });
  }

  function onStart(g) {
    if (window.Overview && window.Overview.isOpen()) return;
    killTransition();
    // Reclaim an interrupted slide (app.js local snapTo; window.snapTo is the
    // dormant twin in spread.js). Only at rest: snapTo's head calls
    // Zoom.reset(true), which would destroy a re-pinch from 2x.
    if (zoom === 1 && window.sliding && typeof window.viewerSnapTo === 'function') window.viewerSnapTo(window.currentIdx || 0, false);
    document.body.classList.add('zoomed'); // promote the compositing layer before the first tracked frame
    origin = localRect();
    measureBox();
    startZoom = zoom; startTx = tx; startTy = ty;
    mid0 = { x: g.mid0.x - origin.left, y: g.mid0.y - origin.top };
    fired = false;
  }
  function onMove(g) {
    if (window.Overview && window.Overview.isOpen()) return;
    var target = clamp(startZoom * g.ratio, K.MIN, K.MAX);
    if (!fired && target <= K.FIRE) {
      fired = true;
      if (window.Zoom.onOverviewFire) window.Zoom.onOverviewFire();
      return;
    } else if (fired && target > K.REARM) { fired = false; }
    var mx = g.mid.x - origin.left, my = g.mid.y - origin.top;
    if (target >= 1) {
      // midpoint anchor: the layer point under mid0 at gesture start stays under mid
      var k = target / startZoom;
      var nx = mx - k * (mid0.x - startTx);
      var ny = my - k * (mid0.y - startTy);
      // Clamp while pinching too: without this, a pinch anchored near a corner
      // walks the spread off the viewport and the pan that follows starts from
      // an already-illegal position.
      var cc = clampPan(nx, ny, target);
      apply(target, cc.x, cc.y);
    } else if (target > K.FIRE) {
      // shrink band: scale follows fingers, center drifts toward the overview cell.
      // The layer is inset:0 inside #img-container, so measureBox()'s W/H — taken
      // once at gesture start — ARE the layer's box. Reading offsetWidth here
      // instead would hit the layout engine on every touchmove frame.
      var W = (box && box.W) || window.viewW || window.innerWidth;
      var H = (box && box.H) || window.viewH || window.innerHeight;
      var c0 = { x: W / 2, y: H / 2 };
      var dest = (!reducedMotion() && window.Zoom.driftTarget) ? window.Zoom.driftTarget() : null;
      var p = clamp((1 - target) / 0.5, 0, 1);
      // driftTarget() speaks viewport coordinates (Task 7 reads an overview
      // cell's client rect); convert to the layer's local frame.
      var cx = dest ? c0.x + p * ((dest.x - origin.left) - c0.x) : c0.x;
      var cy = dest ? c0.y + p * ((dest.y - origin.top) - c0.y) : c0.y;
      // layer is inset:0 → its natural center is (W/2, H/2); with origin 0 0: center = t + s*(W/2,H/2)
      apply(target, cx - target * W / 2, cy - target * H / 2);
    }
  }
  function onEnd(g) {
    if (window.Overview && window.Overview.isOpen()) {
      // The overview took over mid-gesture. Leave the transform alone —
      // Overview.open() owns it from here — but drop the class onStart added,
      // or #zone-prev/#zone-next stay pointer-events:none after it closes.
      fired = false;
      document.body.classList.remove('zoomed');
      return;
    }
    fired = false;
    if (zoom > K.SNAP) { /* persist */ }
    else if (zoom >= 1) { apply(1, 0, 0); }   // >=1: also clears the `zoomed` class onStart added
    else { bounceBack(); }
    // 2→1 finger: the finger still down keeps panning, with no touchstart of
    // its own to seed the pan from.
    panStartFrom(g && g.remaining);
  }

  window.Zoom = {
    K: K,
    state: function () { return { zoom: zoom, tx: tx, ty: ty, band: band, edgeArmed: !!(pan && pan.armed) }; },
    isActive: function () { return zoom > 1; },
    reset: reset,
    onOverviewFire: null, driftTarget: null
  };
  if (window.__BOOKQ_TEST_MODE__ === 'true') {
    window.__zoomDebug = { get panStarts() { return panStartsFromTouch; } };
  }
  var container = document.getElementById('img-container');
  if (container) window.ViewerGesture.attach(container, { onStart: onStart, onMove: onMove, onEnd: onEnd });
})();
