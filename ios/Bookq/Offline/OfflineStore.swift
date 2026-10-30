import Combine
import Foundation
import UIKit

/// Per-work offline download store — `offline-core.js` + `storeAdapter.js`
/// equivalent. One record per leaf folder; page files live under
/// `Documents/BookqOffline/<enc(folder)>/<name>`.
@MainActor
final class OfflineStore: ObservableObject {
    struct LeafRecord: Codable, Equatable {
        var folder: String
        var pageCount: Int
        var cachedCount: Int
        var bytes: Int64
        var thumb: String?
        var names: [String]            // full listing captured at download
        var addedAt: Int64             // ms
        var lastReadAt: Int64          // ms
    }

    /// `OfflineBtnState` equivalent.
    enum BtnState: String {
        case idle, downloading, stopping, partial, complete
        case confirmDelete = "confirm-delete"
        case deleting, listing
    }

    @Published private(set) var records: [String: LeafRecord] = [:]
    @Published private(set) var downloading: Set<String> = []
    @Published private(set) var stopping: Set<String> = []
    @Published private(set) var listing: Set<String> = []
    @Published private(set) var deleting: Set<String> = []
    @Published private(set) var confirming: Set<String> = []
    @Published private(set) var progressPct: [String: Double] = [:]
    /// Lightweight toast surface (DL完了/DL失敗/容量不足).
    @Published private(set) var toast: String?

    private let apiProvider: () -> BookqAPI?
    private let originalImages: () -> Bool
    private var api: BookqAPI? { apiProvider() }
    private var tasks: [String: Task<Void, Never>] = [:]
    private var stopRequested: Set<String> = []
    private var toastTask: Task<Void, Never>?

    private let fm = FileManager.default
    private let root: URL

    /// `apiProvider` is resolved per call so the store survives API-client
    /// rebuilds (server URL change / re-login).
    init(apiProvider: @escaping () -> BookqAPI?,
         originalImages: @escaping () -> Bool = { false },
         root: URL? = nil) {
        self.apiProvider = apiProvider
        self.originalImages = originalImages
        self.root = root ?? fm.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("BookqOffline", isDirectory: true)
        load()
        pruneOrphans()
    }

    // MARK: - Paths

    /// Deterministic per-leaf directory. "" (root loose images) → "__root__".
    func dirURL(for leaf: String) -> URL {
        var cs = CharacterSet.alphanumerics
        cs.insert(charactersIn: "-_.")
        let enc = leaf.isEmpty
            ? "__root__"
            : leaf.addingPercentEncoding(withAllowedCharacters: cs) ?? "__root__"
        return root.appendingPathComponent(enc, isDirectory: true)
    }

    func fileURL(folder: String, name: String) -> URL {
        dirURL(for: folder).appendingPathComponent(name)
    }

    /// On-disk copy for a downloaded page, if present.
    func localFileURL(folder: String, name: String) -> URL? {
        let f = fileURL(folder: folder, name: name)
        return fm.fileExists(atPath: f.path) ? f : nil
    }

    /// Map any remote image URL (`/image/`, `/view/`, `/thumb/`) back to a
    /// downloaded page file — the SW cache-first equivalent.
    func localFileURL(remoteURL url: URL) -> URL? {
        var path = url.path
        for prefix in ["/view/", "/image/", "/thumb/"] where path.hasPrefix(prefix) {
            path.removeFirst(prefix.count)
            guard let rel = path.removingPercentEncoding else { return nil }
            let name = (rel as NSString).lastPathComponent
            let folder = (rel as NSString).deletingLastPathComponent
            return localFileURL(folder: folder, name: name)
        }
        return nil
    }

    // MARK: - Records / persistence

    private var recordsURL: URL { root.appendingPathComponent("works.json") }

    private func load() {
        guard let data = try? Data(contentsOf: recordsURL),
              let decoded = try? JSONDecoder().decode([String: LeafRecord].self, from: data) else {
            records = [:]; return
        }
        records = decoded
    }

    private func persist() {
        try? fm.createDirectory(at: root, withIntermediateDirectories: true)
        if let data = try? JSONEncoder().encode(records) {
            try? data.write(to: recordsURL, options: .atomic)
        }
    }

