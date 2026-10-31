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
    /// Records/files are namespaced by host+account so a server or user
    /// switch can never expose another scope's downloads.
    private(set) lazy var offline = OfflineStore(
        apiProvider: { [weak self] in self?.api },
        originalImages: { [weak self] in self?.config.originalImages ?? false },
        scopeProvider: { [weak self] in
            let host = self?.config.baseURL?.host ?? "anon"
            return "\(host)|\(self?.auth.user?.email ?? "anon")"
        })
    /// Hidden-root state machine (browse/visibility.js equivalent).
    private(set) lazy var visibility = VisibilityStore(
        apiProvider: { [weak self] in self?.api })
    /// Global metadata search (search/facets.js equivalent).
    private(set) lazy var search = SearchModel(
        apiProvider: { [weak self] in self?.api },
        showAll: { [weak self] in self?.visibility.showAll ?? false })
    /// Dual-reader sync (reader_sync server-ws equivalent).
    private(set) lazy var pair = PairStore(
        url: { [weak self] in self?.api?.readerSyncURL },
        token: { [weak self] in self?.auth.tokenBox.token },
        persistKey: { [weak self] in
            "bookq.pairSync.v1.\(self?.config.baseURL?.host ?? "default")" })
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
        RemoteImageLoader.shared.clear()  // server switch: stale images must not resolve
        RemoteImageLoader.shared.offlineResolver = { [weak offline] in
            offline?.localFileURL(remoteURL: $0)
        }
        progress.attach(ProgressSync(api: api))
        configurePair()
    }

    /// PairStore providers — injected per API rebuild so a server switch
    /// can never resolve pages or veto picks against the wrong backend.
    private func configurePair() {
        pair.openWorkResolver = { [weak self] folder in
            await self?.resolveInitialPageRef(folder)
        }
        pair.visibilityBlocks = { [weak self] path in
            self?.visibility.guardPath(path) == .hidden
        }
        pair.bootstrapWork = { [weak self] in
            guard let self, let last = self.progress.lastRead,
                  let ref = await self.resolveInitialPageRef(last.path)
            else { return nil }
            return (last.path, ref)
        }
    }

    /// `_resolveInitialPageRef` — leaf folders take all children, mixed
    /// folders image children only, others fall back to /api/images.
    /// Index = saved resume page clamped into range (web _pickRef).
    func resolveInitialPageRef(_ folder: String) async -> PairPageRef? {
        await Self.initialPageRef(
            folder: folder, tree: library?.tree ?? [],
            images: { try? await self.api?.images(folder: folder) },
            savedPage: progress.entries[folder]?.page)
    }

    /// Pure core of `resolveInitialPageRef` — extracted for tests (web
    /// `_resolveInitialPageRef` parity: leaf = all children, mixed =
    /// image children only, fallback = fetched listing; saved resume
    /// page clamped into range).
    static func initialPageRef(
        folder: String, tree: [TreeNode],
        images: () async -> [String]?,
        savedPage: Int?
    ) async -> PairPageRef? {
        var names: [String]? = nil
        if let node = BrowseModel.node(at: folder, in: tree) {
            if BrowseModel.hasOnlyImages(node) {
                names = (node.children ?? []).map(\.name)
            } else if BrowseModel.hasImages(node) {
                names = (node.children ?? []).filter(\.isImage).map(\.name)
            }
        }
        if names == nil { names = await images() }
        guard let names, !names.isEmpty else { return nil }
        let idx = min(max(savedPage ?? 0, 0), names.count - 1)
        return PairPageRef(folder: folder, name: names[idx])
    }

    /// Probe the server. 401 → login required; 200 → ready.
    func boot() async {
        guard let api else { phase = .onboarding; return }
        phase = .booting
        bootError = nil
        do {
            let me = try await api.me()
            if auth.user?.email != me.email {
                // Account switch — the previous user's per-account state
                // (visibility, offline records) must not bleed through.
                visibility.accountChanged()
                auth.apply(user: me)
                offline.scopeChanged()
            } else {
                auth.apply(user: me)
            }
            phase = .ready
            Task { await progress.syncWithServer() }
            pair.bootResume()
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
    /// Old-scope state is dropped before the new host is probed so a
    /// slow first response cannot serve another server's downloads or
    /// hidden-root set.
    func serverURLChanged() {
        auth.logout()
        visibility.accountChanged()
        configureAPI()
        offline.scopeChanged()
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
