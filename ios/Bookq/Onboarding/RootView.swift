import SwiftUI

/// Phase router: onboarding → booting → login / library.
struct RootView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
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
}
