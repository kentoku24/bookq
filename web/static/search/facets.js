/* bookq search/facets — facet loading & filter sheet options.
 * Plain global script: defines window.loadFacets, renderFacetOptions.
 * Depends on window.activeFilters / window.facetLoadGen etc exposed by
 * app.js composition root.
 *
 * Wire (#236): facet transport goes through the typed window.BookqApi
 * client (web/static/api/v1.js) — 202 (index-building) retries live in
 * BookqApi.fetchWithBuildingRetry only, and response shapes are spec-typed.
 *
 * NOTE: the library-wide /api/v1/search result view was retired — the
 * search input now sorts the current folder's children client-side
 * (browse/matchSort.js). Filter chips/sheet UI lives in app.js; this
 * module only loads facet options and renders the sheet's chip lists.
 */
// @ts-check
(function () {
  // State lives in app.js; access via window at call-time.
  function _getActiveFilters() { return window.activeFilters; }
  function _getFacets() { return window.facets; }
  function _setFacets(v) { window.facets = v; }
  function _incFacetGen() { return ++window.facetLoadGen; }
  function _getFacetGenVal() { return window.facetLoadGen; }

  // Single escaper, from offline-core.js (loaded first in index.html).
  var _escapeHtml = window.OfflineCore.escapeHtml;

  function _resolveDeps(deps){
    deps = deps || {};
    return { fetch: deps.fetch || (typeof window !== 'undefined' && window.Connectivity && window.Connectivity.fetch ? window.Connectivity.fetch : (typeof window !== 'undefined' && window.fetch ? window.fetch.bind(window) : fetch)) };
  }

  async function loadFacets(path, deps) {
    if(path && typeof path === 'object' && path.fetch){ deps = path; path = deps.path || ""; }
    var _d = _resolveDeps(deps);
    var myGen = _incFacetGen();
    // All-display: ask the server for the owner's full facet set — hidden
    // works' artists/tags must be selectable while browsing everything.
    var _FVf = window.FolderVisibility;
    /** @type {import('../api/bookq-api').FacetsQuery} */
    var fquery = {};
    if (path) fquery.path = path;
    if (_FVf && _FVf.showAll) fquery.include_hidden = true;
    try {
      var fdata = await window.BookqApi.getFacets(fquery, {
        fetch: _d.fetch,
        isCurrent: function(){ return myGen === _getFacetGenVal(); },
      });
      // The body parse is itself an await — a newer load may have bumped
      // the generation while it resolved; re-check before committing.
      if (myGen !== _getFacetGenVal()) return;
      _setFacets(fdata);
      renderFacetOptions();
    } catch(e) {
      if (myGen !== _getFacetGenVal()) return;   // superseded 202 loop — silent
      console.warn(e);
    }
  }

  function renderFacetOptions() {
    var facets = _getFacets() || {artists:[],tags:[]};
    var activeFilters = _getActiveFilters() || {artist:[],tag:[],sort:'relevance'};
    var filterBtn = document.getElementById("filter-btn");
    var artistsContainer = document.querySelector("#facet-artists .facet-chips");
    if (artistsContainer) {
      artistsContainer.innerHTML = (facets.artists || []).map(function(a){
        var active = activeFilters.artist.includes(a) ? ' active' : '';
        return '<span class="chip' + active + '" data-facet="artist" data-value="' + _escapeHtml(a) + '" tabindex="0" role="button" aria-pressed="' + (active ? 'true' : 'false') + '">' + _escapeHtml(a) + '</span>';
      }).join('') || '<span style="color:#555;font-size:12px">No artists</span>';
      artistsContainer.querySelectorAll('.chip').forEach(function(el){
        el.addEventListener('click', function(){
          var c = /** @type {any} */ (el);
          var val = c.dataset.value; var facet = c.dataset.facet;
          if (!val || !facet) return;
          var idx = activeFilters[facet].indexOf(val);
          if (idx >= 0) activeFilters[facet].splice(idx, 1);
          else activeFilters[facet].push(val);
          el.classList.toggle('active');
          el.setAttribute('aria-pressed', String(el.classList.contains('active')));
        });
      });
    }
    var tagsContainer = document.querySelector("#facet-tags .facet-chips");
    if (tagsContainer) {
      tagsContainer.innerHTML = (facets.tags || []).map(function(t){
        var active = activeFilters.tag.includes(t) ? ' active' : '';
        return '<span class="chip' + active + '" data-facet="tag" data-value="' + _escapeHtml(t) + '" tabindex="0" role="button" aria-pressed="' + (active ? 'true' : 'false') + '">' + _escapeHtml(t) + '</span>';
      }).join('') || '<span style="color:#555;font-size:12px">No tags</span>';
      tagsContainer.querySelectorAll('.chip').forEach(function(el){
        el.addEventListener('click', function(){
          var c = /** @type {any} */ (el);
          var val = c.dataset.value; var facet = c.dataset.facet;
          if (!val || !facet) return;
          var idx = activeFilters[facet].indexOf(val);
          if (idx >= 0) activeFilters[facet].splice(idx, 1);
          else activeFilters[facet].push(val);
          el.classList.toggle('active');
          el.setAttribute('aria-pressed', String(el.classList.contains('active')));
        });
      });
    }
    var sortRadios = document.querySelectorAll('input[name="sort"]');
    sortRadios.forEach(function(radio){
      var r = /** @type {any} */ (radio);
      r.checked = (r.value === activeFilters.sort);
    });
    if (filterBtn) filterBtn.classList.toggle('active', activeFilters.artist.length > 0 || activeFilters.tag.length > 0);
    var menuBtn = document.getElementById('header-menu-btn');
    if (menuBtn) menuBtn.classList.toggle('active', activeFilters.artist.length > 0 || activeFilters.tag.length > 0);
  }

  window.loadFacets = loadFacets;
  window.renderFacetOptions = renderFacetOptions;
  window.SearchFacets = { loadFacets, renderFacetOptions };
})();
