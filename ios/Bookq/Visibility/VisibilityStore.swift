import Combine
import Foundation

/// Per-user hidden top-level roots — `browse/visibility.js` equivalent.
///
/// The hidden set is server-owned, keyed by the authenticated user, and
/// revisioned. This store holds the last-synced snapshot (memory + one
/// owner-scoped on-disk entry for offline boot), the transient show-all
/// mode (never persisted), and the O(1) predicates the renderers and the
/// navigation guard consume.
///
/// Fail-closed: until ANY snapshot (cache or server) has been applied,
/// `isVisible` returns false and the grid shows the blocked placeholder —
/// an unknown judgment must never paint an unfiltered list.
@MainActor
final class VisibilityStore: ObservableObject {
    enum Status: String { case idle, loading, ready, readonly, error }
    enum LastError: String { case fetch, auth, save, owner }
    enum MatchKind { case self_, descendant, ancestor }
    enum GuardResult { case allow, pending, hidden }
    enum SaveError: Error { case invalid, busy, offline, conflict, save, net }

    struct Presentation: Equatable {
        var badge: String?      // 非表示 / 上位フォルダが非表示 / 配下に非表示
        var dimmed: Bool        // self/descendant hidden → tile dim
        var toggle: Bool        // eye button allowed (editable & not ancestor)
        var hidden: Bool        // covered by hidden set → toggle offers 再表示
    }

    @Published private(set) var status: Status = .idle
    @Published private(set) var hiddenRoots: Set<String> = []
    @Published private(set) var readOnly = false
    @Published private(set) var lastError: LastError?
    @Published private(set) var saving = false
    @Published var showAll = false {
        didSet {
            guard showAll != oldValue else { return }
            objectWillChange.send()
            scheduleRefresh()
        }
    }

    private(set) var owner: String?
    private(set) var revision = 0
    private(set) var hasSnapshot = false

    private var hiddenNorm: [String: String] = [:]   // normKey → original
    private var appliedSig = ""
    private var refreshTask: Task<Void, Never>?
    private var coalesceTask: Task<Void, Never>?
    private var convergenceSent = false
    /// Bumped on account/server switch — in-flight refreshes whose
    /// epoch went stale are discarded so the old owner's snapshot can
    /// never re-apply after the reset.
    private var accountEpoch = 0
    private var topLevel: Set<String> = []

    private let apiProvider: () -> BookqAPI?
    private let fm = FileManager.default
    private let cacheDir: URL

    /// `apiProvider` resolved per call (survives API-client rebuilds).
    /// `cacheDir` injectable for tests.
    init(apiProvider: @escaping () -> BookqAPI?, cacheDir: URL? = nil) {
        self.apiProvider = apiProvider
        self.cacheDir = cacheDir ?? fm.urls(for: .applicationSupportDirectory,
                                            in: .userDomainMask)[0]
            .appendingPathComponent("visibility", isDirectory: true)
        restoreCached()
    }

    // MARK: - Owner-scoped snapshot cache (offline boot fallback only)

    private struct CacheEnv: Codable {
        var v: Int
        var owner: String
        var revision: Int
        var hiddenRoots: [String]
    }

    private var lastOwnerURL: URL { cacheDir.appendingPathComponent("last-owner.txt") }
    private func cacheURL(for owner: String) -> URL {
        var cs = CharacterSet.alphanumerics
        cs.insert(charactersIn: "-_")
        let key = owner.addingPercentEncoding(withAllowedCharacters: cs) ?? "x"
        return cacheDir.appendingPathComponent("\(key).json")
    }

    private func readCache(owner: String) -> CacheEnv? {
        guard let data = try? Data(contentsOf: cacheURL(for: owner)),
              let o = try? JSONDecoder().decode(CacheEnv.self, from: data),
              o.v == 1, o.owner == owner else { return nil }
        return o
    }

    private func writeCache() {
        guard let owner else { return }
        try? fm.createDirectory(at: cacheDir, withIntermediateDirectories: true)
        let env = CacheEnv(v: 1, owner: owner, revision: revision,
                           hiddenRoots: Array(hiddenRoots))
        if let d = try? JSONEncoder().encode(env) {
            try? d.write(to: cacheURL(for: owner), options: .atomic)
        }
        try? owner.write(to: lastOwnerURL, atomically: true, encoding: .utf8)
    }

