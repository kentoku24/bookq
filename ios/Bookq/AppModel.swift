import SwiftUI

/// Top-level app state machine: configure server → authenticate → browse.
@MainActor
final class AppModel: ObservableObject {
    enum Phase {
        case onboarding     // no server URL yet
        case booting        // checking /api/me
        case login          // server demands auth, no valid token
        case ready          // authenticated (or TEST_MODE server)
    }

    @Published private(set) var phase: Phase = .onboarding
    @Published var bootError: String?
    @Published var navPath: [BrowseRoute] = []
    @Published var openReader: BrowseRoute?

    func pushLevel(_ path: String) {
        navPath.append(.level(path))
    }

    let config = AppConfig()
    let auth = AuthManager()
    let progress = ReadingProgressStore()
    /// Offline downloads — survives API rebuilds via provider closures.
    private(set) lazy var offline = OfflineStore(
        apiProvider: { [weak self] in self?.api },
        originalImages: { [weak self] in self?.config.originalImages ?? false })
    /// Hidden-root state machine (browse/visibility.js equivalent).
    private(set) lazy var visibility = VisibilityStore(
        apiProvider: { [weak self] in self?.api })
    /// Global metadata search (search/facets.js equivalent).
    private(set) lazy var search = SearchModel(
        apiProvider: { [weak self] in self?.api },
        showAll: { [weak self] in self?.visibility.showAll ?? false })
    private(set) var api: BookqAPI?
    private(set) var library: LibraryStore?

    init() {
        auth.restore()
        if config.baseURL != nil {
            configureAPI()
            phase = .booting
            Task { await boot() }
        }
    }

    /// (Re)build the API client after a server URL change or login.
    func configureAPI() {
        guard let base = config.baseURL else {
            api = nil
            library = nil
            phase = .onboarding
            return
        }
        let api = BookqAPI(baseURL: base,
                           tokenProvider: { [box = auth.tokenBox] in box.token })
        self.api = api
        self.library = LibraryStore(api: api)
        self.library?.visibility = visibility
        RemoteImageLoader.shared.api = api
        RemoteImageLoader.shared.offlineResolver = { [weak offline] in
            offline?.localFileURL(remoteURL: $0)
        }
        progress.attach(ProgressSync(api: api))
    }

    /// Probe the server. 401 → login required; 200 → ready.
    func boot() async {
        guard let api else { phase = .onboarding; return }
        phase = .booting
        bootError = nil
        do {
            let me = try await api.me()
            auth.apply(user: me)
            phase = .ready
            Task { await progress.syncWithServer() }
        } catch APIError.unauthorized {
            phase = .login
        } catch {
            bootError = error.localizedDescription
            // Web parity: a dead server is not a setup problem when we hold
            // a tree snapshot — enter the library offline (SW cache parity)
            // instead of dumping the user on the onboarding screen.
            phase = (library?.tree.isEmpty == false) ? .ready : .onboarding
        }
    }

    /// Server URL was saved in onboarding/settings → rebuild and probe.
    func serverURLChanged() {
        auth.logout()
        configureAPI()
        Task { await boot() }
    }

    func didLogin() {
        configureAPI()
        phase = .ready
    }

    func logout() {
        auth.logout()
        phase = .login
    }
}
