import SwiftUI

/// ペアリング sheet — pairUI.js equivalent: create (code+QR) / join (6桁) /
/// auto-connect (MATCH) / session info / leave. Auto-approve means the
/// leader never sees an accept button.
struct PairSheet: View {
    @EnvironmentObject private var pair: PairStore
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss

    @State private var joinCode = ""
    @State private var busy = false
    @State private var localError: String?

    var body: some View {
        NavigationStack {
            Form {
                statusSection
                switch pair.status {
                case .idle:
                    idleSection
                case .connecting:
                    Section { ProgressView("接続中…") }
                case .waiting:
                    waitingSection
                case .pending:
                    pendingSection
                case .paired:
                    pairedSection
                case .ended(let reason):
                    endedSection(reason)
                }
            }
            .navigationTitle("ペアリング")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("閉じる") { dismiss() }
                }
            }
        }
        .presentationDetents([.medium, .large])
    }

    // MARK: - Sections

    @ViewBuilder
    private var statusSection: some View {
        if let err = localError ?? pair.errorText {
            Section {
                Label(err, systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.red)
            }
        }
    }

    private var idleSection: some View {
        Group {
            Section("新しいセッション") {
                Button {
                    run { try await pair.create() }
                } label: {
                    Label("コードを発行する", systemImage: "qrcode")
                }
                .disabled(busy)
                Button {
                    run { _ = try await pair.match() }
                } label: {
                    Label("自動で接続", systemImage: "bolt.horizontal")
                }
                .disabled(busy)
            }
            Section("コードで参加") {
                TextField("6桁のコード", text: $joinCode)
                    .keyboardType(.numberPad)
                    .onChange(of: joinCode) { _, v in
                        joinCode = String(v.filter(\.isNumber).prefix(6))
                    }
                Button {
                    run { try await pair.join(code: joinCode) }
                } label: {
                    Label("参加する", systemImage: "person.2")
                }
                .disabled(joinCode.count != 6 || busy)
            }
        }
    }

    private var waitingSection: some View {
        Section("このコードを相手に入力") {
            Text(pair.code ?? "------")
                .font(.system(size: 44, weight: .bold, design: .monospaced))
                .frame(maxWidth: .infinity)
                .listRowBackground(Color.clear)
            if let code = pair.code,
               let host = model.config.baseURL?.absoluteString,
               let img = QRImage.image("\(host)/#/join/\(code)") {
                Image(uiImage: img)
                    .interpolation(.none)
                    .resizable()
                    .frame(width: 168, height: 168)
                    .frame(maxWidth: .infinity)
                    .listRowBackground(Color.clear)
            }
            if let jr = pair.joinRequest {
                Label("\(jr.name) が参加を申請中…", systemImage: "person.crop.circle.badge.clock")
            } else {
                Label("参加者を待っています", systemImage: "hourglass")
                    .foregroundStyle(.secondary)
            }
            Button(role: .destructive) { pair.leave(); dismiss() } label: {
                Label("キャンセル", systemImage: "xmark.circle")
            }
        }
    }

    private var pendingSection: some View {
        Section {
            Label("リーダーの承認待ち…", systemImage: "hourglass")
            Button(role: .destructive) { pair.leave(); dismiss() } label: {
                Label("キャンセル", systemImage: "xmark.circle")
            }
        }
    }

    private var pairedSection: some View {
        Group {
            Section("セッション") {
                LabeledContent("あなた", value: pair.role == .leader ? "リーダー" : "フォロワー")
                if let peer = pair.peer {
                    LabeledContent("相手", value: "\(peer.name.isEmpty ? peer.role.rawValue : peer.name)")
                    LabeledContent("状態", value: peerStatusText(peer))
                }
                if let ph = pair.phase {
                    LabeledContent("フェーズ", value: phaseText(ph))
                }
            }
            Section {
                Button { pair.swapSides() } label: {
                    Label("左右を入れ替える", systemImage: "arrow.left.arrow.right")
                }
                .disabled(pair.phase != .reading)
                Button(role: .destructive) { pair.leave(); dismiss() } label: {
                    Label("ペアを解除", systemImage: "link.badge.plus")
                }
            }
        }
    }

    private func endedSection(_ reason: String) -> some View {
        Section {
            Label(endReasonText(reason), systemImage: "info.circle")
            Button("もう一度") {
                localError = nil
                pair.disconnect()
            }
        }
    }

    // MARK: - Labels

    private func peerStatusText(_ p: PairPresenceEntry) -> String {
        switch p.status {
        case "active": return p.ready ? "準備完了" : "接続中"
        case "suspended": return "離席中"
        case "offline": return "オフライン"
        default: return p.status
        }
    }

    private func phaseText(_ ph: PairPhase) -> String {
        switch ph {
        case .waiting: return "待機中"
        case .selecting: return "作品選択中"
        case .loading: return "読み込み中"
        case .reading: return "同期読書中"
        case .paused: return "一時停止"
        }
    }

    private func endReasonText(_ r: String) -> String {
        switch r {
        case "left": return "ペアを解除しました"
        case "rejected": return "参加が拒否されました"
        case "code_expired": return "コードの有効期限が切れました"
        case "resume_expired": return "再接続の期限が切れました"
        case "session_lost": return "セッションが失われました"
        case "resume_in_use": return "別の端末が参加中です"
        case "incompatible": return "プロトコルバージョンが非対応です"
        default: return "終了: \(r)"
        }
    }

    private func run(_ op: @escaping () async throws -> Void) {
        busy = true; localError = nil
        Task {
            do { try await op() }
            catch PairError.rejected(let reason, _) { localError = reason }
            catch { localError = error.localizedDescription }
            await MainActor.run { busy = false }
        }
    }
}