    /// Drop records whose directory vanished (files deleted externally).
    private func pruneOrphans() {
        var dirty = false
        for (k, rec) in records where !fm.fileExists(atPath: dirURL(for: k).path)
            || rec.cachedCount <= 0 {
            if fm.fileExists(atPath: dirURL(for: k).path) && rec.cachedCount > 0 {
                continue
            }
            records.removeValue(forKey: k); dirty = true
        }
        if dirty { persist() }
    }

    var totalBytes: Int64 { records.values.reduce(0) { $0 + $1.bytes } }
    var workCount: Int { records.count }

    private func nowMs() -> Int64 { Int64(Date().timeIntervalSince1970 * 1000) }

    /// Test hook — inject a record without a download.
    func testPut(_ rec: LeafRecord) {
        records[rec.folder] = rec
        persist()
    }

    // MARK: - State machine (getOfflineBtnState + isFolderOffline + partial)

    /// Leaf-most leaves — drop leaves that are ancestors of other leaves
    /// (`_leafMost`: every path prefix ending at a "/" is an ancestor).
    private func leafMost(_ leaves: [String]) -> [String] {
        var ancestors: Set<String> = []
        for lp in leaves {
            var from = lp.startIndex
            while let i = lp[from...].firstIndex(of: "/") {
                ancestors.insert(String(lp[lp.startIndex..<i]))
                from = lp.index(after: i)
            }
        }
        return leaves.filter { !ancestors.contains($0) }
    }

    func partialProgress(leaves: [String]) -> (cached: Int, total: Int, pct: Double) {
        var cached = 0, total = 0, cachedLeaves = 0
        for lp in leafMost(leaves) {
            if let rec = records[lp] {
                cached += rec.cachedCount
                total += rec.pageCount
                if rec.cachedCount > 0 { cachedLeaves += 1 }
            }
        }
        if total == 0 {
            return (cachedLeaves, leaves.count,
                    leaves.isEmpty ? 0 : Double(cachedLeaves) / Double(leaves.count))
        }
        return (cached, total, Double(cached) / Double(total))
    }

    func isFolderOffline(_ path: String, leaves: [String]) -> Bool {
        if records[path] != nil { return true }
        if leaves.count > 1 || (leaves.count == 1 && leaves[0] != path) {
            return !leaves.isEmpty && leaves.allSatisfy { records[$0] != nil }
        }
        return false
    }

    func buttonState(_ path: String, leaves: [String]) -> BtnState {
        if confirming.contains(path) { return .confirmDelete }
        if deleting.contains(path) { return .deleting }
        if stopping.contains(path) { return .stopping }
        if downloading.contains(path) {
            return listing.contains(path) ? .listing : .downloading
        }
        let p = partialProgress(leaves: leaves)
        if p.pct > 0 && p.pct < 1 { return .partial }
        if isFolderOffline(path, leaves: leaves) { return .complete }
        return .idle
    }

    // MARK: - Download

    /// `downloadWork` equivalent. Sequential per leaf and per file; abort
    /// keeps what landed as a partial record.
    func download(path: String, leaves: [String]) {
        guard tasks[path] == nil, api != nil else { return }
        downloading.insert(path)
        listing.insert(path)
        stopRequested.remove(path)
        showToast(nil)
        tasks[path] = Task { await self.runDownload(path: path, leaves: leaves) }
    }

    /// Tap while downloading → abort, keep partial (web single-tap stop).
    func stop(path: String) {
        guard downloading.contains(path) else { return }
        stopping.insert(path)
        stopRequested.insert(path)
        tasks[path]?.cancel()
    }

    private func checkAbort(_ path: String) throws {
        if Task.isCancelled || stopRequested.contains(path) {
            throw CancellationError()
        }
    }

