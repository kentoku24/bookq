import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var offline: OfflineStore
    @Environment(\.dismiss) private var dismiss
    @State private var version: String = ""
    @State private var urlDraft = ""

    var body: some View {
        NavigationStack {
            Form {
                Section("サーバー") {
                    TextField("https://…", text: $urlDraft)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    if urlDraft != model.config.serverURLString {
                        Button("接続先を変更") {
                            model.config.serverURLString = urlDraft
                            model.serverURLChanged()
                            dismiss()
                        }
                    }
                    Toggle("原本を取得（/image）", isOn: Binding(
                        get: { model.config.originalImages },
                        set: { model.config.originalImages = $0 }))
                }
                Section("アカウント") {
                    if let user = model.auth.user {
                        LabeledContent("ユーザー", value: user.name)
                        LabeledContent("メール", value: user.email)
                    }
                    if model.auth.token != nil {
                        Button("ログアウト", role: .destructive) {
                            model.logout()
                            dismiss()
                        }
                    }
                }
                Section("情報") {
                    LabeledContent("サーバーバージョン", value: version)
                    LabeledContent("画像キャッシュ", value: cacheText)
                    LabeledContent("オフライン保存", value: offlineText)
                    Button("画像キャッシュをクリア") {
                        RemoteImageLoader.shared.clear()
                        URLCache.shared.removeAllCachedResponses()
                    }
                }
            }
            .navigationTitle("設定")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("完了") { dismiss() }
                }
            }
        }
        .onAppear {
            urlDraft = model.config.serverURLString
            Task {
                version = (try? await model.api?.version().version) ?? "不明"
            }
        }
    }

    private var cacheText: String {
        let m = URLCache.shared
        return "\(m.currentDiskUsage / 1024 / 1024) MB"
    }

    private var offlineText: String {
        let mb = Double(offline.totalBytes) / 1_000_000
        let size = mb >= 1000
            ? String(format: "%.1f GB", mb / 1000)
            : String(format: "%.0f MB", mb)
        return "\(offline.workCount) 作品・\(size)"
    }
}
