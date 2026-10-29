import Combine
import Foundation

/// Local reading positions — `bookq_progress` equivalent (user-defaults JSON).
/// Server sync (`/api/reading-position`) arrives with the reader phase; the
/// local store already exists so browse can show progress bars + 続きから.
@MainActor
final class ReadingProgressStore: ObservableObject {
    struct Entry: Codable, Equatable {
        var page: Int
        var pages: Int          // total pages in that work at time of read
        var timestamp: TimeInterval
        var title: String?
    }

    @Published private(set) var entries: [String: Entry] = [:]

    private let key = "bookq_progress_v1"

    init() {
        load()
    }

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

    /// Update position; delete when at the last page (web behavior).
    func record(path: String, page: Int, pages: Int, title: String? = nil) {
        if page >= pages - 1 && pages > 1 {
            entries.removeValue(forKey: path)
        } else {
            entries[path] = Entry(page: page, pages: pages,
                                  timestamp: Date().timeIntervalSince1970,
                                  title: title)
        }
        save()
    }

    /// Most recently read entry — drives the 続きから pill.
    var lastRead: (path: String, entry: Entry)? {
        entries.max(by: { $0.value.timestamp < $1.value.timestamp })
            .map { ($0.key, $0.value) }
    }

    /// Progress ratio for a work: considers the work itself and any leaf
    /// descendants (a parent's progress = max descendant ratio).
    func progress(for path: String, leaves: [String]) -> Double {
        var best = 0.0
        for leaf in leaves {
            guard let e = entries[leaf], e.pages > 0 else { continue }
            best = max(best, Double(e.page + 1) / Double(e.pages))
        }
        return best
    }
}
