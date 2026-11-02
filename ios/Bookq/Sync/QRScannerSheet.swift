import AVFoundation
import SwiftUI
import VisionKit

/// Scanned-QR → join-code extraction. Accepts the web join link
/// (`<origin>/#/join/<code>` — the payload both pairUI.js and
/// QRImage render) or a bare 6-digit code.
enum PairJoinCode {
    static func extract(_ payload: String) -> String? {
        if let m = payload.range(of: #"/join/(\d{6})"#,
                                 options: .regularExpression) {
            return String(payload[m].suffix(6))
        }
        return payload.range(of: #"^\d{6}$"#, options: .regularExpression) != nil
            ? payload : nil
    }
}

/// pairUI.js `pair-scan` equivalent — a live camera sheet that
/// recognizes the leader's join QR. VisionKit's DataScannerViewController
/// does decode; devices without Live Text support fall back to manual
/// code entry (the sheet explains instead of scanning).
struct QRScannerSheet: View {
    /// Called with the raw QR payload — the caller extracts/validates.
    var onPayload: (String) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var auth = AVCaptureDevice.authorizationStatus(for: .video)

    var body: some View {
        NavigationStack {
            content
                .navigationTitle("QRコードで参加")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("閉じる") { dismiss() }
                    }
                }
        }
        .task {
            if auth == .notDetermined {
                auth = await AVCaptureDevice.requestAccess(for: .video)
                    ? .authorized : .denied
            }
        }
    }

    @ViewBuilder
    private var content: some View {
        if !DataScannerViewController.isSupported {
            messageView("この端末はカメラ読み取りに対応していません",
                        systemImage: "qrcode.viewfinder")
        } else if auth == .authorized {
            ScannerRepresentable { payload in
                onPayload(payload)
            }
            .ignoresSafeArea()
        } else if auth == .notDetermined {
            ProgressView("カメラの使用を確認中…")
        } else {
            VStack(spacing: 16) {
                messageView("カメラの使用が許可されていません",
                            systemImage: "camera.badge.ellipsis")
                Button("設定を開く") {
                    if let url = URL(string: UIApplication.openSettingsURLString) {
                        UIApplication.shared.open(url)
                    }
                }
            }
        }
    }

    private func messageView(_ text: String, systemImage: String) -> some View {
        VStack(spacing: 12) {
            Image(systemName: systemImage).font(.largeTitle)
            Text(text).font(.subheadline).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

private struct ScannerRepresentable: UIViewControllerRepresentable {
    let onPayload: (String) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(onPayload: onPayload) }

    func makeUIViewController(context: Context) -> DataScannerViewController {
        let vc = DataScannerViewController(
            recognizedDataTypes: [.barcode()],
            qualityLevel: .balanced,
            recognizesMultipleItems: false,
            isHighFrameRateTrackingEnabled: false,
            isPinchToZoomEnabled: true,
            isGuidanceEnabled: true,
            isHighlightingEnabled: true)
        vc.delegate = context.coordinator
        try? vc.startScanning()
        return vc
    }

    func updateUIViewController(_: DataScannerViewController,
                                context _: Context) {}

    static func dismantleUIViewController(_ vc: DataScannerViewController,
                                          coordinator _: Coordinator) {
        vc.stopScanning()
    }

    final class Coordinator: NSObject, DataScannerViewControllerDelegate {
        let onPayload: (String) -> Void
        private var fired = false

        init(onPayload: @escaping (String) -> Void) { self.onPayload = onPayload }

        func dataScanner(_ scanner: DataScannerViewController,
                         didAdd addedItems: [RecognizedItem],
                         allItems: [RecognizedItem]) {
            guard !fired else { return }
            for item in addedItems {
                guard case .barcode(let b) = item,
                      let s = b.observation.payloadStringValue else { continue }
                fired = true
                scanner.stopScanning()
                onPayload(s)
                return
            }
        }
    }
}
