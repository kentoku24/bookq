import Foundation
import SwiftUI

/// `offline/connectivity.js` (docs/offline-spec.md §2) equivalent —
/// a 3-state connectivity machine: `offline` → `connecting` → `online`.
/// Initial state is `offline`; `connect()` runs ONE finite cycle
/// (3 probes × 4 s timeout, 1 s→2 s backoff ≈ 15 s worst case). While
/// `offline` there is NO automatic retry — a new cycle only starts on
/// app foreground (`foregrounded()`), the badge's explicit `retry()`,
/// or `reportFail()` demoting a live `online`.
@MainActor
final class OnlineStatusStore: ObservableObject {
    enum State: String {
        case offline, connecting, online
    }

    @Published private(set) var state: State = .offline

    /// Compat accessor — true only while `.online`.
    var online: Bool { state == .online }
    var isOnline: Bool { state == .online }

    private let apiProvider: () -> BookqAPI?
    private let probeTimeout: TimeInterval
    private let backoffs: [TimeInterval]
    private let maxAttempts: Int
    private var cycle: Task<Bool, Never>?
    /// Cycle generation — a late-finishing older cycle must not clear a
    /// newer cycle's handle or demote its live `.connecting` state.
    private var cycleGen = 0

    /// `probeTimeout`/`backoffs`/`maxAttempts` are injectable so tests can
    /// shrink the ~15 s budget; production keeps spec §4 values.
    init(apiProvider: @escaping () -> BookqAPI?,
         probeTimeout: TimeInterval = 4,
         backoffs: [TimeInterval] = [1, 2],
         maxAttempts: Int = 3) {
        self.apiProvider = apiProvider
        self.probeTimeout = probeTimeout
        self.backoffs = backoffs
        self.maxAttempts = maxAttempts
    }

    /// Boot trigger — the first render is offline-first; start ONE cycle.
    /// No polling: spec §3 retires the old 30 s health loop.
    func start() {
        _ = connect()
    }

    /// Foreground trigger — spec §3: returning to the app re-verifies
    /// (Web `revalidate`/visibilitychange parity). While online it runs a
    /// single silent probe — a dead server demotes via `reportFail` into
    /// the finite cycle; otherwise a fresh cycle starts.
    @discardableResult
    func foregrounded() -> Task<Bool, Never>? {
        if state == .online {
            return Task { [weak self] in
                guard let self else { return true }
                if await self.probe() { return true }
                if let cycle = self.reportFail() { return await cycle.value }
                return self.state == .online
            }
        }
        return connect()
    }

    /// One connection cycle. Re-entry while connecting returns the same
    /// cycle (coalesced); offline→connecting is the only real transition in.
    @discardableResult
    func connect() -> Task<Bool, Never> {
        if state == .connecting, let cycle { return cycle }
        state = .connecting
        cycleGen += 1
        let g = cycleGen
        let t = Task<Bool, Never> { [weak self] in
            guard let self else { return false }
            let ok = await self.runAttempts()
            guard g == self.cycleGen else { return self.state == .online }
            // A reportSuccess may have landed 'online' already — don't
            // demote it.
            if ok { self.state = .online }
            else if self.state == .connecting { self.state = .offline }
            self.cycle = nil
            return self.state == .online
        }
        cycle = t
        return t
    }

    /// Explicit user retry (badge tap) — identical to connect() (spec §3).
    @discardableResult
    func retry() -> Task<Bool, Never> { connect() }

    /// An online request got no response: demote to connecting immediately;
    /// the cycle decides online vs offline (spec §2). Offline stays
    /// offline — no auto-retry; connecting ignores extra failures.
    /// Returns the demotion cycle so callers/tests can await the verdict.
    @discardableResult
    func reportFail() -> Task<Bool, Never>? {
        guard state == .online else { return nil }
        return connect()
    }

    /// A real response proves reachability — even mid-cycle (spec §5).
    func reportSuccess() {
        if state != .online { state = .online }
    }

    private func runAttempts() async -> Bool {
        for attempt in 0..<maxAttempts {
            // reportSuccess already landed online mid-cycle — stop probing.
            if state != .connecting { return true }
            if await probe() { return true }
            if attempt < backoffs.count {
                try? await Task.sleep(
                    nanoseconds: UInt64(backoffs[attempt] * 1_000_000_000))
            }
        }
        return false
    }

    /// One probe — spec §4's 4 s cap so a dead tailnet host cannot stall
    /// the flip to offline.
    private func probe() async -> Bool {
        guard let api = apiProvider() else { return false }
        do {
            try await Self.withTimeout(seconds: probeTimeout) {
                try await api.health(onlineCheck: true)
            }
            return true
        } catch {
            return false
        }
    }

    private struct ProbeTimeout: Error {}

    private static func withTimeout(
        seconds: TimeInterval,
        work: @escaping @Sendable () async throws -> Void
    ) async throws {
        try await withThrowingTaskGroup(of: Void.self) { group in
            group.addTask { try await work() }
            group.addTask {
                try await Task.sleep(
                    nanoseconds: UInt64(seconds * 1_000_000_000))
                try Task.checkCancellation()
                throw ProbeTimeout()
            }
            defer { group.cancelAll() }
            _ = try await group.next()
        }
    }
}

/// `#offline-status` equivalent — a persistent ●/◌/○ pill pinned to the
/// bottom edge of the browse surface (web parks it in the sidebar
/// header; the bottom strip is where native keeps its status pills).
/// While `.offline` the pill is a Button — spec §3's explicit retry.
struct OnlineStatusBadge: View {
    @EnvironmentObject private var online: OnlineStatusStore

    private var label: String {
        switch online.state {
        case .online: return "●オンライン"
        case .connecting: return "◌接続中…"
        case .offline: return "○オフライン"
        }
    }

    private var tint: Color {
        switch online.state {
        case .online: return Color(red: 0.541, green: 0.706, blue: 0.973)   // web #8ab4f8
        case .connecting: return Color(red: 0.608, green: 0.678, blue: 0.749) // muted blue-grey
        case .offline: return Color(red: 0.788, green: 0.635, blue: 0.310)  // web #c9a24f
        }
    }

    var body: some View {
        Group {
            if online.state == .offline {
                Button { _ = online.retry() } label: { pill }
                    .buttonStyle(.plain)
            } else {
                pill
            }
        }
        .accessibilityLabel(label.replacingOccurrences(of: "●", with: "")
            .replacingOccurrences(of: "◌", with: "")
            .replacingOccurrences(of: "○", with: "")
            .replacingOccurrences(of: "…", with: ""))
    }

    private var pill: some View {
        Text(label)
            .font(.caption2)
            .foregroundStyle(tint)
            .padding(.horizontal, 10).padding(.vertical, 5)
            .background(.ultraThinMaterial, in: Capsule())
            .accessibilityLabel(online.online ? "オンライン" : "オフライン")
            .accessibilityIdentifier("onlineBadge")
    }
}
