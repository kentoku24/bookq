import SwiftUI

/// Login gate shown when the server requires auth (non-TEST_MODE).
struct LoginView: View {
    @EnvironmentObject private var model: AppModel
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        VStack(spacing: 24) {
            Spacer()
            Image(systemName: "books.vertical.fill")
                .font(.system(size: 64))
                .foregroundStyle(.secondary)
            Text("bookq")
                .font(.largeTitle).bold()
            Text("Google アカウントでログイン")
                .foregroundStyle(.secondary)
            Button {
                Task { await login() }
            } label: {
                HStack {
                    if busy { ProgressView().tint(.white) }
                    Text(busy ? "ログイン中…" : "Google でログイン")
                }
                .frame(maxWidth: 280)
            }
            .buttonStyle(.borderedProminent)
            .disabled(busy)
            if let error {
                Text(error).font(.footnote).foregroundStyle(.red)
                    .multilineTextAlignment(.center)
            }
            Spacer()
        }
        .padding()
    }

    private func login() async {
        guard let base = model.config.baseURL else { return }
        busy = true
        defer { busy = false }
        do {
            try await model.auth.login(baseURL: base)
            model.didLogin()
        } catch AuthManager.AuthError.cancelled {
            // silent
        } catch {
            self.error = error.localizedDescription
        }
    }
}