    /// Account/server switch — the previous owner's hidden roots must
    /// not stay applied (they'd leak or wrongly hide roots) while the
    /// new account's snapshot is pending. Reset to fail-closed loading;
    /// the next refresh()/cache-restore repopulates for the new owner.
    /// `knownOwner` skips the reset when the resolved account matches
    /// the restored owner — at cold boot auth.user is nil until /me
    /// resolves, and wiping there would discard the same account's
    /// cached snapshot it exists to serve.
    func accountChanged(knownOwner: String? = nil) {
        guard owner != knownOwner else { return }
        owner = nil
        revision = 0
        hasSnapshot = false
        appliedSig = ""
        hiddenRoots = []
        hiddenNorm = [:]
        showAll = false
        readOnly = false
        convergenceSent = false
        status = .loading
        lastError = nil
        coalesceTask?.cancel()
        accountEpoch += 1
        objectWillChange.send()
    }

    /// Same-owner cached snapshot applies immediately (read-only intent
    /// until the GET resolves) — visibility.js init() equivalent.
    private func restoreCached() {
        guard let raw = try? String(contentsOf: lastOwnerURL, encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines),
              !raw.isEmpty,
              let cached = readCache(owner: raw) else { return }
        applySnapshot(owner: cached.owner, revision: cached.revision,
                      roots: cached.hiddenRoots, cache: false)
        status = .readonly
        readOnly = true
    }

    // MARK: - Snapshot adoption

    private func sig(_ rev: Int, _ roots: [String]) -> String {
        rev.description + "|" + roots.sorted().joined(separator: "\0")
    }

    /// Adopt a snapshot; fires objectWillChange when the visible set
    /// changed. Owner switch resets mode/revision so a stale response can
    /// never leak into the new account. `allowDownrev` for server snapshots
    /// only (server truth adopted verbatim; caller converges once).
    @discardableResult
    private func applySnapshot(owner: String, revision: Int, roots: [String],
                               cache: Bool = true, force: Bool = false,
                               allowDownrev: Bool = false) -> Bool {
        if owner != self.owner {
            self.owner = owner
            self.revision = 0
            appliedSig = ""
            showAll = false
        }
        if revision < self.revision && !allowDownrev { return false }
        let list = roots.filter { !$0.isEmpty }
        let newSig = sig(revision, list)
        let changed = newSig != appliedSig
        self.revision = revision
        hiddenRoots = Set(list)
        hiddenNorm = [:]
        for r in list {
            let k = normKey(r)
            if hiddenNorm[k] == nil { hiddenNorm[k] = r }
        }
        hasSnapshot = true
        appliedSig = newSig
        if cache { writeCache() }
        if changed || force { objectWillChange.send() }
        return changed
    }

    // MARK: - Network

    /// `refresh()` — one in-flight GET; queued refresh coalesces (400ms).
    func refresh() async {
        guard let api = apiProvider() else { status = .error; lastError = .fetch; return }
        let cached = owner.flatMap { readCache(owner: $0) }
        let prevStatus = status
        let epoch = accountEpoch
        do {
            // 3s timeout — GET_TIMEOUT_MS parity
            let snap = try await withThrowingTaskGroup(of: VisibilitySnapshot.self) { group in
                group.addTask { try await api.folderVisibility() }
                group.addTask {
                    try await Task.sleep(nanoseconds: 3_000_000_000)
                    throw CancellationError()
                }
                let v = try await group.next()!
                group.cancelAll()
                return v
            }
            guard epoch == accountEpoch else { return }  // account switched mid-flight

            let downrev = (snap.owner == owner) && (snap.revision < revision)
            status = .ready; readOnly = false; lastError = nil
            applySnapshot(owner: snap.owner, revision: snap.revision,
                          roots: snap.hiddenRoots,
                          force: snap.owner != owner, allowDownrev: true)
            if downrev && !convergenceSent {
                convergenceSent = true
                scheduleRefresh()
            } else if !downrev {
                convergenceSent = false
            }
        } catch let e as APIError {
            guard epoch == accountEpoch else { return }
            if case .unauthorized = e {
                status = .error; lastError = .auth
            } else { resolveFetchFailure(cached: cached) }
        } catch {
            guard epoch == accountEpoch else { return }
            resolveFetchFailure(cached: cached)
        }
        if status != prevStatus { objectWillChange.send() }
    }