    private func runDownload(path: String, leaves: [String]) async {
        defer {
            tasks[path] = nil
            downloading.remove(path)
            stopping.remove(path)
            listing.remove(path)
            progressPct[path] = nil
        }
        var leafFiles: [String: [String]] = [:]
        var totalFiles = 0
        do {
            // Listing phase
            for leaf in leaves {
                try checkAbort(path)
                guard let api else { throw CancellationError() }
                let names = try await api.images(folder: leaf)
                leafFiles[leaf] = names
                totalFiles += names.count
            }
            listing.remove(path)
            if totalFiles == 0 { showToast("DL失敗: 画像が見つかりません"); return }

            var done = 0
            for leaf in leaves {
                guard let names = leafFiles[leaf], !names.isEmpty else { continue }
                try fm.createDirectory(at: dirURL(for: leaf),
                                       withIntermediateDirectories: true)
                var bytes: Int64 = 0
                var cached = 0
                do {
                    for (i, name) in names.enumerated() {
                        try checkAbort(path)
                        let dest = fileURL(folder: leaf, name: name)
                        if fm.fileExists(atPath: dest.path) {
                            bytes += (try? dest.resourceValues(forKeys: [.fileSizeKey])
                                        .fileSize).map(Int64.init) ?? 0
                        } else {
                            guard let api else { throw CancellationError() }
                            let url = api.pageURL(folder: leaf, name: name,
                                                  original: originalImages())
                            let data = try await api.imageData(url: url)
                            try data.write(to: dest, options: .atomic)
                            bytes += Int64(data.count)
                        }
                        cached += 1
                        done += 1
                        progressPct[path] = Double(done) / Double(totalFiles)
                        if leaf != path {
                            progressPct[leaf] = Double(i + 1) / Double(names.count)
                        }
                    }
                    putRecord(leaf: leaf, pageCount: names.count, cached: cached,
                              bytes: bytes, thumb: names.first, names: names)
                } catch let e as CancellationError {
                    if cached > 0 {
                        putRecord(leaf: leaf, pageCount: names.count, cached: cached,
                                  bytes: bytes, thumb: names.first, names: names)
                    }
                    throw e
                }
            }
            showToast("DL完了")
        } catch is CancellationError {
            // aborted — partial records already written
        } catch let e as NSError where e.domain == NSCocoaErrorDomain
                    && e.code == NSFileWriteOutOfSpaceError {
            showToast("容量不足のため中断")
        } catch {
            showToast("DL失敗")
        }
    }

    private func putRecord(leaf: String, pageCount: Int, cached: Int,
                           bytes: Int64, thumb: String?, names: [String]) {
        let old = records[leaf]
        records[leaf] = LeafRecord(
            folder: leaf, pageCount: pageCount, cachedCount: cached,
            bytes: bytes, thumb: thumb, names: names,
            addedAt: old?.addedAt ?? nowMs(), lastReadAt: nowMs())
        persist()
    }

    /// `deleteWork` equivalent — every leaf record + the folder key itself.
    func delete(path: String, leaves: [String]) {
        guard !deleting.contains(path) else { return }
        deleting.insert(path)
        confirming.remove(path)
        let targets = Array(Set(leaves + [path]))
        Task {
            defer { deleting.remove(path); showToast("削除しました") }
            for t in targets {
                records[t] = nil
                try? fm.removeItem(at: dirURL(for: t))
            }
            persist()
        }
    }

    /// Cached image names for offline reading — listing ∩ existing files.
    func downloadedNames(leaf: String) -> [String]? {
        guard let rec = records[leaf] else { return nil }
        let present = rec.names.filter {
            fm.fileExists(atPath: fileURL(folder: leaf, name: $0).path)
        }
        return present.isEmpty ? nil : present
    }

    /// Reader entry — bump lastReadAt for LRU ordering (getOldest parity).
    func touchRead(leaf: String) {
        guard var rec = records[leaf] else { return }
        rec.lastReadAt = nowMs()
        records[leaf] = rec
        persist()
    }

    // MARK: - Confirm-delete (double-tap) + toast

    /// Tap on a COMPLETE button → arm confirm; second tap within 3s deletes.
    /// Mirrors `_confirmMap` + outside-click reset.
    func armConfirm(_ path: String, onSecond: () -> Void) {
        if confirming.contains(path) {
            confirming.remove(path)
            onSecond()
            return
        }
        confirming.insert(path)
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            _ = await MainActor.run { self?.confirming.remove(path) }
        }
    }

    private func showToast(_ msg: String?) {
        toast = msg
        toastTask?.cancel()
        guard msg != nil else { return }
        toastTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            _ = await MainActor.run { self?.toast = nil }
        }
    }
}
