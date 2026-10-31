import SwiftUI
import UIKit

/// External-display projection (windowExternalDisplayNonInteractive scope):
/// owns the scene-session lifecycle and the reader snapshot the external
/// window renders. Singleton — the scene delegate cannot receive
/// environment objects, so it reaches the same store via `.shared`.
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

    /// A physical screen is attached (or our session is already live) —
    /// drives tv-button visibility in the reader chrome.
    @Published private(set) var available = false
    /// The external scene session is live and rendering.
    @Published private(set) var connected = false
    /// Session-only opt-out via the reader chrome tv button.
    @Published var userEnabled = true {
        didSet { refreshActivation() }
    }
    @Published private(set) var presentation: Presentation?

    /// The live scene session (AnyObject so tests can inject a stand-in —
    /// UISceneSession cannot be constructed).
    var sessionRef: AnyObject?
    private var activationPending = false
    private var destroying = false
    private var observers: [NSObjectProtocol] = []

    // Injectable seams — real UIApplication calls are installed in init.
    var activate: () -> Void = {}
    var deactivate: (AnyObject) -> Void = { _ in }
    /// Attached non-device screens. `UIScreen.screens`/didConnect are
    /// deprecated since iOS 16 but remain the only API that reports a
    /// physically attached display *before* any scene session exists —
    /// `openSessions` only covers already-connected scenes.
    var externalScreenCount: () -> Int = { 0 }

    init() {
        externalScreenCount = { max(UIScreen.screens.count - 1, 0) }
        activate = { [weak self] in
            let req = UISceneSessionActivationRequest(
                role: .windowExternalDisplayNonInteractive)
            UIApplication.shared.activateSceneSession(for: req) { _ in
                Task { @MainActor in self?.activationDidFail() }
            }
        }
        deactivate = { ref in
            guard let session = ref as? UISceneSession else { return }
            UIApplication.shared.requestSceneSessionDestruction(
                session, options: nil) { _ in }
        }
        for name in [UIScreen.didConnectNotification, UIScreen.didDisconnectNotification] {
            observers.append(NotificationCenter.default.addObserver(
                forName: name, object: nil, queue: .main
            ) { [weak self] _ in
                Task { @MainActor in self?.refreshAvailability() }
            })
        }
        refreshAvailability()
    }

    // MARK: - Presentation (ReaderView)

    /// Publish/replace the rendered snapshot. Called on load, every turn,
    /// and when layout inputs change.
    func updatePresentation(_ p: Presentation) {
        presentation = p
        refreshActivation()
    }

    /// Reader dismissed — drop the session; the display falls back to
    /// mirroring the device.
    func endPresentation() {
        presentation = nil
        refreshActivation()
    }

    // MARK: - Scene delegate callbacks

    func sceneDidConnect(_ session: UISceneSession) {
        sessionRef = session
        markConnected()
    }

    func markConnected() {
        activationPending = false
        destroying = false
        connected = true
        available = true
    }

    func sceneDidDisconnect(_ session: UISceneSession) {
        if sessionRef == nil || sessionRef === session { markDisconnected() }
    }

    func markDisconnected() {
        sessionRef = nil
        connected = false
        activationPending = false
        destroying = false
        refreshAvailability()
    }

    // MARK: - Availability / activation state machine

    /// Recompute `available` from physical screens + session liveness, then
    /// reconcile activation. Called on screen (dis)connect notifications.
    func refreshAvailability() {
        available = externalScreenCount() > 0 || connected
        refreshActivation()
    }

    /// want = presenting && userEnabled && screen present. One pending
    /// activation at a time; destruction defers session cleanup to
    /// sceneDidDisconnect so a stale session can't bounce back.
    private func refreshActivation() {
        let want = presentation != nil && userEnabled && available
        if want, sessionRef == nil, !activationPending, !destroying {
            activationPending = true
            activate()
        } else if !want, let session = sessionRef, !destroying {
            destroying = true
            deactivate(session)
        }
    }

    /// Clear the pending flag only — no immediate retry (a persistent
    /// failure would loop). The next publish/state change retries.
    private func activationDidFail() {
        activationPending = false
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
    /// ties, deterministic). Pure — nonisolated so the scene delegate can
    /// call it synchronously during willConnectTo.
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
}
