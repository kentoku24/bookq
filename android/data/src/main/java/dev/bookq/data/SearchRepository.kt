package dev.bookq.data

import dev.bookq.core.api.ComicMetadata
import dev.bookq.core.api.Facets
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.FlowPreview
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.flow.debounce
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/**
 * Global metadata search — `search/facets.js` `doSearch`/`loadFacets`
 * equivalent (iOS `SearchModel` port). Owns query/filters/results state;
 * rendering stays in the browse UI.
 *
 *  - 300 ms input debounce (web `searchTimer`); generation guard drops
 *    late responses (a post-decode re-check mirrors the web's `res.json()`
 *    race window).
 *  - Empty query exits search mode and clears results.
 *  - 202 index-building retries live inside [BookqApi.fetchBuilding].
 *  - `include_hidden` follows the visibility showAll mode via [showAll].
 */
@OptIn(FlowPreview::class, ExperimentalCoroutinesApi::class)
class SearchRepository(
    private val api: BookqApi,
    private val scope: CoroutineScope,
    private val showAll: () -> Boolean = { false },
) {

    private val _query = MutableStateFlow("")
    val query = _query.asStateFlow()

    private val _active = MutableStateFlow(false)          // searchMode
    val active = _active.asStateFlow()

    private val _results = MutableStateFlow<List<ComicMetadata>>(emptyList())
    val results = _results.asStateFlow()

    private val _searching = MutableStateFlow(false)
    val searching = _searching.asStateFlow()

    /** Armed facet filters (artist/tag sets). Mutated live by the sheet —
     *  matches web's shared `activeFilters`. */
    val artists = MutableStateFlow<Set<String>>(emptySet())
    val tags = MutableStateFlow<Set<String>>(emptySet())

    private val _facets = MutableStateFlow<Facets?>(null)
    val facets = _facets.asStateFlow()

    private var searchGen = 0
    private var facetGen = 0

    init {
        // Input debounce — `searchTimer = setTimeout(doSearch, 300)`.
        scope.launch {
            _query.debounce(300).collectLatest { doSearch(it) }
        }
    }

    fun setQuery(q: String) { _query.value = q }

    private var filtersJob: kotlinx.coroutines.Job? = null

    /** Facet toggle/chip-remove path — same 300 ms debounce the web
     *  chips use (`searchTimer = setTimeout(doSearch, 300)`). */
    fun filtersChanged() {
        filtersJob?.cancel()
        filtersJob = scope.launch {
            delay(300)
            doSearch(_query.value)
        }
    }

    /** `doSearch` — generation-guarded; empty query exits search mode. */
    private suspend fun doSearch(rawQ: String) {
        val q = rawQ.trim()
        val gen = ++searchGen
        if (q.isEmpty()) {
            _active.value = false
            _results.value = emptyList()
            _searching.value = false
            return
        }
        _searching.value = true
        try {
            val res = api.search(
                q = q,
                artist = artists.value.sorted(),
                tag = tags.value.sorted(),
                includeHidden = showAll(),
            ) ?: return // still building after all retries — keep prior state
            // Post-decode staleness check (web re-checks after res.json()).
            if (gen != searchGen) return
            _results.value = res.results
            _active.value = true
        } catch (e: Exception) {
            // keep prior state — web logs and leaves results untouched
        } finally {
            if (gen == searchGen) _searching.value = false
        }
    }

    /** `loadFacets` — options for the filter sheet. `path` scopes to a
     *  subtree ("" = whole library, web's search-mode scope). */
    fun loadFacets(path: String = "") {
        val gen = ++facetGen
        scope.launch {
            try {
                val f = api.facets(path, showAll()) ?: return@launch
                if (gen != facetGen) return@launch
                _facets.value = f
            } catch (e: Exception) { /* keep prior options */ }
        }
    }

    fun toggleArtist(a: String) {
        artists.update { if (a in it) it - a else it + a }
    }

    fun toggleTag(t: String) {
        tags.update { if (t in it) it - t else it + t }
    }

    fun resetFilters() {
        artists.value = emptySet()
        tags.value = emptySet()
        filtersChanged()
    }

    fun removeFilter(kind: Kind, value: String) {
        when (kind) {
            Kind.ARTIST -> artists.update { it - value }
            Kind.TAG -> tags.update { it - value }
        }
        filtersChanged()
    }

    enum class Kind { ARTIST, TAG }

    /** Result-tap / hidden-redirect path — clears input + exits mode. */
    fun clear() {
        searchGen++
        _query.value = ""
        _active.value = false
        _results.value = emptyList()
        _searching.value = false
    }
}
