// @ts-check
/* bookq api/v1 — typed fetch client over the /api/v1 contract.
 * Plain global script: defines window.BookqApi. The single typed seam
 * between the PWA and the backend (#236):
 *
 *   - endpoint paths come from api/endpoints.js (generated from
 *     api/openapi.v1.yaml) — no `/api/v1/...` literals anywhere else;
 *   - request/response shapes come from the generated api-types.d.ts, so a
 *     spec change that renames a field breaks `tsc` (jsconfig.json +
 *     pwa-typecheck CI) instead of surfacing only in Playwright E2E;
 *   - every 202 (index-building) retry lives in fetchWithBuildingRetry and
 *     nowhere else; every non-2xx surfaces as ApiError carrying the spec
 *     `code` (Error.code) for call-site interpretation.
 *
 * Legacy envelope tolerance (bare arrays / v0 shapes from pre-v1 offline
 * snapshots) stays in nodesOf/imagesOf/latestPositionOf only — the typed
 * methods validate the v1 envelope and throw ApiError on mismatch.
 * Loads before all feature modules (after offline-core.js, endpoints.js).
 *
 * Contract source of truth: api/openapi.v1.yaml. Regenerate types with:
 *   npx openapi-typescript@7 api/openapi.v1.yaml -o app/static/api/api-types.d.ts
 * Regenerate paths with:
 *   python3 scripts/gen-api-endpoints.py
 */
