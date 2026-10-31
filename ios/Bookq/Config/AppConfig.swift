import Foundation
import SwiftUI

/// User-configurable settings persisted in UserDefaults.
@MainActor
final class AppConfig: ObservableObject {
    /// Tailscale HTTPS URL of the bookq server, e.g. https://bookq.tail-xxx.ts.net
    @AppStorage("serverURL") var serverURLString: String = ""

    /// Page image source. false = `/view/` (server-normalized, decode-fast —
    /// matches the web reader). true = `/image/` (original bytes).
    @AppStorage("originalImages") var originalImages: Bool = false

    /// Browse layout preference.
    @AppStorage("browseLayout") var browseLayout: String = LayoutMode.tile.rawValue

    enum LayoutMode: String, CaseIterable {
        case tile, list, card
    }

    var layoutMode: LayoutMode {
        get { LayoutMode(rawValue: browseLayout) ?? .tile }
        set { browseLayout = newValue.rawValue }
    }

    var baseURL: URL? {
        let s = serverURLString.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !s.isEmpty else { return nil }
        let candidate = s.contains("://") ? s : "https://\(s)"
        guard let url = URL(string: candidate),
              let scheme = url.scheme, scheme.hasPrefix("http"),
              let host = url.host else { return nil }
        // Bearer tokens go out on every request — plaintext http is only
        // acceptable for loopback dev servers, never a remote host.
        if scheme == "http", !Self.isLoopback(host) { return nil }
        return url
    }

    /// localhost / 127.* / ::1 — the only hosts allowed over http.
    static func isLoopback(_ host: String) -> Bool {
        let h = host.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
        return h == "localhost" || h.hasSuffix(".localhost")
            || h.hasPrefix("127.") || h == "::1"
    }
}
