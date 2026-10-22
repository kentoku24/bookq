/* bookq thumbs/resolveCover — shared cover fallback helper.
 * Used by app.js tile/list/card renders (browse + search).
 * Plain global script (no build step): defines window.resolveCover.
 * Load BEFORE app.js (see index.html deferred script order).
 * Fallback chain: node.thumb → node.thumbs[0] → null (caller shows placeholder).
 * Todo 3 (single-cover-first): resolveCoverSingle(node) is thumb-ONLY at the
 * device cover width (OfflineCore.coverWidth: dpr>=2 → 400, else 200; null
 * when no node.thumb — caller shows placeholder). First-to-verdict stays
 * unified on that one width, never a second fetch (no double-fetch).
 * resolveCover(node, w): callers pass the display width (w=200 for
 * tile/list thumbs, default w=400 for card-img); only 200/400/800 are
 * valid (OfflineCore.thumbUrl clamps anything else to 400).
 * Browse-list covers go through /thumb/ (window.OfflineCore.thumbUrl);
 * viewer + downloads stay on /image/ (window.OfflineCore.imageUrl, untouched).
 */
(function () {
  function resolveCover(node, w) {
    if (!node) return null;
    var folder = node._fullPath;
    if (!folder) return null;
    var OC = (typeof window !== "undefined" && window.OfflineCore) ? window.OfflineCore : null;
    var width = (w === 200 || w === 800) ? w : 400;
    if (node.thumb) {
      return OC && OC.thumbUrl ? OC.thumbUrl(folder, node.thumb, width) : "/thumb/" + folder.split("/").map(encodeURIComponent).join("/") + "/" + node.thumb.split("/").map(encodeURIComponent).join("/") + "?w=" + width;
    }
    if (node.thumbs && Array.isArray(node.thumbs) && node.thumbs.length > 0 && node.thumbs[0]) {
      var t = node.thumbs[0];
      return OC && OC.thumbUrl ? OC.thumbUrl(folder, t, width) : "/thumb/" + folder.split("/").map(encodeURIComponent).join("/") + "/" + t.split("/").map(encodeURIComponent).join("/") + "?w=" + width;
    }
    return null;
  }
  // Todo 3 single-first cover: node.thumb ONLY at the device cover width
  // (OfflineCore.coverWidth), else null (caller shows placeholder).
  // Signature: resolveCoverSingle(node) -> url|null.
  // Todo 5 integration point: setFullPathsAndIndex/thumbSingleList reuse this.
  function resolveCoverSingle(node) {
    if (!node) return null;
    var folder = node._fullPath;
    if (!folder || !node.thumb) return null;
    var OC = (typeof window !== "undefined" && window.OfflineCore) ? window.OfflineCore : null;
    var w = (OC && OC.coverWidth) ? OC.coverWidth() : 200;
    if (OC && OC.thumbUrl) return OC.thumbUrl(folder, node.thumb, w);
    return "/thumb/" + folder.split("/").map(encodeURIComponent).join("/") + "/" + node.thumb.split("/").map(encodeURIComponent).join("/") + "?w=" + w;
  }
  // Returns <img> HTML or placeholder div; cssClass is tile-img/card-img/thumb
  function resolveCoverHtml(node, cssClass) {
    var url = resolveCover(node);
    if (url) {
      var cls = cssClass || "tile-img";
      // Escape url for attribute context — encodePath already safe but be defensive
      var esc = window.OfflineCore.escapeHtml(url);
      return '<img class="' + cls + '" src="' + esc + '" loading="lazy" decoding="async" onerror="this.style.display=\'none\'">';
    }
    // Keep existing placeholder variants; tile/card use tile-placeholder, list uses no thumb
    if (cssClass === "thumb") return "";
    if (cssClass === "card-img") return '<div class="tile-placeholder" style="aspect-ratio:16/9">\uD83D\uDCC1</div>';
    return '<div class="tile-placeholder">\uD83D\uDCC1</div>';
  }
  if (typeof window !== "undefined") {
    window.resolveCover = resolveCover;
    window.resolveCoverSingle = resolveCoverSingle;
    window.resolveCoverHtml = resolveCoverHtml;
    window.ThumbResolve = { resolveCover: resolveCover, resolveCoverSingle: resolveCoverSingle, resolveCoverHtml: resolveCoverHtml };
  }
})();
