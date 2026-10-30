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
                .preferredColorScheme(.dark)
        }
    }
}
