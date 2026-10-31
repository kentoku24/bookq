import SwiftUI
import UIKit

/// Scene delegate for the `windowExternalDisplayNonInteractive` role —
/// builds the fullscreen reader window on the external screen at its
/// highest-resolution mode (native 4K when the panel offers it).
/// `overscanCompensation = .none` uses the full panel: `.insetBounds`
/// shrinks the window into the visible area and leaves a black frame on
/// displays that report overscan. Monitors don't overscan — the only cost
/// is edge clipping on actual overscanned TVs.
final class ExternalDisplaySceneDelegate: UIResponder, UIWindowSceneDelegate {
    var window: UIWindow?

    func scene(_ scene: UIScene, willConnectTo session: UISceneSession,
               options connectionOptions: UIScene.ConnectionOptions) {
        // UIKit invokes scene-delegate callbacks on the main thread; the
        // delegate itself is nonisolated, so re-enter MainActor for the
        // screen/window/store work.
        MainActor.assumeIsolated {
            guard let windowScene = scene as? UIWindowScene else { return }
            let screen = windowScene.screen
            if let best = ExternalDisplayStore.bestMode(for: screen) {
                screen.currentMode = best
            }
            screen.overscanCompensation = .none

            let window = UIWindow(windowScene: windowScene)
            window.rootViewController = UIHostingController(rootView: ExternalDisplayRootView())
            window.isHidden = false
            self.window = window

            ExternalDisplayStore.shared.sceneDidConnect(session)
        }
    }

    func sceneDidDisconnect(_ scene: UIScene) {
        window = nil
        Task { @MainActor in ExternalDisplayStore.shared.sceneDidDisconnect(scene.session) }
    }
}
