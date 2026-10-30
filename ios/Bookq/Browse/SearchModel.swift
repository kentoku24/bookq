import Combine
import Foundation

/// Global metadata search — `search/facets.js` `doSearch` + `loadFacets`
/// equivalent. Owns query/filters/results state; rendering stays in the
/// browse views (shared tile/list/card cells).
@MainActor
final class SearchModel: ObservableObject {
    @Published var query = "" { didSet { queryChanged() } }
    @Published private(set) var active = false        // searchMode
    @Published private(set) var results: [ComicMetadata] = []
    @Published private(set) var searching = false
    @Published var artists: Set<String> = []
    @Published var tags: Set<String> = []
    @Published private(set) var facets: Facets?

    private let apiProvider: () -> BookqAPI?
    private let showAll: () -> Bool
    private var searchGen = 0
    private var facetGen = 0
    private var debounceTask: Task<Void, Never>?

    init(apiProvider: @escaping () -> BookqAPI?,
         showAll: @escaping () -> Bool = { false }) {
        self.apiProvider = apiProvider
        self.showAll = showAll
    }

    /// Input debounce — web `searchTimer = setTimeout(doSearch, 300)`.
    private func queryChanged() {
        debounceTask?.cancel()
        let q = query
        debounceTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 300_000_000)
            guard !Task.isCancelled else { return }
            await self?.search(q)
        }
    }

    /// Facet toggle/chip-remove path — immediate re-search (same debounce
    /// the web chips use, triggered explicitly).
    func filtersChanged() { queryChanged() }

    /// `doSearch` — generation-guarded; empty query exits search mode.
    func search(_ q: String? = nil) async {
        let query = (q ?? self.query).trimmingCharacters(in: .whitespacesAndNewlines)
        searchGen += 1
        let gen = searchGen
        guard !query.isEmpty else {
            active = false; results = []; searching = false
            return
        }
        guard let api = apiProvider() else { return }
        searching = true
        defer { if gen == searchGen { searching = false } }
        do {
            guard let res = try await api.search(
                q: query, artist: artists.sorted(), tag: tags.sorted(),
                includeHidden: showAll()) else { return }
            // Post-decode staleness check (web re-checks after res.json())
            guard gen == searchGen else { return }
            results = res.results
            active = true
        } catch {
            // keep prior state — web logs and leaves results untouched
        }
    }

    /// `loadFacets` — facet options for the filter sheet; refetched on
    /// showAll toggles (include_hidden affects the server-side set).
    func loadFacets() async {
        facetGen += 1
        let gen = facetGen
        guard let api = apiProvider() else { return }
        do {
            guard let f = try await api.facets(
                path: "", includeHidden: showAll()) else { return }
            guard gen == facetGen else { return }
            facets = f
        } catch {}
    }

    func toggleArtist(_ a: String) {
        if artists.contains(a) { artists.remove(a) } else { artists.insert(a) }
    }

    func toggleTag(_ t: String) {
        if tags.contains(t) { tags.remove(t) } else { tags.insert(t) }
    }

    func resetFilters() { artists = []; tags = [] }

    /// Clear query + exit search mode (result-tap path clears input too).
    func clear() {
        debounceTask?.cancel()
        searchGen += 1
        query = ""
        active = false
        results = []
        searching = false
    }
}
