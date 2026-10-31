/* bookq api/v1 — thin fetch layer over the /api/v1 contract.
 * Plain global script: defines window.BookqApi.
 * Centralizes the endpoint URLs and the v1 response-envelope unwrappers so
 * call sites don't hand-roll `{kind, nodes, generation}` / `{folder, images}`
 * handling (and a future generated client has one seam to replace).
 * Loads before all feature modules (after offline-core.js).
 *
 * Contract source of truth: api/openapi.v1.yaml. The response shapes the
 * unwrap helpers accept are typed in `app/static/api/api-types.d.ts`,
 * generated with:
 *   npx openapi-typescript@7 api/openapi.v1.yaml -o app/static/api/api-types.d.ts
 */
(function () {
  "use strict";

  var E = {
    tree: "/api/v1/tree",
    images: "/api/v1/images",
    search: "/api/v1/search",
    facets: "/api/v1/facets",
    thumbSprite: "/api/v1/thumb-sprite",
    readingPositions: "/api/v1/reading-positions",
    folderVisibility: "/api/v1/folder-visibility",
    indexRefresh: "/api/v1/index/refresh",
    version: "/api/v1/version",
    health: "/api/v1/health",
  };

  /** Build `/api/v1/tree` with `path`/`depth`/`since` query params. */
  function treeUrl(opts) {
    var url = E.tree;
    var params = new URLSearchParams();
    if (opts && opts.path) params.set("path", opts.path);
    if (opts && opts.depth) params.set("depth", String(opts.depth));
    if (opts && opts.since != null) params.set("since", String(opts.since));
    var qs = params.toString();
    return qs ? url + "?" + qs : url;
  }

  function imagesUrl(folder) {
    return E.images + "?folder=" + encodeURIComponent(folder);
  }

  /**
   * Normalize a tree response body to the node array.
   * Accepts the v1 `{kind, nodes, generation}` envelope and the legacy bare
   * array (offline snapshots written before the v1 cutover). Returns null
   * when the shape is unrecognized.
   * @param {unknown} body
   * @returns {import('./api-types').components['schemas']['TreeNode'][] | null}
   */
  function nodesOf(body) {
    if (Array.isArray(body)) return body;
    if (body && Array.isArray(body.nodes)) return body.nodes;
    if (body && Array.isArray(body.tree)) return body.tree;   // v0 since-poll dict (tolerated)
    return null;
  }

  /**
   * Normalize an images response body to the filename array —
   * v1 `{folder, images[]}` (legacy bare arrays tolerated).
   * @param {unknown} body
   * @returns {string[] | null}
   */
  function imagesOf(body) {
    if (Array.isArray(body)) return body;   // tolerate pre-v1 cached bodies
    if (body && Array.isArray(body.images)) return body.images;
    return null;
  }

  /**
   * The caller's single latest reading position from a GET response body —
   * `{positions: [...]}` newest-first → first element, or null.
   * @param {unknown} body
   * @returns {import('./api-types').components['schemas']['ReadingPosition'] | null}
   */
  function latestPositionOf(body) {
    if (body && typeof body.position !== "undefined") return body.position || null; // v0 shape (tolerated)
    if (body && Array.isArray(body.positions)) return body.positions[0] || null;
    return null;
  }

  /** PUT URL for one folder's record: `/api/v1/reading-positions?folder=<enc>`. */
  function putPositionUrl(folder) {
    return E.readingPositions + "?folder=" + encodeURIComponent(folder);
  }

  globalThis.BookqApi = {
    endpoints: E,
    treeUrl: treeUrl,
    imagesUrl: imagesUrl,
    nodesOf: nodesOf,
    imagesOf: imagesOf,
    latestPositionOf: latestPositionOf,
    putPositionUrl: putPositionUrl,
  };
})();
