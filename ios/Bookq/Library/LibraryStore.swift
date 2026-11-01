import Combine
import Foundation
import UIKit

/// Folder tree + hidden-root state. Loaded on boot, refreshed via
/// `/api/v1/index/refresh` + generation tracking.
@MainActor
final class LibraryStore: ObservableObject {
    @Published private(set) var tree: [TreeNode] = []
    @Published private(set) var generation: Int = 0
    @Published private(set) var loading = false
    @Published private(set) var error: String?

    /// Hidden-root judgment lives in VisibilityStore (server-owned,
    /// guarded). Set post-init so the store can be constructed first;
    /// attaching republishes the top-level set (snapshot trees too).
    var visibility: VisibilityStore? {
        didSet { republishTopLevel() }
    }

    private let api: BookqAPI
    private var etag: String?
    private var loadedOnce = false

    /// Snapshot of the last good tree — the web equivalent is the SW
    /// NetworkFirst cache of `/api/v1/tree`, which lets the PWA browse (and
    /// open downloaded works) while offline. Keyed per server base URL so
    /// switching servers can't leak the previous server's tree.
    private let snapshotURL: URL

    init(api: BookqAPI) {
        self.api = api
        var cs = CharacterSet.alphanumerics
        cs.insert(charactersIn: "-_")
        let key = (api.baseURL.host ?? "default")
            .addingPercentEncoding(withAllowedCharacters: cs) ?? "default"
        snapshotURL = FileManager.default.urls(for: .applicationSupportDirectory,
                                               in: .userDomainMask)[0]
            .appendingPathComponent("tree-\(key).json")
        restoreSnapshot()
    }

    /// Initial + incremental load. `force` bypasses the ETag/generation skip.
    func load(force: Bool = false) async {
        guard !loading else { return }
        loading = true
        defer { loading = false }
        error = nil
        do {
            let result: TreeResult
            if loadedOnce && !force {
                result = try await api.tree(since: generation, etag: etag)
            } else {
                result = try await api.tree()
            }
            switch result {
            case .nodes(_, let tree, let gen, let etag):
                self.tree = tree
                self.generation = gen      // v1 envelope carries it — no ETag parse
                self.etag = etag
                loadedOnce = true
            case .notModified:
                break
            }
            republishTopLevel()
            Task { await visibility?.refresh() }
            saveSnapshot()
        } catch {
            self.error = error.localizedDescription
        }
    }

    /// Republish the top-level _fullPath set to the visibility store —
    /// web republishes `setTopLevelPaths` on every (re)index too.
    private func republishTopLevel() {
        visibility?.setTopLevelPaths(tree.filter { $0.isFolder }.map { $0.name })
    }

    // MARK: - Offline snapshot

    private struct Snapshot: Codable {
        var tree: [TreeNode]
        var generation: Int
    }

    private func saveSnapshot() {
        struct Env: Encodable { var snapshot: Snapshot }
        let snap = Snapshot(tree: tree, generation: generation)
        try? FileManager.default.createDirectory(
            at: snapshotURL.deletingLastPathComponent(),
            withIntermediateDirectories: true)
        if let data = try? JSONEncoder().encode(Env(snapshot: snap)) {
            try? data.write(to: snapshotURL, options: .atomic)
        }
    }

    /// Pre-populate `tree` before the first fetch — offline launches get
    /// last-known folders immediately (SW-cached /api/v1/tree parity).
    private func restoreSnapshot() {
        struct Env: Decodable { var snapshot: Snapshot }
        guard let data = try? Data(contentsOf: snapshotURL),
              let snap = try? JSONDecoder().decode(Env.self, from: data).snapshot
        else { return }
        tree = snap.tree
        generation = snap.generation
        republishTopLevel()
    }

    private var pollTask: Task<Void, Never>?

    /// Web fill-poll equivalent: while the app is active, re-request the
    /// tree delta (`?since=<gen>`) on a fixed 15s cadence so newly added
    /// works appear without a manual refresh.
    func startPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 15_000_000_000)
                guard !Task.isCancelled else { return }
                if UIApplication.shared.applicationState == .active {
                    await self?.load()
                }
            }
        }
    }

    /// POST /api/v1/index/refresh (server rescan) then reload unconditionally.
    func refresh() async {
        do { try await api.refresh() } catch {
            self.error = error.localizedDescription
            return
        }
        loadedOnce = false
        await load(force: true)
    }

    // MARK: - Queries

    func node(at path: String) -> TreeNode? {
        BrowseModel.node(at: path, in: tree)
    }

    /// Folder entries for a browse level. Visibility is judged per full
    /// path via `isVisible` (fail-closed, ancestor matches filtered too) —
    /// same predicate every web renderer uses.
    func levelFolders(path: String) -> [(path: String, node: TreeNode)] {
        let entries = BrowseModel.folders(at: path, in: tree)
        guard let vis = visibility, !vis.showAll else { return entries }
        return entries.filter { vis.isVisible($0.path) }
    }

    /// Self/read tiles for a level (mixed children, or root loose images).
    func selfTiles(path: String) -> [(path: String, node: TreeNode)] {
        let tiles = BrowseModel.selfTiles(at: path, in: tree)
        guard let vis = visibility, !vis.showAll else { return tiles }
        return tiles.filter { $0.path.isEmpty || vis.isVisible($0.path) }
    }

    /// All leaf folders (for future resume/offline enumeration).
    func leafPaths(under path: String = "") -> [String] {
        var out: [String] = []
        func walk(_ nodes: [TreeNode], _ parent: String) {
            for n in nodes where n.isFolder {
                let full = BrowseModel.fullPath(parent, n.name)
                if BrowseModel.hasOnlyImages(n) { out.append(full) }
                walk(n.children ?? [], full)
            }
        }
        let start = path.isEmpty ? tree : (node(at: path)?.children ?? [])
        if path.isEmpty {
            // root itself may directly hold images
            if tree.contains(where: { $0.isImage }) { out.append("") }
        } else if let n = node(at: path), BrowseModel.hasImages(n) {
            out.append(path)
        }
        walk(start, path)
        return out
    }
}
