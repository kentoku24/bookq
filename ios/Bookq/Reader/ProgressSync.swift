import Foundation

/// Server transport for the single latest reading position — Swift port of
/// `app/static/progress/sync.js`. Same wire ({folder,page,total,ts}), same
/// policy: 2s trailing debounce, one in-flight PUT, dedup by
/// "folder|page|total", backoff 4s→8s→…→60s on network/5xx/408/429, drop
/// other 4xx, original-ts preserved on requeue.
@MainActor
final class ProgressSync {
    struct Record: Equatable, Codable {
        var folder: String
        var page: Int
        var total: Int
        var ts: Int            // client ms
    }

    private let api: BookqAPI
    var nowMs: () -> Int = { Int(Date().timeIntervalSince1970 * 1000) }
    /// Test hook — override to avoid real sleeps.
    var sleep: (Double) async -> Void = { secs in
        try? await Task.sleep(nanoseconds: UInt64(secs * 1_000_000_000))
    }

    private var pending: Record?
    private var inFlight = false
    private var timer: Task<Void, Never>?
    private var lastSentKey: String?
    private var retryDelay: Double = 0
    private var epoch = 0

    init(api: BookqAPI) { self.api = api }

    private func key(_ r: Record) -> String { "\(r.folder)|\(r.page)|\(r.total)" }

    /// 408/429 transient; every other 4xx is a definitive rejection.
    static func droppable(_ status: Int) -> Bool {
        status >= 400 && status < 500 && status != 408 && status != 429
    }

    /// Queue a trailing-edge PUT (2s debounce). Never blocks the turn path.
    func push(_ rec: Record) {
        if var p = pending, key(p) == key(rec) {
            if rec.ts < p.ts { p.ts = rec.ts; pending = p }
        } else {
            pending = rec
        }
        // During backoff a timer is already armed — re-arming to 2s would
        // defeat the backoff while the user keeps turning pages offline.
        if retryDelay > 0 && (timer != nil || inFlight) { return }
        armTimer(2)
    }

    /// Send the queued record now (hide/foreground transitions).
    func flush() { sendNow() }

    private func armTimer(_ secs: Double) {
        timer?.cancel()
        let s = secs
        timer = Task { [weak self] in
            await self?.sleep(s)
            guard !Task.isCancelled else { return }
            await MainActor.run { self?.sendNow() }
        }
    }

    private func sendNow() {
        timer?.cancel(); timer = nil
        guard let rec = pending, !inFlight else { return }
        if key(rec) == lastSentKey { pending = nil; return }
        pending = nil
        inFlight = true
        let gen = epoch
        Task {
            let outcome: String
            do {
                _ = try await api.putReadingPosition(ReadingPosition(
                    folder: rec.folder, page: rec.page, total: rec.total, ts: rec.ts))
                lastSentKey = key(rec)
                outcome = "sent"
            } catch APIError.status(let s) where Self.droppable(s) {
                outcome = "dropped"
            } catch {
                outcome = "retry"
            }
            inFlight = false
            guard gen == epoch else { return }
            if outcome != "retry" {
                retryDelay = 0
                if pending != nil { sendNow() }
            } else {
                if pending == nil { pending = rec }
                retryDelay = min((retryDelay == 0 ? 2 : retryDelay) * 2, 60)
                armTimer(retryDelay)
            }
        }
    }

    /// GET the stored position. v1 returns `{positions[]}` newest-first —
    /// web's `latestPositionOf` picks `positions[0]`; same here. nil =
    /// server empty or fetch failed (pull-fail ≠ empty, callers use `ok`).
    func pull() async -> (ok: Bool, position: Record?) {
        do {
            let pos = try await api.readingPositions().first
            guard let pos else { return (true, nil) }
            return (true, Record(folder: pos.folder, page: pos.page,
                                 total: pos.total, ts: pos.ts))
        } catch {
            return (false, nil)
        }
    }

    /// DELETE the record and drop everything that could write back.
    func remove() async -> Bool {
        pending = nil
        lastSentKey = nil
        retryDelay = 0
        epoch += 1
        timer?.cancel(); timer = nil
        do {
            try await api.deleteReadingPosition()
            return true
        } catch {
            return false
        }
    }
}
