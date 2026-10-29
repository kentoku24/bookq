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
              url.host != nil else { return nil }
        return url
    }
}
