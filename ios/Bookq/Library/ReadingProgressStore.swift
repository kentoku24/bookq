import Combine
import Foundation

/// Local reading positions — `bookq_progress` equivalent (user-defaults
/// JSON, cap 50 newest). Wire-format mirrors the web: {page,total,ts} per
/// path. `ProgressSync` handles the single remote slot.
@MainActor
final class ReadingProgressStore: ObservableObject {
    struct Entry: Codable, Equatable {
        var page: Int
        var total: Int
        var ts: Int            // ms
        var title: String?     // UI-only label (not on the wire)
    }

    @Published private(set) var entries: [String: Entry] = [:]

    private let key = "bookq_progress_v1"
    private(set) var sync: ProgressSync?

    init() { load() }

    /// Attach the server transport (called after configureAPI).
    func attach(_ sync: ProgressSync?) { self.sync = sync }

    func load() {
        guard let data = UserDefaults.standard.data(forKey: key),
              let decoded = try? JSONDecoder().decode([String: Entry].self, from: data) else {
            entries = [:]
            return
        }
        entries = decoded
    }

    func save() {
        if let data = try? JSONEncoder().encode(entries) {
            UserDefaults.standard.set(data, forKey: key)
        }
    }

    private var nowMs: Int { Int(Date().timeIntervalSince1970 * 1000) }

    /// Record a position locally + push to the server (web `save()`):
    /// debounced by callers, writes ts=now, pushes {folder,page,total,ts}.
    /// Same-position re-saves (close/inactive flush without a page move)
    /// are no-ops — a fresh ts on a stale page would clobber a newer
    /// remote position from another device.
    func record(path: String, page: Int, total: Int, title: String? = nil) {
        if let cur = entries[path], cur.page == page, cur.total == total {
            if let title, title != cur.title {
                entries[path]?.title = title
                save()
            }
            return
        }
        let ts = nowMs
        store(path, page: page, total: total, ts: ts, title: title)
        sync?.push(.init(folder: path, page: page, total: total, ts: ts))
    }

    /// Adopt a server-pulled position: same write, remote ts verbatim,
    /// never re-pushes (no echo loop).
    func mergeRemote(path: String, page: Int, total: Int, ts: Int) {
        store(path, page: page, total: total, ts: ts, title: nil)
    }

    private func store(_ path: String, page: Int, total: Int, ts: Int, title: String?) {
        entries[path] = Entry(page: page, total: total, ts: ts, title: title)
        // cap 50 newest by ts (web behavior)
        if entries.count > 50 {
            let sorted = entries.keys.sorted { (entries[$0]?.ts ?? 0) > (entries[$1]?.ts ?? 0) }
            for k in sorted.dropFirst(50) { entries.removeValue(forKey: k) }
        }
        save()
    }

    /// Most recently read entry — drives the 続きから pill.
    var lastRead: (path: String, entry: Entry)? {
        entries.max(by: { $0.value.ts < $1.value.ts })
            .map { ($0.key, $0.value) }
    }

    /// Progress ratio for a work: considers the work itself and any leaf
    /// descendants (a parent's progress = max descendant ratio).
    func progress(for path: String, leaves: [String]) -> Double {
        var best = 0.0
        for leaf in leaves {
            guard let e = entries[leaf], e.total > 0 else { continue }
            best = max(best, Double(e.page + 1) / Double(e.total))
        }
        return best
    }

    /// Boot-time remote merge (`_syncRemoteProgressRun` in app.js):
    /// pull → adopt remote if newer for its folder → push local newest if
    /// it beats remote. Pull-fail does nothing (never clobber remote).
    func syncWithServer() async {
        guard let sync else { return }
        let (ok, pos) = await sync.pull()
        guard ok else { return }
        if let pos {
            let local = entries[pos.folder]
            if local == nil || local!.ts < pos.ts {
                mergeRemote(path: pos.folder, page: pos.page,
                            total: pos.total, ts: pos.ts)
            }
        }
        if let newest = entries.max(by: { $0.value.ts < $1.value.ts }),
           newest.value.ts > (pos?.ts ?? 0) {
            sync.push(.init(folder: newest.key, page: newest.value.page,
                            total: newest.value.total, ts: newest.value.ts))
        }
    }
}
