// Inject into the page via cdp-eval.py BEFORE the gesture series.
// Arms two collectors; read results afterwards with:
//   cdp-eval.py 'JSON.stringify({lt:window.__lt,loaf:window.__loaf})'
//
// __lt   — longtask entries (>50ms main-thread tasks), rounded ms.
// __loaf — long-animation-frame entries (>50ms frames) as compact strings:
//          "Dms block:B render:R scripts:[S]" where each script is
//          "ms@file:line:invoker".
//
// Reading the result:
//   blocking:0 + scripts:[] -> the long frame is NOT main-thread work.
//   Frame time went to the compositor/raster side (e.g. backdrop-filter
//   re-blur, layer promotion, GPU back-pressure). Optimising JS further
//   will not move the number — look at paint/compositing cost instead.
//   scripts:[...] -> JS work is on the frame; use the invoker/line to find it.
window.__lt = [];
window.__loaf = [];
try {
  new PerformanceObserver(function (l) {
    for (const e of l.getEntries())
      window.__lt.push(Math.round(e.duration) + "ms");
  }).observe({ type: "longtask" });
} catch (e) { /* Firefox lacks the entry type */ }
try {
  new PerformanceObserver(function (l) {
    for (const e of l.getEntries()) {
      var s = (e.scripts || []).map(function (x) {
        return Math.round(x.duration) + "ms@" +
          (x.sourceURL || "").split("/").pop() + ":" + x.sourceLine + ":" +
          (x.invoker || "");
      }).join(";");
      var render = e.renderStart ? e.duration - (e.renderStart - e.startTime) : 0;
      window.__loaf.push(Math.round(e.duration) + "ms block:" +
        Math.round(e.blockingDuration) + " render:" + Math.round(render) +
        " scripts:[" + s + "]");
    }
  }).observe({ type: "long-animation-frame" });
} catch (e) { /* Chrome-only */ }
"armed";
