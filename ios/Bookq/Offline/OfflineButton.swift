import SwiftUI

/// `offlineControlsHtml` equivalent — 44pt circular button with the
/// idle/downloading/partial/complete/confirm-delete state machine.
/// Tap: idle→download, downloading→abort, partial→popover, complete→confirm.
struct OfflineButton: View {
    let path: String
    let leaves: [String]
    @ObservedObject var store: OfflineStore

    @State private var showPartialSheet = false

    private var state: OfflineStore.BtnState {
        store.buttonState(path, leaves: leaves)
    }

    private var pct: Double {
        if let p = store.progressPct[path] { return p }
        return store.partialProgress(leaves: leaves).pct
    }

    var body: some View {
        Button(action: tapped) {
            ZStack {
                Circle()
                    .fill(.black.opacity(0.55))
                    .frame(width: 34, height: 34)
                if state == .downloading || state == .listing {
                    Circle()
                        .trim(from: 0, to: state == .listing ? 0.25 : max(pct, 0.02))
                        .stroke(.white, lineWidth: 2.5)
                        .frame(width: 22, height: 22)
                        .rotationEffect(.degrees(-90))
                    if state == .downloading {
                        // abort affordance — web ■
                        RoundedRectangle(cornerRadius: 1.5)
                            .fill(.white)
                            .frame(width: 8, height: 8)
                    }
                } else {
                    Image(systemName: icon)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(iconColor)
                }
            }
        }
        .buttonStyle(.plain)
        .confirmationDialog(
            "一部ダウンロード済み (\(store.partialProgress(leaves: leaves).cached)/\(store.partialProgress(leaves: leaves).total) ページ)",
            isPresented: $showPartialSheet, titleVisibility: .visible
        ) {
            Button("続きをダウンロード") { store.download(path: path, leaves: leaves) }
            Button("削除 (\(store.partialProgress(leaves: leaves).cached) ページ)",
                   role: .destructive) { store.delete(path: path, leaves: leaves) }
            Button("キャンセル", role: .cancel) {}
        }
        .accessibilityLabel(accessibility)
    }

    private var icon: String {
        switch state {
        case .idle: return "arrow.down"
        case .partial: return "arrow.down.circle"
        case .complete: return "checkmark"
        case .confirmDelete: return "trash.fill"
        case .stopping, .deleting, .listing: return "ellipsis"
        case .downloading: return "stop.fill"
        }
    }

    private var iconColor: Color {
        switch state {
        case .complete: return .green
        case .confirmDelete: return .red
        case .partial: return .yellow
        default: return .white
        }
    }

    private var accessibility: String {
        switch state {
        case .idle: return "ダウンロード"
        case .downloading: return "ダウンロード中（タップで停止）"
        case .partial: return "一部ダウンロード済み"
        case .complete: return "完了（タップで削除確認）"
        case .confirmDelete: return "確認: もう一度タップで削除"
        case .deleting: return "削除中"
        case .stopping: return "停止中"
        case .listing: return "一覧取得中"
        }
    }

    private func tapped() {
        switch state {
        case .idle:
            store.download(path: path, leaves: leaves)
        case .downloading, .listing:
            store.stop(path: path)
        case .partial:
            showPartialSheet = true
        case .complete, .confirmDelete:
            // armConfirm: first tap arms (3s), second tap consumes → delete
            store.armConfirm(path) { store.delete(path: path, leaves: leaves) }
        case .stopping, .deleting:
            break
        }
    }
}
