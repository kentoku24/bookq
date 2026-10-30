import Combine
import Foundation
import UIKit

/// Folder tree + hidden-root state. Loaded on boot, refreshed via
/// `/api/refresh` + generation tracking.
@MainActor
final class LibraryStore: ObservableObject {
    @Published private(set) var tree: [TreeNode] = []
    @Published private(set) var generation: Int = 0
    @Published private(set) var hiddenRoots: Set<String> = []
    @Published var showAll: Bool = false
    @Published private(set) var loading = false
    @Published private(set) var error: String?

    private let api: BookqAPI
    private var etag: String?
    private var loadedOnce = false

    /// Snapshot of the last good tree — the web equivalent is the SW
    /// NetworkFirst cache of `/api/folders`, which lets the PWA browse (and
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
            let result: FoldersResult
            if loadedOnce && !force {
                result = try await api.folders(since: generation, etag: etag)
            } else {
                result = try await api.folders()
            }
            switch result {
            case .full(let tree, let etag):
                self.tree = tree
                self.etag = etag
                loadedOnce = true
                generation = extractGeneration(etag: etag) ?? generation
            case .delta(let tree, let gen, let etag):
                self.tree = tree
                self.generation = gen
                self.etag = etag
            case .notModified:
                break
            }
            await loadVisibility()
            saveSnapshot()
        } catch {
            self.error = error.localizedDescription
        }
    }

    /// "building" 202s are already retried inside BookqAPI.folders.
    private func loadVisibility() async {
        do {
            let snap = try await api.folderVisibility()
            hiddenRoots = Set(snap.hidden)
        } catch {
            // visibility is best-effort — never fail the browse for it
        }
    }

    // MARK: - Offline snapshot

    private struct Snapshot: Codable {
        var tree: [TreeNode]
        var hiddenRoots: Set<String>
        var generation: Int
    }

    private func saveSnapshot() {
        struct Env: Encodable { var snapshot: Snapshot }
        let snap = Snapshot(tree: tree, hiddenRoots: hiddenRoots,
                            generation: generation)
        try? FileManager.default.createDirectory(
            at: snapshotURL.deletingLastPathComponent(),
            withIntermediateDirectories: true)
        if let data = try? JSONEncoder().encode(Env(snapshot: snap)) {
            try? data.write(to: snapshotURL, options: .atomic)
        }
    }

    /// Pre-populate `tree` before the first fetch — offline launches get
    /// last-known folders immediately (SW-cached /api/folders parity).
    private func restoreSnapshot() {
        struct Env: Decodable { var snapshot: Snapshot }
        guard let data = try? Data(contentsOf: snapshotURL),
              let snap = try? JSONDecoder().decode(Env.self, from: data).snapshot
        else { return }
        tree = snap.tree
        hiddenRoots = snap.hiddenRoots
        generation = snap.generation
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

    /// POST /api/refresh (server rescan) then reload unconditionally.
    func refresh() async {
        do { try await api.refresh() } catch {
            self.error = error.localizedDescription
            return
        }
        loadedOnce = false
        await load(force: true)
    }

    /// ETag embeds the generation (`"\"<gen>-<digest>\""`) — cheap hint only;
    /// the authoritative counter arrives on the first delta.
    private func extractGeneration(etag: String?) -> Int? {
        guard let etag else { return nil }
        let digits = etag.filter { $0.isNumber }
        return Int(digits.prefix(10))
    }

    // MARK: - Queries

    func node(at path: String) -> TreeNode? {
        BrowseModel.node(at: path, in: tree)
    }

    /// Folder entries for a browse level, honoring hidden roots at top level.
    func levelFolders(path: String) -> [(path: String, node: TreeNode)] {
        var entries = BrowseModel.folders(at: path, in: tree)
        if path.isEmpty && !showAll {
            entries = entries.filter { !hiddenRoots.contains($0.node.name) }
        }
        return entries
    }

    /// Self/read tiles for a level (mixed children, or root loose images).
    func selfTiles(path: String) -> [(path: String, node: TreeNode)] {
        var tiles = BrowseModel.selfTiles(at: path, in: tree)
        if path.isEmpty && !showAll {
            tiles = tiles.filter { $0.path.isEmpty || !hiddenRoots.contains($0.node.name) }
        }
        return tiles
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
