import Foundation
import SwiftUI

/// `updateOnlineStatus` (app.js) equivalent — probes
/// `/api/v1/health?online_check=1` and drives the persistent
/// ●オンライン/○オフライン browse indicator. Polls on an interval;
/// `foregrounded()` is the window online/offline-event trigger.
@MainActor
final class OnlineStatusStore: ObservableObject {
    @Published private(set) var online = true

    private let apiProvider: () -> BookqAPI?
    private let interval: TimeInterval
    private var loop: Task<Void, Never>?

    init(apiProvider: @escaping () -> BookqAPI?,
         interval: TimeInterval = 30) {
        self.apiProvider = apiProvider
        self.interval = interval
    }

    /// Idempotent — called when the library mounts.
    func start() {
        guard loop == nil else { return }
        loop = Task { [weak self] in
            while !Task.isCancelled {
                await self?.check()
                try? await Task.sleep(
                    nanoseconds: UInt64((self?.interval ?? 30) * 1_000_000_000))
            }
        }
    }

    /// Foreground trigger — web's 'online'/'offline' event parity.
    func foregrounded() {
        Task { await check() }
    }

    /// One probe — 8 s cap so a dead tailnet host cannot stall the flip
    /// to offline (same failure class as the boot probe's 15 s guard).
    func check() async {
        guard let api = apiProvider() else { online = false; return }
        do {
            try await Self.withTimeout(seconds: 8) {
                try await api.health(onlineCheck: true)
            }
            online = true
        } catch {
            online = false
        }
    }

    private struct ProbeTimeout: Error {}

    private static func withTimeout(
        seconds: UInt64,
        work: @escaping @Sendable () async throws -> Void
    ) async throws {
        try await withThrowingTaskGroup(of: Void.self) { group in
            group.addTask { try await work() }
            group.addTask {
                try await Task.sleep(nanoseconds: seconds * 1_000_000_000)
                try Task.checkCancellation()
                throw ProbeTimeout()
            }
            defer { group.cancelAll() }
            _ = try await group.next()
        }
    }
}

/// `#offline-status` equivalent — a persistent ●/○ pill pinned to the
/// bottom edge of the browse surface (web parks it in the sidebar
/// header; the bottom strip is where native keeps its status pills).
struct OnlineStatusBadge: View {
    @EnvironmentObject private var online: OnlineStatusStore

    var body: some View {
        Text(online.online ? "●オンライン" : "○オフライン")
            .font(.caption2)
            .foregroundStyle(online.online
                ? Color(red: 0.541, green: 0.706, blue: 0.973)   // web #8ab4f8
                : Color(red: 0.788, green: 0.635, blue: 0.310))  // web #c9a24f
            .padding(.horizontal, 10).padding(.vertical, 5)
            .background(.ultraThinMaterial, in: Capsule())
            .accessibilityLabel(online.online ? "オンライン" : "オフライン")
    }
}
