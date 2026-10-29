import SwiftUI

/// First-run / reconfigure screen: enter the Tailscale HTTPS URL, probe
/// /api/health, then move on to login or library.
struct ServerURLView: View {
    @EnvironmentObject private var model: AppModel
    @State private var text = ""
    @State private var checking = false
    @State private var error: String?

    var body: some View {
        VStack(spacing: 20) {
            Spacer()
            Image(systemName: "books.vertical.fill")
                .font(.system(size: 56))
                .foregroundStyle(.secondary)
            Text("bookq")
                .font(.largeTitle).bold()
            Text("サーバーの URL を入力\n（Tailscale 経由の https アドレス）")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            TextField("https://bookq.tail-xxx.ts.net", text: $text)
                .textFieldStyle(.roundedBorder)
                .keyboardType(.URL)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .frame(maxWidth: 380)
            Button {
                Task { await check() }
            } label: {
                HStack {
                    if checking { ProgressView().tint(.white) }
                    Text(checking ? "接続確認中…" : "接続する")
                }
                .frame(maxWidth: 280)
            }
            .buttonStyle(.borderedProminent)
            .disabled(checking || text.trimmingCharacters(in: .whitespaces).isEmpty)
            if let msg = model.bootError ?? error {
                Text(msg).font(.footnote).foregroundStyle(.red)
                    .multilineTextAlignment(.center)
            }
            Spacer()
        }
        .padding()
        .onAppear { text = model.config.serverURLString }
    }

    private func check() async {
        model.config.serverURLString = text
        guard let base = model.config.baseURL else {
            error = "URL が不正です"
            return
        }
        checking = true
        defer { checking = false }
        do {
            try await BookqAPI(baseURL: base, tokenProvider: { nil }).health()
            error = nil
            model.serverURLChanged()
        } catch {
            self.error = "接続できません: \(error.localizedDescription)"
        }
    }
}
