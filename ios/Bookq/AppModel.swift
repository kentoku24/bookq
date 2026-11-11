import SwiftUI

/// Top-level app state machine: configure server → authenticate → browse.
@MainActor
final class AppModel: ObservableObject {
    enum Phase {
        case onboarding     // no server URL yet
        case booting        // checking /api/v1/version
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
    /// Local reading positions — namespaced by host+account like
    /// `offline` so a server/account switch never exposes another
    /// identity's progress (#375). Lazy: the scope closure needs self.
    private(set) lazy var progress = ReadingProgressStore(
        scopeProvider: { [weak self] in
            let host = self?.config.baseURL?.host ?? "anon"
            return "\(host)|\(self?.auth.user?.email ?? "anon")"
        })
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
    /// External-display projection (USB-C → monitor). Singleton shared
    /// with the scene delegate, which cannot receive environment objects.
    let external = ExternalDisplayStore.shared
    /// `updateOnlineStatus` — health-probe driven connectivity pill.
    private(set) lazy var online = OnlineStatusStore(
        apiProvider: { [weak self] in self?.api })
    /// Dual-reader sync (reader_sync server-ws equivalent).
    private(set) lazy var pair = PairStore(
        url: { [weak self] in self?.api?.readerSyncURL },
        token: { [weak self] in self?.auth.tokenBox.token },
        persistKey: { [weak self] in
            "bookq.pairSync.v1.\(self?.config.baseURL?.host ?? "default")" })
    private(set) var api: BookqAPI?
    private(set) var library: LibraryStore?

    init() {
        // `devicectl process launch -e '{"BOOKQ_SERVER_URL": "..."}'` (or a
        // `-serverURL <url>` argv pair) injects a URL that only shadows the
        // stored value for that one session — the next cold start falls
        // back to the stale one. Writing it through @AppStorage makes the
        // injected URL persist.
        if let injected = Self.injectedServerURL(),
           injected != config.serverURLString {
            print("[bookq] serverURL injected → \(injected)")
            config.serverURLString = injected
        }
        auth.restore()
        // `BOOKQ_TEST_TOKEN` env or `-testToken <jwt>` argv seeds a dummy
        // pair for simulator/dev runs against BOOKQ_TEST_MODE servers,
        // which accept any Bearer — only the JWT `sub` claim is decoded
        // client-side to build AuthUser. Seeded after restore() so it
        // wins over a stale Keychain pair.
        if let token = Self.injectedTestToken() {
            print("[bookq] test token injected")
            auth.testSeed(TokenPair(TokenPairResponse(
                access_token: token, token_type: "Bearer",
                expires_in: 3600, refresh_token: "")))
        }
        if config.baseURL != nil {
            configureAPI()
            phase = .booting
            Task { await boot() }
        }
    }

    /// `BOOKQ_SERVER_URL` env var or `-serverURL <url>` argv pair, or nil.
    static func injectedServerURL() -> String? {
        if let e = ProcessInfo.processInfo.environment["BOOKQ_SERVER_URL"]?
            .trimmingCharacters(in: .whitespacesAndNewlines), !e.isEmpty {
            return e
        }
        let args = ProcessInfo.processInfo.arguments
        guard let i = args.firstIndex(of: "-serverURL"), i + 1 < args.count
        else { return nil }
        let v = args[i + 1].trimmingCharacters(in: .whitespacesAndNewlines)
        return v.isEmpty ? nil : v
    }

    /// `BOOKQ_TEST_TOKEN` env var or `-testToken <jwt>` argv pair, or nil.
    static func injectedTestToken() -> String? {
        if let e = ProcessInfo.processInfo.environment["BOOKQ_TEST_TOKEN"]?
            .trimmingCharacters(in: .whitespacesAndNewlines), !e.isEmpty {
            return e
        }
        let args = ProcessInfo.processInfo.arguments
        guard let i = args.firstIndex(of: "-testToken"), i + 1 < args.count
        else { return nil }
        let v = args[i + 1].trimmingCharacters(in: .whitespacesAndNewlines)
        return v.isEmpty ? nil : v
    }

