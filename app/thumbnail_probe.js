(() => {
  // Measurement-only observer. Never changes the app's loading/lazy policy.
  const decoded = new WeakMap();
  const ids = new WeakMap();
  let nextId = 0;
  let signature = "";
  let stableSince = 0;
  let readyAt = null;
  let firstVisibleAt = null;
  const config = window.__thumbnailProbeConfig || {};
  const timeoutMs = Number.isFinite(config.timeoutMs) && config.timeoutMs > 0
    ? config.timeoutMs : 30000;
  window.__thumbnailProbe = {done: false};

  function intersects(r, clip) {
    return r.width > 0 && r.height > 0 && r.bottom > clip.top &&
      r.top < clip.bottom && r.right > clip.left && r.left < clip.right;
  }
  function sample(now) {
    const grid = document.getElementById("tile-grid");
    const sidebar = document.getElementById("sidebar");
    const scroll = document.getElementById("sidebar-scroll");
    const sr = scroll && scroll.getBoundingClientRect();
    const clip = {
      top: Math.max(0, sr ? sr.top : 0),
      bottom: Math.min(innerHeight, sr ? sr.bottom : innerHeight),
      left: Math.max(0, sr ? sr.left : 0),
      right: Math.min(innerWidth, sr ? sr.right : innerWidth),
    };
    const tiles = grid ? [...grid.querySelectorAll(
      ".tile[data-path], .item[data-path], .card[data-path]"
    )] : [];
    const visibleTiles = sidebar && sidebar.classList.contains("open")
      ? tiles.filter(el => intersects(el.getBoundingClientRect(), clip)) : [];
    const images = visibleTiles.flatMap(tile => {
      const tr = tile.getBoundingClientRect();
      const tileClip = {
        top: Math.max(clip.top, tr.top), bottom: Math.min(clip.bottom, tr.bottom),
        left: Math.max(clip.left, tr.left), right: Math.min(clip.right, tr.right),
      };
      return [...tile.querySelectorAll("img")].filter(img =>
        intersects(img.getBoundingClientRect(), tileClip));
    });
    let loaded = 0;
    let broken = 0;
    const keys = [];
    for (const img of images) {
      if (!ids.has(img)) ids.set(img, ++nextId);
      const src = img.currentSrc || img.getAttribute("src") || "";
      keys.push([ids.get(img), src]);
      let state = decoded.get(img);
      if (!state || state.src !== src) {
        state = {src, ok: false, error: false};
        decoded.set(img, state);
        if (src) {
          // A fulfilled decode is stronger than img.complete, which includes 404s.
          img.decode().then(() => { state.ok = true; }, () => { state.error = true; });
        }
      }
      if (src && img.complete && img.naturalWidth > 0 && state.ok) loaded++;
      else if (src && (state.error || (img.complete && img.naturalWidth === 0))) broken++;
    }
    const fullTreeReady = window.treeLoaded === true &&
      !(window.__BOOKQ_DEBUG && window.__BOOKQ_DEBUG.shallow);
    const current = JSON.stringify([fullTreeReady, tiles.length, visibleTiles.length, keys]);
    if (current !== signature) {
      signature = current;
      stableSince = now;
      readyAt = null;
    }
    if (loaded && firstVisibleAt === null) firstVisibleAt = now;
    const allReady = fullTreeReady && images.length > 0 && loaded === images.length;
    if (!allReady) readyAt = null;
    else if (readyAt === null) readyAt = now;
    // Confirm a quiet final grid, but do not add the quiet window to the KPI.
    const confirmedAt = Math.max(stableSince, readyAt) + 250;
    const success = allReady && now >= confirmedAt && confirmedAt <= timeoutMs;
    const timedOut = now >= timeoutMs;
    const done = success || timedOut;
    window.__thumbnailProbe = {
      done,
      status: success ? "ok" : timedOut ? (broken ? "broken_images" : "timeout") : "measuring",
      first_visible_ms: firstVisibleAt,
      visible_ready_ms: success ? readyAt : null,
      observed_ms: now,
      full_tree_ready: fullTreeReady,
      rendered_tiles: tiles.length,
      visible_tiles: visibleTiles.length,
      visible_images: images.length,
      decoded_images: loaded,
      broken_images: broken,
      pending_images: images.length - loaded - broken,
      stability_window_ms: 250,
    };
    if (done) {
      // Only aggregate state leaves the probe; never expose image URLs or work names.
      window.__thumbnailProbeReport = {
        metric_version: 1,
        environment: {
          user_agent: navigator.userAgent,
          navigation_time_origin_ms: performance.timeOrigin,
          viewport: {width: innerWidth, height: innerHeight, dpr: devicePixelRatio},
          browser_cache: config.cacheCondition || "existing_browser_session_not_reset",
          service_worker_controlled: !!(navigator.serviceWorker && navigator.serviceWorker.controller),
          timeout_ms: timeoutMs,
        },
        result: window.__thumbnailProbe,
      };
    }
    if (!done) requestAnimationFrame(sample);
  }
  requestAnimationFrame(sample);
})();