    private func resolveFetchFailure(cached: CacheEnv?) {
        if let cached {
            // same-owner snapshot → read-only fallback (fail-closed data kept)
            status = .readonly; readOnly = true; lastError = nil
            applySnapshot(owner: cached.owner, revision: cached.revision,
                          roots: cached.hiddenRoots, cache: false)
        } else {
            // No snapshot at all: do NOT assume empty hidden set (would
            // expose everything) and NOT denied-by-default either — grid
            // shows the blocked state with retry.
            status = .error; lastError = .fetch
        }
    }

    /// Coalesced re-refresh (web `_scheduleRefresh` 400ms) — showAll toggle,
    /// visibility regain, post-save converge.
    private func scheduleRefresh() {
        coalesceTask?.cancel()
        coalesceTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 400_000_000)
            guard !Task.isCancelled else { return }
            await self?.refresh()
        }
    }

    /// Error-state retry (status stays error while in flight — web parity).
    func retry() {
        lastError = nil
        objectWillChange.send()
        Task { await refresh() }
    }

    // MARK: - Predicates

    /// Normalize "." / ".." / "" segments away; escaping the library root
    /// yields nil — never a valid root.
    private func normalizePath(_ path: String) -> String? {
        guard !path.isEmpty else { return nil }
        var out: [String] = []
        for s in path.split(separator: "/", omittingEmptySubsequences: false).map(String.init) {
            if s.isEmpty || s == "." { continue }
            if s == ".." { if out.isEmpty { return nil }; out.removeLast(); continue }
            out.append(s)
        }
        return out.joined(separator: "/")
    }

    /// NFC+lowercase compare key — case-insensitive filesystems resolve
    /// "HIDE_ME" to "hide_me"; matching uses this normalization while the
    /// stored keys keep their original names (display/PUT/tree lookup).
    private func normKey(_ p: String) -> String {
        p.precomposedStringWithCanonicalMapping.lowercased()
    }

    /// covering (self/descendant — longest key wins) + ancestor (shortest
    /// key wins). Returns the ORIGINAL hidden key.
    func hiddenMatch(_ path: String) -> (key: String, kind: MatchKind)? {
        guard let norm = normalizePath(path), !norm.isEmpty else { return nil }
        let nkey = normKey(norm)
        var bestCover: String?
        var bestAnc: String?
        for (kn, _) in hiddenNorm {
            if nkey == kn || nkey.hasPrefix(kn + "/") {
                if bestCover == nil || kn.count > bestCover!.count { bestCover = kn }
            } else if kn.hasPrefix(nkey + "/") {
                if bestAnc == nil || kn.count < bestAnc!.count { bestAnc = kn }
            }
        }
        if let bestCover {
            return (hiddenNorm[bestCover] ?? bestCover,
                    bestCover == nkey ? .self_ : .descendant)
        }
        if let bestAnc { return (hiddenNorm[bestAnc] ?? bestAnc, .ancestor) }
        return nil
    }

    /// Hidden key covering path (self/descendant only — deny predicates
    /// never use ancestor matches).
    func hiddenRootFor(_ path: String) -> String? {
        guard let m = hiddenMatch(path), m.kind != .ancestor else { return nil }
        return m.key
    }

    func isHidden(_ path: String) -> Bool { hiddenRootFor(path) != nil }

    /// Fail-closed: no snapshot → nothing is visible (never assume "all
    /// visible"). Ancestor-only matches are also not visible in normal mode
    /// (a provisional tile whose whole subtree is hidden).
    func isVisible(_ path: String) -> Bool {
        if showAll { return true }
        guard hasSnapshot else { return false }
        return hiddenMatch(path) == nil
    }

    /// Top-level nodes that may be hidden/unhidden (server checks tree
    /// membership on hide; ancestor matches are never editable).
    func setTopLevelPaths(_ paths: [String]) { topLevel = Set(paths) }
    func isTopLevel(_ path: String) -> Bool { topLevel.contains(path) }

    /// Navigation gate: pending while the judgment is unknown (loading, or
    /// error without any snapshot), hidden on a positive covering match in
    /// normal mode, else allow.
    func guardPath(_ path: String) -> GuardResult {
        if showAll { return .allow }
        if status == .loading || !hasSnapshot { return .pending }
        return (!path.isEmpty && hiddenRootFor(path) != nil) ? .hidden : .allow
    }

    /// spec §9 fail-open — offline/connecting with NO snapshot: the hidden-
    /// roots judgment cannot complete (the refresh is unreachable), so the
    /// gate opens and the stricter download filter decides. Any snapshot
    /// (cached or live) keeps the normal fail-closed rules.
    func failOpen(isOnline: Bool) -> Bool { !isOnline && !hasSnapshot }

    /// Grid-wide blocked placeholder while NO snapshot exists — with any
    /// snapshot (cached or live) the grid renders normally off its data.
    enum Blocked { case none, loading, error }
    var blockedState: Blocked {
        if hasSnapshot { return .none }
        switch status {
        case .idle, .loading: return .loading
        case .error: return .error
        default: return .none
        }
    }

    /// `uiPresentation` — badges/eye exist ONLY in showAll mode (normal
    /// mode filters hidden roots outright; nothing is marked).
    func presentation(_ path: String, editable: Bool? = nil) -> Presentation? {
        guard showAll else { return nil }
        let m = hiddenMatch(path)
        let badge: String?
        switch m?.kind {
        case .self_: badge = "非表示"
        case .descendant: badge = "上位フォルダが非表示"
        case .ancestor: badge = "配下に非表示"
        case nil: badge = nil
        }
        let hidden = (m?.kind == .self_ || m?.kind == .descendant)
        var editable = editable ?? isTopLevel(path)
        if m?.kind == .ancestor { editable = false }
        return Presentation(badge: badge,
                            dimmed: hidden,
                            toggle: editable && !path.isEmpty,
                            hidden: hidden)
    }

    // MARK: - Write path

    /// Client-side copy of the backend root rule: normalized path, no
    /// empty/"." / ".." / dot-leading segments, no NUL/backslash.
    private func validRootPath(_ root: String) -> String? {
        guard !root.isEmpty, root.count <= 2048,
              !root.contains("\\"), !root.contains("\0") else { return nil }
        for s in root.split(separator: "/", omittingEmptySubsequences: false) {
            if s.isEmpty || s == "." || s == ".." || s.hasPrefix(".") { return nil }
        }
        return root
    }

    /// `setHidden` — one in-flight PUT; response snapshot commits verbatim.
    /// Throws SaveError so the caller can toast the right failure.
    func setHidden(_ root: String, hidden: Bool) async throws {
        guard validRootPath(root) != nil else { throw SaveError.invalid }
        guard !saving else { throw SaveError.busy }
        guard !readOnly, let api = apiProvider() else { throw SaveError.offline }
        saving = true
        defer { saving = false }
        do {
            let snap = try await api.setFolderVisibility(
                root: root, hidden: hidden, owner: owner ?? "")
            status = .ready; readOnly = false; lastError = nil
            if snap.owner != owner || snap.revision >= revision {
                applySnapshot(owner: snap.owner, revision: snap.revision,
                              roots: snap.hiddenRoots,
                              force: snap.owner != owner)
                convergenceSent = false
            } else if !convergenceSent {
                // Stale PUT response (another device wrote via GET while this
                // was in flight) — one convergence GET pulls the true head.
                convergenceSent = true
                scheduleRefresh()
            }
        } catch let e as APIError {
            if case .unauthorized = e {
                status = .error; lastError = .auth; throw SaveError.net
            }
            if case .status(let s) = e, s == 409 {
                // Owner the page booted under is stale — pull the server's
                // authoritative owner/snapshot instead of writing elsewhere.
                status = .error; lastError = .owner
                Task { await refresh() }
                throw SaveError.conflict
            }
            lastError = .save; throw SaveError.save
        } catch {
            lastError = .save; throw SaveError.net
        }
    }

    /// Test hook — apply a snapshot without going through the network path.
    func testApply(owner: String, revision: Int, roots: [String],
                   allowDownrev: Bool = false) {
        applySnapshot(owner: owner, revision: revision, roots: roots,
                      allowDownrev: allowDownrev)
    }

    /// Status row text (web `_updateStatusDom` labels).
    var statusText: String? {
        if saving { return "保存中…" }
        switch status {
        case .loading: return "読込中…"
        case .readonly where readOnly: return "オフライン（変更不可）"
        case .error: return "取得失敗"
        default:
            if lastError == .save { return "保存失敗" }
            return nil
        }
    }
}
