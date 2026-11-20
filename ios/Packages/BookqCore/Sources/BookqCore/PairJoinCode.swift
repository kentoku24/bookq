import Foundation

/// Scanned-QR → join-code extraction. Accepts the web join link
/// (`<origin>/#/join/<code>` — the payload both pairUI.js and
/// QRImage render) or a bare 6-digit code.
///
/// Moved verbatim from `ios/Bookq/Sync/QRScannerSheet.swift` — the sheet
/// keeps the camera UI, the pure parsing lives here and is unit-tested
/// without a simulator.
public enum PairJoinCode {
    public static func extract(_ payload: String) -> String? {
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
    public static func sameServer(payload: String, baseURL: URL?) -> Bool {
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