(function () {
  "use strict";

  /**
   * @typedef {import('./api-types').components['schemas']['TreeNode']} TreeNode
   * @typedef {import('./api-types').components['schemas']['TreeResponse']} TreeResponse
   * @typedef {import('./api-types').components['schemas']['Facets']} Facets
   * @typedef {import('./api-types').components['schemas']['ComicMetadata']} ComicMetadata
   * @typedef {import('./api-types').components['schemas']['ReadingPosition']} ReadingPosition
   * @typedef {import('./api-types').components['schemas']['FolderVisibility']} FolderVisibility
   * @typedef {import('./api-types').components['schemas']['Error']} ApiErrorBody
   * @typedef {import('./bookq-api').TreeQuery} TreeQuery
   * @typedef {import('./bookq-api').SearchQuery} SearchQuery
   * @typedef {import('./bookq-api').FacetsQuery} FacetsQuery
   * @typedef {import('./bookq-api').PositionsQuery} PositionsQuery
   * @typedef {import('./bookq-api').DeletePositionsQuery} DeletePositionsQuery
   * @typedef {import('./bookq-api').PutPositionBody} PutPositionBody
   * @typedef {import('./bookq-api').PutVisibilityBody} PutVisibilityBody
   * @typedef {import('./bookq-api').FetchOpts} FetchOpts
   * @typedef {import('./bookq-api').RetryOpts} RetryOpts
   */

  // Canonical paths live in api/endpoints.js (generated). The inline table is
  // for standalone loads only (offline-harness.html) — literals are allowed
  // here because this file IS app/static/api/.
  var _FALLBACK_ENDPOINTS = {
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
    authToken: "/api/v1/auth/token",
    pairReport: "/api/v1/pair-report",
  };
  var E = (typeof BookqApiEndpoints !== "undefined") ? BookqApiEndpoints : _FALLBACK_ENDPOINTS;

  /**
   * Typed HTTP failure: status + machine-readable spec `code` (Error.code,
   * optional — null when absent) + human `detail`. Throw site for every
   * non-2xx (and every malformed-2xx) client call.
   */
  class ApiError extends Error {
    /**
     * @param {number} status
     * @param {string | null} code
     * @param {string} detail
     */
    constructor(status, code, detail) {
      super("bookq api error " + status + (code ? " (" + code + ")" : "") + ": " + detail);
      this.name = "ApiError";
      this.status = status;
      this.code = code;
      this.detail = detail;
    }
  }

  /**
   * Machine-readable error code from a parsed error body. The `.code` access
   * is typed against the spec Error schema — renaming/removing `code` in
   * api/openapi.v1.yaml breaks this line on purpose (#236).
   * @param {unknown} body
   * @returns {string | null}
   */
  function errorCodeOf(body) {
    if (!body || typeof body !== "object") return null;
    /** @type {ApiErrorBody} */
    var err = /** @type {any} */ (body);
    return (typeof err.code === "string") ? err.code : null;
  }

  /**
   * Human detail from a parsed error body (spec Error.detail, tolerant).
   * @param {unknown} body
   * @returns {string}
   */
  function detailOf(body) {
    if (body && typeof body === "object") {
      var d = /** @type {any} */ (body).detail;
      if (typeof d === "string") return d;
    }
    return "";
  }

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
   * @returns {TreeNode[] | null}
   */
  function nodesOf(body) {
    if (Array.isArray(body)) return /** @type {TreeNode[]} */ (body);
    if (body && typeof body === "object") {
      var o = /** @type {{ nodes?: unknown, tree?: unknown }} */ (body);
      if (Array.isArray(o.nodes)) return /** @type {TreeNode[]} */ (o.nodes);
      if (Array.isArray(o.tree)) return /** @type {TreeNode[]} */ (o.tree);   // v0 since-poll dict (tolerated)
    }
    return null;
  }

  /**
   * Normalize an images response body to the filename array —
   * v1 `{folder, images[]}` (legacy bare arrays tolerated).
   * @param {unknown} body
   * @returns {string[] | null}
   */
  function imagesOf(body) {
    if (Array.isArray(body)) return /** @type {string[]} */ (body);   // tolerate pre-v1 cached bodies
    if (body && typeof body === "object") {
      var o = /** @type {{ images?: unknown }} */ (body);
      if (Array.isArray(o.images)) return /** @type {string[]} */ (o.images);
    }
    return null;
  }

  /**
   * The caller's single latest reading position from a GET response body —
   * `{positions: [...]}` newest-first → first element, or null.
   * @param {unknown} body
   * @returns {ReadingPosition | null}
   */
  function latestPositionOf(body) {
    if (!body || typeof body !== "object") return null;
    var o = /** @type {{ position?: unknown, positions?: unknown }} */ (body);
    if (typeof o.position !== "undefined") return /** @type {ReadingPosition} */ (o.position) || null; // v0 shape (tolerated)
    if (Array.isArray(o.positions)) return /** @type {ReadingPosition} */ (o.positions[0]) || null;
    return null;
  }

  /** PUT URL for one folder's record: `/api/v1/reading-positions?folder=<enc>`. */
  function putPositionUrl(folder) {
    return E.readingPositions + "?folder=" + encodeURIComponent(folder);
  }

  /**
   * Single fetch+parse seam for the typed methods: same-origin credentials
   * by default, JSON parsed (null when the body is not JSON), transport
   * failures left to reject.
   * @param {string} url
   * @param {RequestInit} [init]
   * @param {(typeof fetch)} [fetchFn]
   * @returns {Promise<{ status: number, body: unknown, headers: Headers, res: Response }>}
   */
  function fetchJson(url, init, fetchFn) {
    var fn = fetchFn || globalThis.fetch.bind(globalThis);
    var merged = Object.assign({}, init);
    if (!merged.credentials) merged.credentials = "same-origin";
    return fn(url, merged).then(function (res) {
      return res.json().then(function (body) {
        return { status: res.status, body: body, headers: res.headers, res: res };
      }, function () {
        return { status: res.status, body: null, headers: res.headers, res: res };
      });
    });
  }

  /**
   * THE 202 centralization (#236): every index-building endpoint
   * (tree/search/facets answer 202 + Retry-After while the metadata index
   * builds) retries here and nowhere else. Honors Retry-After (clamped to
   * 10s), otherwise a 1.5s→5s backoff, bounded by maxAttempts; a flipped
   * isCurrent() stops the loop and hands back the last 202 so superseded UI
   * generations never spin.
   * @param {string} url
   * @param {(typeof fetch)} [fetchFn]
   * @param {RetryOpts} [opts]
   * @returns {Promise<Response>}
   */
  function fetchWithBuildingRetry(url, fetchFn, opts) {
    var fn = fetchFn || globalThis.fetch.bind(globalThis);
    var maxAttempts = (opts && typeof opts.maxAttempts === "number") ? opts.maxAttempts : 10;
    var isCurrent = (opts && opts.isCurrent) || null;
    var init = Object.assign({}, (opts && opts.init) || {});
    if (!init.credentials) init.credentials = "same-origin";
    var attempt = 0;
    /**
     * @param {Response} res
     * @returns {number}
     */
    function retryDelayMs(res) {
      var secs = NaN;
      try {
        var h = res.headers ? res.headers.get("Retry-After") : null;
        if (h != null) secs = parseInt(h, 10);
      } catch (e) { /* header unreadable — fall back to the clock */ }
      if (isFinite(secs) && secs >= 0) return Math.min(secs * 1000, 10000);
      return Math.min(1500 * (attempt + 1), 5000);
    }
    /**
     * @returns {Promise<Response>}
     */
    function loop() {
      return fn(url, init).then(function (res) {
        if (res.status !== 202) return res;
        if (attempt >= maxAttempts - 1) return res;
        if (isCurrent && !isCurrent()) return res;
        var delay = retryDelayMs(res);
        attempt++;
        return new Promise(function (r) { setTimeout(r, delay); }).then(loop);
      });
    }
    return loop();
  }

  /**
   * Throw an ApiError for a non-ok fetchJson result (carries spec `code`).
   * @param {string} url
   * @param {{ status: number, body: unknown }} out
   * @returns {never}
   */
  function throwApiError(url, out) {
    throw new ApiError(out.status, errorCodeOf(out.body), detailOf(out.body) || ("request failed: " + url));
  }

  /**
   * GET tree — 202 auto-retried centrally. Envelope field accesses are typed
   * against the spec: renaming `nodes`/`generation` breaks this method (#236).
   * @param {TreeQuery} [params]
   * @param {RetryOpts} [opts]
   * @returns {Promise<TreeResponse>}
   */
  function getTree(params, opts) {
    var url = treeUrl(params || undefined);
    var fn = (opts && opts.fetch) || undefined;
    return fetchWithBuildingRetry(url, fn, opts).then(function (res) {
      if (!res.ok) {
        return res.json().then(function (body) {
          return throwApiError(url, { status: res.status, body: body });
        }, function () {
          return throwApiError(url, { status: res.status, body: null });
        });
      }
      return res.json().then(function (body) {
        if (body && typeof body === "object" && !Array.isArray(body)) {
          /** @type {TreeResponse} */
          var env = /** @type {any} */ (body);
          if (Array.isArray(env.nodes) && typeof env.generation === "number") {
            return { kind: env.kind, nodes: env.nodes, generation: env.generation };
          }
        }
        var legacy = nodesOf(body);   // pre-v1 bare-array snapshots (tolerated)
        if (legacy) return { kind: /** @type {"tree"} */ ("tree"), nodes: legacy, generation: 0 };
        return throwApiError(url, { status: res.status, body: body });
      }, function () {
        return throwApiError(url, { status: res.status, body: null });
      });
    });
  }

  /**
   * GET images — typed `{folder, images[]}` envelope; `images` access is
   * spec-checked (#236). Malformed 200s throw (callers treat as failure,
   * never as an empty folder).
   * @param {string} folder
   * @param {FetchOpts} [opts]
   * @returns {Promise<string[]>}
   */
  function getImages(folder, opts) {
    var url = imagesUrl(folder);
    var init = { credentials: "same-origin", signal: (opts && opts.signal) || undefined };
    return fetchJson(url, /** @type {RequestInit} */ (init), opts && opts.fetch || undefined).then(function (out) {
      if (out.status < 200 || out.status >= 300) return throwApiError(url, out);
      var body = out.body;
      if (body && typeof body === "object" && !Array.isArray(body)) {
        /** @type {import('./api-types').paths['/api/v1/images']['get']['responses'][200]['content']['application/json']} */
        var env = /** @type {any} */ (body);
        if (Array.isArray(env.images)) return env.images;
      }
      var legacy = imagesOf(body);
      if (legacy) return legacy;
      return throwApiError(url, { status: out.status, body: body });
    });
  }

  /**
   * Build a query string from a spec-typed query object (undefined values
   * skipped; everything else String()-ified for URLSearchParams).
   * @param {any} params
   * @returns {string}
   */
  function queryString(params) {
    var qp = new URLSearchParams();
    for (var k in params) {
      if (!Object.prototype.hasOwnProperty.call(params, k)) continue;
      var v = params[k];
      if (v === undefined || v === null || v === "") continue;
      qp.set(k, String(v));
    }
    return qp.toString();
  }

  /**
   * GET search — 202 auto-retried centrally; response field accesses are
   * spec-checked (#236).
   * @param {SearchQuery} params
   * @param {RetryOpts} [opts]
   * @returns {Promise<import('./api-types').paths['/api/v1/search']['get']['responses'][200]['content']['application/json']>}
   */
  function search(params, opts) {
    var qs = queryString(params);
    var url = qs ? E.search + "?" + qs : E.search;
    var fn = (opts && opts.fetch) || undefined;
    return fetchWithBuildingRetry(url, fn, opts).then(function (res) {
      if (!res.ok) {
        return res.json().then(function (body) {
          return throwApiError(url, { status: res.status, body: body });
        }, function () {
          return throwApiError(url, { status: res.status, body: null });
        });
      }
      return res.json().then(function (body) {
        /** @type {import('./api-types').paths['/api/v1/search']['get']['responses'][200]['content']['application/json']} */
        var env = /** @type {any} */ (body);
        var results = Array.isArray(env.results) ? env.results : null;
        if (!results) return throwApiError(url, { status: res.status, body: body });
        return {
          query: (typeof env.query === "string") ? env.query : "",
          total: (typeof env.total === "number") ? env.total : results.length,
          results: results,
        };
      }, function () {
        return throwApiError(url, { status: res.status, body: null });
      });
    });
  }

  /**
   * GET facets — 202 auto-retried centrally; `artists`/`tags` accesses are
   * spec-checked (#236).
   * @param {FacetsQuery} [params]
   * @param {RetryOpts} [opts]
   * @returns {Promise<Facets>}
   */
  function getFacets(params, opts) {
    var qs = queryString(params || {});
    var url = qs ? E.facets + "?" + qs : E.facets;
    var fn = (opts && opts.fetch) || undefined;
    return fetchWithBuildingRetry(url, fn, opts).then(function (res) {
      if (!res.ok) {
        return res.json().then(function (body) {
          return throwApiError(url, { status: res.status, body: body });
        }, function () {
          return throwApiError(url, { status: res.status, body: null });
        });
      }
      return res.json().then(function (body) {
        /** @type {Facets} */
        var env = /** @type {any} */ (body);
        var artists = Array.isArray(env.artists) ? env.artists : null;
        var tags = Array.isArray(env.tags) ? env.tags : null;
        if (!artists || !tags) return throwApiError(url, { status: res.status, body: body });
        return { artists: artists, tags: tags };
      }, function () {
        return throwApiError(url, { status: res.status, body: null });
      });
    });
  }

  /**
   * GET reading-positions — newest-first array (v0 `{position}` tolerated).
   * @param {PositionsQuery} [query]
   * @param {FetchOpts} [opts]
   * @returns {Promise<ReadingPosition[]>}
   */
  function getPositions(query, opts) {
    var qs = queryString(query || {});
    var url = qs ? E.readingPositions + "?" + qs : E.readingPositions;
    var init = { credentials: "same-origin", cache: "no-store", signal: (opts && opts.signal) || undefined };
    return fetchJson(url, /** @type {RequestInit} */ (init), opts && opts.fetch || undefined).then(function (out) {
      if (out.status < 200 || out.status >= 300) return throwApiError(url, out);
      var body = out.body;
      if (body && typeof body === "object" && !Array.isArray(body)) {
        /** @type {import('./api-types').paths['/api/v1/reading-positions']['get']['responses'][200]['content']['application/json']} */
        var env = /** @type {any} */ (body);
        if (Array.isArray(env.positions)) return env.positions;
      }
      var single = latestPositionOf(body);
      if (single) return [single];
      return throwApiError(url, { status: out.status, body: body });
    });
  }

  /**
   * PUT one folder's reading position. keepalive so pagehide flushes survive;
   * `{status:"stale"}` (older ts than the server record) is a success.
   * @param {PutPositionBody & { folder: string }} rec
   * @param {FetchOpts} [opts]
   * @returns {Promise<import('./api-types').paths['/api/v1/reading-positions']['put']['responses'][200]['content']['application/json']>}
   */
  function putPosition(rec, opts) {
    var url = putPositionUrl(rec.folder);
    /** @type {PutPositionBody} */
    var payload = { page: rec.page, total: rec.total, ts: rec.ts };
    var init = {
      method: "PUT", credentials: "same-origin", keepalive: true,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: (opts && opts.signal) || undefined,
    };
    return fetchJson(url, /** @type {RequestInit} */ (init), opts && opts.fetch || undefined).then(function (out) {
      if (out.status < 200 || out.status >= 300) return throwApiError(url, out);
      /** @type {import('./api-types').paths['/api/v1/reading-positions']['put']['responses'][200]['content']['application/json']} */
      var env = /** @type {any} */ (out.body);
      if (env && (env.status === "ok" || env.status === "stale")) return { status: env.status };
      return throwApiError(url, out);
    });
  }

  /**
   * DELETE reading position(s) — folder-scoped, or everything when omitted.
   * @param {DeletePositionsQuery} [query]
   * @param {FetchOpts} [opts]
   * @returns {Promise<boolean>}
   */
  function deletePositions(query, opts) {
    var qs = queryString(query || {});
    var url = qs ? E.readingPositions + "?" + qs : E.readingPositions;
    var init = { method: "DELETE", credentials: "same-origin", keepalive: true, signal: (opts && opts.signal) || undefined };
    var fn = (opts && opts.fetch) || globalThis.fetch.bind(globalThis);
    return fn(url, /** @type {RequestInit} */ (init)).then(function (res) {
      return !!(res && res.ok);
    });
  }

  /**
   * Validate an untyped value as a FolderVisibility snapshot; null when the
   * shape is wrong (fail-closed callers treat this as an error, never as an
   * empty hidden set).
   * @param {unknown} body
   * @returns {FolderVisibility | null}
   */
  function visibilityOf(body) {
    if (!body || typeof body !== "object" || Array.isArray(body)) return null;
    /** @type {FolderVisibility} */
    var env = /** @type {any} */ (body);
    if (typeof env.owner !== "string" || !env.owner) return null;
    if (typeof env.revision !== "number") return null;
    if (!Array.isArray(env.hiddenRoots)) return null;
    return { owner: env.owner, revision: env.revision, hiddenRoots: env.hiddenRoots };
  }

  /**
   * GET folder-visibility — network-only by SW policy; validated snapshot.
   * @param {FetchOpts} [opts]
   * @returns {Promise<FolderVisibility>}
   */
  function getVisibility(opts) {
    var url = E.folderVisibility;
    var init = { credentials: "same-origin", cache: "no-store", signal: (opts && opts.signal) || undefined };
    return fetchJson(url, /** @type {RequestInit} */ (init), opts && opts.fetch || undefined).then(function (out) {
      if (out.status < 200 || out.status >= 300) return throwApiError(url, out);
      var snap = visibilityOf(out.body);
      if (!snap) return throwApiError(url, out);
      return snap;
    });
  }

  /**
   * PUT folder-visibility toggle — the response snapshot commits verbatim;
   * a 409 (stale owner) throws ApiError with status 409 for the caller to
   * re-sync instead of writing to another account.
   * @param {string} root
   * @param {boolean} hidden
   * @param {string} owner
   * @param {FetchOpts} [opts]
   * @returns {Promise<FolderVisibility>}
   */
  function putVisibility(root, hidden, owner, opts) {
    var url = E.folderVisibility;
    /** @type {PutVisibilityBody} */
    var payload = { root: root, hidden: !!hidden };
    var init = {
      method: "PUT", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json", "X-Bookq-Visibility-Owner": owner },
      body: JSON.stringify(payload),
      signal: (opts && opts.signal) || undefined,
    };
    return fetchJson(url, /** @type {RequestInit} */ (init), opts && opts.fetch || undefined).then(function (out) {
      if (out.status < 200 || out.status >= 300) return throwApiError(url, out);
      var snap = visibilityOf(out.body);
      if (!snap) return throwApiError(url, out);
      return snap;
    });
  }

  /** @type {import('./bookq-api').BookqApiShape} */
  var api = {
    endpoints: E,
    ApiError: ApiError,
    treeUrl: treeUrl,
    imagesUrl: imagesUrl,
    putPositionUrl: putPositionUrl,
    nodesOf: nodesOf,
    imagesOf: imagesOf,
    latestPositionOf: latestPositionOf,
    errorCodeOf: errorCodeOf,
    fetchJson: fetchJson,
    fetchWithBuildingRetry: fetchWithBuildingRetry,
    getTree: getTree,
    getImages: getImages,
    search: search,
    getFacets: getFacets,
    getPositions: getPositions,
    putPosition: putPosition,
    deletePositions: deletePositions,
    getVisibility: getVisibility,
    putVisibility: putVisibility,
  };

  globalThis.BookqApi = api;
})();
