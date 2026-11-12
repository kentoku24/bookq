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

    /// Offline download records + connectivity state (docs/offline-spec.md
    /// §6). Set post-init by AppModel — while the machine is not `.online`
    /// the browse queries below apply the uniform download filter:
    /// intermediate folders appear only when they contain downloaded
    /// content, and leaf tiles appear greyed unless readable offline.
    var offline: OfflineStore?
    var online: OnlineStatusStore?

    /// Connectivity gate for browse filtering — `connecting` shares the
    /// offline presentation (spec §6).
    private var offlineMode: Bool { online?.isOnline == false }

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
    /// works appear without a manual refresh. Polls only exist while
    /// online — LibraryView stops them on every non-online transition
    /// (spec §3: offline performs no automatic retry).
    func startPolling() {
        guard online?.isOnline != false else { return }
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

    /// Suspend the tree poll — spec §3 offline pause / spec §7 resume.
    func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
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

    /// Browse surface — while offline this is `tree` plus synthesized
    /// nodes for download records missing from the snapshot (spec §8's
    /// works-record union; web `_mergeOfflineTree`). Online it is `tree`.
    private var displayTree: [TreeNode] {
        guard offlineMode, let recs = offline?.records else { return tree }
        var merged = tree
        for (_, rec) in recs where rec.cachedCount > 0 && !rec.folder.isEmpty {
            ensureSynthPath(&merged, rec)
        }
        return merged
    }

    /// Web `_ensureSynthPath`: materialize one folder chain from a record
    /// path. Level matching compares COMPOSED paths (parent + "/" + name)
    /// so flattened `a/b` node names cannot be confused for depth. The
    /// synthesized leaf gets image children from `rec.names`, which makes
    /// every leaf predicate (`hasOnlyImages`, `drillsDown`, `leafPaths`)
    /// work without a flag. Leaf-ancestor guard: a record whose path
    /// passes through an existing leaf node is skipped — synthesizing
    /// inside it would turn the leaf mixed and double-render rows.
    private func ensureSynthPath(_ nodes: inout [TreeNode],
                                 _ rec: OfflineStore.LeafRecord,
                                 prefix: String = "") {
        let segs = rec.folder.split(separator: "/").map(String.init)
        guard !segs.isEmpty else { return }
        func step(_ level: inout [TreeNode], _ i: Int, _ acc: String) {
            guard i < segs.count else { return }
            let full = acc.isEmpty ? segs[i] : acc + "/" + segs[i]
            var idx = level.firstIndex {
                $0.isFolder && BrowseModel.fullPath(acc, $0.name) == full
            }
            if idx == nil {
                var n = TreeNode(name: segs[i], type: "folder", children: [])
                if i == segs.count - 1 {
                    n.children = rec.names.map { TreeNode(name: $0, type: "image") }
                    n.thumb = rec.thumb
                }
                level.append(n)
                idx = level.count - 1
            }
            guard let j = idx else { return }
            if BrowseModel.hasOnlyImages(level[j]) { return }   // leaf guard
            if level[j].children == nil { level[j].children = [] }
            step(&level[j].children!, i + 1, full)
        }
        step(&nodes, 0, prefix)
    }

    func node(at path: String) -> TreeNode? {
        BrowseModel.node(at: path, in: displayTree)
    }

    // MARK: - Offline predicates (spec §6 — web app.js parity)

    /// Any usable download record at all.
    func hasAnyDownload() -> Bool {
        offline?.records.values.contains { $0.cachedCount > 0 } ?? false
    }

    /// `path`'s subtree (incl. its own record) holds a download.
    func subtreeHasDownload(_ path: String) -> Bool {
        guard let recs = offline?.records, !path.isEmpty else { return false }
        return recs.contains { $0.value.cachedCount > 0
            && ($0.key == path || $0.key.hasPrefix(path + "/")) }
    }

    /// A leaf's own record is present and non-empty (web `_isReadablePath`).
    func readable(_ leaf: String) -> Bool {
        (offline?.records[leaf]?.cachedCount ?? 0) > 0
    }

    /// `_offlineWorkReadable`: EVERY leaf path of the work must carry a
    /// usable record. `""` (root loose images) maps to `[""]` — web
    /// `computeLeafPathsFor("")` finds no node and falls back likewise.
    func workReadable(_ path: String) -> Bool {
        var leaves = path.isEmpty ? [""] : leafPaths(under: path)
        if leaves.isEmpty { leaves = [path] }
        return leaves.allSatisfy(readable)
    }

    /// spec §6 tile predicate: grey when offline/connecting and the tile's
    /// own leaf record is absent (mixed self-tiles judge their own record).
    func tileUnavailable(path: String, episodeLike: Bool) -> Bool {
        offlineMode && episodeLike && !readable(path)
    }

    /// Folder entries for a browse level. Visibility is judged per full
    /// path via `isVisible` (fail-closed, ancestor matches filtered too) —
    /// same predicate every web renderer uses. While offline/connecting
    /// the spec §6 uniform rule applies: intermediate folders only when
    /// their subtree holds downloads, episodes only when this level does.
    func levelFolders(path: String) -> [(path: String, node: TreeNode)] {
        var entries = BrowseModel.folders(at: path, in: displayTree)
        if let vis = visibility, !vis.showAll {
            entries = entries.filter { vis.isVisible($0.path) }
        }
        guard offlineMode else { return entries }
        let levelHasDl = path.isEmpty ? hasAnyDownload() : subtreeHasDownload(path)
        return entries.filter {
            BrowseModel.hasOnlyImages($0.node) ? levelHasDl
                                               : subtreeHasDownload($0.path)
        }
    }

    /// Self/read tiles for a level (mixed children, or root loose images).
    /// Offline they follow the same node rule as their folder entry
    /// (subtree download); a `""` record can surface the root self tile
    /// even when the snapshot lost the loose images.
    func selfTiles(path: String) -> [(path: String, node: TreeNode)] {
        var tiles = BrowseModel.selfTiles(at: path, in: displayTree)
        if let vis = visibility, !vis.showAll {
            tiles = tiles.filter { $0.path.isEmpty || vis.isVisible($0.path) }
        }
        guard offlineMode else { return tiles }
        let keep = Set(levelFolders(path: path).map(\.path))
        tiles = tiles.filter { $0.path.isEmpty || keep.contains($0.path) }
        if path.isEmpty, readable(""),
           !tiles.contains(where: { $0.path.isEmpty }),
           let rec = offline?.records[""] {
            tiles.append(("", TreeNode(name: "", type: "folder",
                children: rec.names.map { TreeNode(name: $0, type: "image") })))
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
        let start = path.isEmpty ? displayTree : (node(at: path)?.children ?? [])
        if path.isEmpty {
            // root itself may directly hold images
            if displayTree.contains(where: { $0.isImage }) { out.append("") }
        } else if let n = node(at: path), BrowseModel.hasImages(n) {
            out.append(path)
        }
        walk(start, path)
        return out
    }
}
