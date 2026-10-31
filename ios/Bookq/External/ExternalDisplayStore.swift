import SwiftUI
import UIKit

/// External-display projection (iOS 26+ `ExternalNonInteractiveAccessory`):
/// the system presents a scene accessory on an attached display — no manual
/// session management. This store carries the reader snapshot the accessory
/// content renders, plus availability reported by `onAvailabilityChange`.
/// Singleton — the accessory scene cannot receive environment objects, so
/// it reaches the same store via `.shared`.
@MainActor
final class ExternalDisplayStore: ObservableObject {
    static let shared = ExternalDisplayStore()

    /// Snapshot published by the device reader — what the external screen
    /// renders. pageMode is NOT stored: it is derived from the external
    /// viewport with the same rule the device uses.
    struct Presentation {
        var work: ReaderWork
        var currentIdx: Int
        var pageAR: [Int: CGFloat]
        var spreadOffset: Int
        var landscapeSpread: Bool
        var urlFor: (PageRef) -> URL?
    }

    /// A display is attached and the accessory can present — drives
    /// tv-button visibility in the reader chrome.
    @Published private(set) var accessoryAvailable = false
    /// The accessory content is actually on screen.
    @Published private(set) var connected = false
    /// Projection on/off — bound straight into `isEnabled` of the
    /// ExternalNonInteractiveAccessory, so the system mirrors when false.
    @Published var userEnabled = true
    @Published private(set) var presentation: Presentation?

    /// The external UIScreen, captured by the probe inside the accessory
    /// scene — drives the diagnostics readout. Private(set): assigned via
    /// `screenFound` (which also applies the mode).
    private(set) var externalScreen: UIScreen?

    // MARK: - Presentation (ReaderView)

    /// Publish/replace the rendered snapshot. Called on load, every turn,
    /// and when layout inputs change.
    func updatePresentation(_ p: Presentation) {
        presentation = p
    }

    /// Reader dismissed — the accessory declares on that view unregisters
    /// and the display falls back to mirroring.
    func endPresentation() {
        presentation = nil
    }

    // MARK: - Accessory lifecycle

    /// `onAvailabilityChange` from the scene accessory.
    func accessoryAvailabilityChanged(_ available: Bool) {
        accessoryAvailable = available
    }

    /// The probe inside the accessory scene found its UIScreen — apply the
    /// highest-pixel mode and full-panel rendering here (no scene delegate
    /// exists on an accessory scene).
    func screenFound(_ screen: UIScreen) {
        externalScreen = screen
        markAccessoryPresented()
        if let best = Self.bestMode(for: screen) {
            screen.currentMode = best
        }
        screen.overscanCompensation = .none
    }

    /// State half of `screenFound` — separate so tests can drive it
    /// (UIScreen cannot be constructed).
    func markAccessoryPresented() {
        connected = true
    }

    /// Accessory content torn down (display detached, disabled, or the
    /// declaring reader view removed) → back to mirroring.
    func screenLost() {
        externalScreen = nil
        connected = false
    }

    // MARK: - Resolution

    /// Testable mirror of UIScreenMode (which cannot be constructed):
    /// size in points × pixelAspectRatio → native pixels.
    struct ModeRef: Equatable {
        var size: CGSize
        var pixelAspectRatio: CGFloat
        var pixelCount: CGFloat {
            size.width * size.height * pixelAspectRatio * pixelAspectRatio
        }
    }

    /// Index of the mode with the most native pixels (max-by: last wins
    /// ties, deterministic).
    nonisolated static func bestModeIndex(_ modes: [ModeRef]) -> Int? {
        modes.indices.max(by: { modes[$0].pixelCount < modes[$1].pixelCount })
    }

    /// Highest-resolution mode the external screen offers — assigned to
    /// `screen.currentMode` so the panel runs at native 4K (3840×2160)
    /// rather than whatever mode the system happened to pick.
    /// @MainActor because `availableModes` is main-isolated.
    static func bestMode(for screen: UIScreen) -> UIScreenMode? {
        let modes = screen.availableModes
        guard let i = bestModeIndex(modes.map {
            ModeRef(size: $0.size, pixelAspectRatio: $0.pixelAspectRatio)
        }) else { return nil }
        return modes[i]
    }

    /// "3840×2160@1.0  bounds 3840×2160  insets 0,0,0,0" — one glance tells
    /// whether the panel got native 4K or fell back to a scaled mode.
    static func describe(_ screen: UIScreen) -> String {
        let m = screen.currentMode
        let mode = m.map { "\(Int($0.size.width))×\(Int($0.size.height))@\($0.pixelAspectRatio)" } ?? "?"
        let b = screen.bounds
        let i = screen.overscanCompensationInsets
        let fmt = { (v: CGFloat) in String(format: "%.0f", v) }
        return "\(mode)  bounds \(fmt(b.width))×\(fmt(b.height))  " +
               "insets \(fmt(i.top)),\(fmt(i.left)),\(fmt(i.bottom)),\(fmt(i.right))"
    }
}
