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
                .preferredColorScheme(.dark)
        }
    }
}
