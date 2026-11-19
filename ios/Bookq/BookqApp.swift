import SwiftUI

@main
struct BookqApp: App {
    @StateObject private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(model)
                .environmentObject(model.progress)
                .environmentObject(model.offline)
                .environmentObject(model.visibility)
                .environmentObject(model.search)
                .environmentObject(model.pair)
                .environmentObject(model.external)
                .environmentObject(model.online)
                .preferredColorScheme(.dark)
                .onOpenURL { model.handleDeepLink($0) }
        }
    }
}
