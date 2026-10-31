import SwiftUI
import UIKit

/// Scene delegate for the `windowExternalDisplayNonInteractive` role —
/// builds the fullscreen reader window on the external screen at its
/// highest-resolution mode (native 4K when the panel offers it).
/// `overscanCompensation = .insetBounds` keeps content inside the visible
/// area on overscanned TVs; it is a no-op on regular monitors.
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
            screen.overscanCompensation = .insetBounds

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