    /// (Re)build the API client after a server URL change or login.
    func configureAPI() {
        guard let base = config.baseURL else {
            api = nil
            library = nil
            phase = .onboarding
            return
        }
        // No shared URLCache for the authed API session — cached error
        // responses (e.g. a 404 caught during a server restart) must not be
        // replayed to later boots, and authed bodies stay off disk. Cookies
        // stay off too: native auth is Bearer-only, and a stray Set-Cookie
        // session would win over the Bearer header server-side.
        let cfg = URLSessionConfiguration.default
        cfg.urlCache = nil
        cfg.httpCookieStorage = nil
        cfg.httpShouldSetCookies = false
        auth.baseURLProvider = { [weak self] in self?.config.baseURL }
        let api = BookqAPI(baseURL: base,
                           session: URLSession(configuration: cfg),
                           tokenProvider: { [box = auth.tokenBox] in box.token },
                           tokenRefresher: { [weak self] in
                               await self?.auth.refreshAccessToken()
                           })
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
    /// folders image children only, others fall back to /api/v1/images.
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
    /// `GET /api/v1/version` doubles as the auth probe — v1 has no /me,
    /// identity comes from the access-token JWT claims instead.
    /// The probe is time-boxed: a dead tailnet host blackholes TCP inside
    /// the default connection timeout (~75 s), leaving the spinner up with
    /// no way out. 15 s is far above any healthy probe RTT.
    func boot() async {
        guard let api else { phase = .onboarding; return }
        phase = .booting
        bootError = nil
        print("[bookq] boot: probing \(api.baseURL)")
        do {
            _ = try await Self.withTimeout(seconds: 15) { try await api.version() }
            // Account switch — the previous user's per-account state
            // (visibility, offline records) must not bleed through. The
            // store itself knows the previous owner (auth.user is nil
            // until now): a same-account reboot must keep its cached
            // snapshot.
            visibility.accountChanged(knownOwner: auth.user?.email)
            offline.scopeChanged()
            progress.scopeChanged()
            phase = .ready
            Task { await progress.syncWithServer() }
            pair.bootResume()
            print("[bookq] boot: ready")
        } catch APIError.unauthorized {
            phase = .login
            print("[bookq] boot: unauthorized → login")
        } catch {
            bootError = error.localizedDescription
            // Web parity: a dead server is not a setup problem when we hold
            // a tree snapshot — enter the library offline (SW cache parity)
            // instead of dumping the user on the onboarding screen.
            phase = (library?.tree.isEmpty == false) ? .ready : .onboarding
            print("[bookq] boot: \(error) → \(phase == .ready ? "ready(offline)" : "onboarding")")
        }
    }

    /// Timeout marker for the boot probe.
    private struct BootTimeout: Error {}

    /// Race `work` against a deadline; the loser is cancelled.
    private static func withTimeout<T: Sendable>(
        seconds: UInt64,
        work: @escaping @Sendable () async throws -> T
    ) async throws -> T {
        try await withThrowingTaskGroup(of: T.self) { group in
            group.addTask { try await work() }
            group.addTask {
                try await Task.sleep(nanoseconds: seconds * 1_000_000_000)
                try Task.checkCancellation()
                throw BootTimeout()
            }
            defer { group.cancelAll() }
            guard let r = try await group.next() else { throw BootTimeout() }
            return r
        }
    }

    /// `bookq://config?server=<url>` — deep link to (re)point the app at a
    /// server; works through `devicectl device process openURL`, a QR code,
    /// or a web link. Server switches always log out first, so a hostile
    /// link can never smuggle a Bearer token to another host.
    func handleDeepLink(_ url: URL) {
        guard url.scheme == "bookq", url.host == "config",
              let comps = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let server = comps.queryItems?.first(where: { $0.name == "server" })?.value,
              !server.isEmpty else { return }
        print("[bookq] deep link: server → \(server)")
        config.serverURLString = server
        serverURLChanged()
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
        progress.scopeChanged()
        Task { await boot() }
    }

    func didLogin() {
        configureAPI()
        // A different account signing in must not inherit the previous
        // owner's hidden roots or offline records.
        visibility.accountChanged(knownOwner: auth.user?.email)
        offline.scopeChanged()
        progress.scopeChanged()
        phase = .ready
    }

    func logout() {
        auth.logout()
        phase = .login
    }
}
