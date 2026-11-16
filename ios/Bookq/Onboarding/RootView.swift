import SwiftUI

/// Phase router: onboarding → booting → login / library.
struct RootView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        Group {
            switch model.phase {
            case .onboarding:
                ServerURLView()
            case .booting:
                ProgressView("接続中…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .background(.background)
            case .login:
                LoginView()
            case .ready:
                LibraryView()
            }
        }
        // web 'online' window-event equivalent — re-probe on foreground.
        // `model.foregrounded()` is the session keepalive: it exercises
        // the refresh token so a server restart's wiped refresh store
        // surfaces as sessionExpired → .login at resume, not dead pages.
        .onChange(of: scenePhase) { _, ph in
            if ph == .active {
                model.online.foregrounded()
                model.foregrounded()
            }
        }
    }
}
