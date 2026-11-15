/* bookq api endpoints — shared `/api/v1/...` path constants.
 * GENERATED — do not hand-edit. Regenerate:
 *   python3 scripts/gen-api-endpoints.py
 * Sources: api/openapi.v1.yaml (v1 paths) + test-only pair-report
 * (app/api_v1.py, include_in_schema=False — see script EXTRA).
 * Loads in both the page (before api/v1.js) and the Service Worker via
 * importScripts (no window there) — hence globalThis, no window/document.
 */
(function () {
  "use strict";

  var E = {
    authToken: "/api/v1/auth/token",
    facets: "/api/v1/facets",
    folderVisibility: "/api/v1/folder-visibility",
    health: "/api/v1/health",
    images: "/api/v1/images",
    indexRefresh: "/api/v1/index/refresh",
    readingPositions: "/api/v1/reading-positions",
    search: "/api/v1/search",
    thumbSprite: "/api/v1/thumb-sprite",
    tree: "/api/v1/tree",
    version: "/api/v1/version",
    pairReport: "/api/v1/pair-report",
  };

  var g = (typeof globalThis !== 'undefined') ? globalThis : self;
  g.BookqApiEndpoints = E;
})();
