import AVFoundation
import SwiftUI
import Vision
import VisionKit

/// Scanned-QR → join-code extraction. Accepts the web join link
/// (`<origin>/#/join/<code>` — the payload both pairUI.js and
/// QRImage render) or a bare 6-digit code.
enum PairJoinCode {
    static func extract(_ payload: String) -> String? {
        if let m = payload.range(of: #"/join/(\d{6})(?!\d)"#,
                                 options: .regularExpression) {
            return String(payload[m].suffix(6))
        }
        return payload.range(of: #"^\d{6}$"#, options: .regularExpression) != nil
            ? payload : nil
    }

    /// The payload's origin must equal the configured server — host AND
    /// port (default-port normalized). Two bookq instances can share a
    /// host on different ports (dev pool / local test servers); scheme
    /// is transport, not identity (tailscale-serve https vs direct
    /// http to the same deployment).
    static func sameServer(payload: String, baseURL: URL?) -> Bool {
        guard let src = URL(string: payload), src.host != nil else {
            return true   // bare code — nothing to compare
        }
        guard let mine = baseURL, let mh = mine.host else { return true }
        return src.host!.lowercased() == mh.lowercased()
            && effectivePort(src) == effectivePort(mine)
    }

    private static func effectivePort(_ url: URL) -> Int? {
        if let p = url.port { return p }
        switch url.scheme?.lowercased() {
        case "https": return 443
        case "http": return 80
        case "wss": return 443
        case "ws": return 80
        default: return nil
        }
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
    /// Scanner failure after the camera opened — swaps the (dead)
    /// viewfinder for a readable message instead of a black pane.
    @State private var scanError: String?

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
            if let scanError {
                messageView(scanError, systemImage: "camera.badge.ellipsis")
            } else {
                ScannerRepresentable(
                    onPayload: { onPayload($0) },
                    onUnavailable: { scanError = $0 })
                .ignoresSafeArea()
            }
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
    let onUnavailable: (String) -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(onPayload: onPayload, onUnavailable: onUnavailable)
    }

    func makeUIViewController(context: Context) -> ScannerContainerViewController {
        // `.qr` only — jsQR parity: with all symbologies a stray EAN/UPC
        // in frame fires a payload that fails join-code extraction.
        let scanner = DataScannerViewController(
            recognizedDataTypes: [.barcode(symbologies: [.qr])],
            qualityLevel: .balanced,
            recognizesMultipleItems: false,
            isHighFrameRateTrackingEnabled: false,
            isPinchToZoomEnabled: true,
            isGuidanceEnabled: true,
            isHighlightingEnabled: true)
        scanner.delegate = context.coordinator
        let vc = ScannerContainerViewController(scanner: scanner)
        vc.onStartFailure = { [weak coordinator = context.coordinator] _ in
            coordinator?.onUnavailable("カメラを起動できませんでした")
        }
        return vc
    }

    func updateUIViewController(_: ScannerContainerViewController,
                                context _: Context) {}

    static func dismantleUIViewController(_ vc: ScannerContainerViewController,
                                          coordinator _: Coordinator) {
        vc.scanner.stopScanning()
    }

    /// Child-VC container for the data scanner. `startScanning()` must be
    /// called only once the view is in the window hierarchy (Apple: after
    /// presentation completes); calling it in makeUIViewController races
    /// that requirement and leaves a dead viewfinder. DataScannerView-
    /// Controller is `public`, not `open`, so the hook goes through a
    /// parent VC's viewDidAppear instead of a subclass override.
    final class ScannerContainerViewController: UIViewController {
        let scanner: DataScannerViewController
        var onStartFailure: ((Error) -> Void)?

        init(scanner: DataScannerViewController) {
            self.scanner = scanner
            super.init(nibName: nil, bundle: nil)
        }
        @available(*, unavailable)
        required init?(coder _: NSCoder) { fatalError() }

        override func viewDidLoad() {
            super.viewDidLoad()
            addChild(scanner)
            scanner.view.frame = view.bounds
            scanner.view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
            view.addSubview(scanner.view)
            scanner.didMove(toParent: self)
        }

        override func viewDidAppear(_ animated: Bool) {
            super.viewDidAppear(animated)
            guard !scanner.isScanning else { return }
            do { try scanner.startScanning() }
            catch { onStartFailure?(error) }
        }

        override func viewWillDisappear(_ animated: Bool) {
            if scanner.isScanning { scanner.stopScanning() }
            super.viewWillDisappear(animated)
        }
    }

    final class Coordinator: NSObject, DataScannerViewControllerDelegate {
        let onPayload: (String) -> Void
        let onUnavailable: (String) -> Void
        private var fired = false

        init(onPayload: @escaping (String) -> Void,
             onUnavailable: @escaping (String) -> Void) {
            self.onPayload = onPayload
            self.onUnavailable = onUnavailable
        }

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

        /// Camera lost mid-session (restricted, captured by another app)
        /// — without this the viewfinder just freezes silently.
        func dataScanner(_: DataScannerViewController,
                         becameUnavailableWithError _: DataScannerViewController.ScanningUnavailable) {
            guard !fired else { return }
            fired = true
            onUnavailable("カメラを起動できませんでした")
        }
    }
}
