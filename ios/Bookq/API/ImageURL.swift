import Foundation

/// Path encoding that mirrors `encodeURIComponent` per segment
/// (`app/static/offline-core.js` `encodePath`). Byte-exact parity with the
/// web client's URLs is required (cache keys / server path matching). The
/// composed path is split on "/" — flattened names like "parent/child"
/// naturally become two segments, exactly as the JS encodePath does.
enum ImageURL {
    /// JS encodeURIComponent character class: everything except
    /// A-Z a-z 0-9 - _ . ! ~ * ' ( ) gets percent-encoded.
    static let unreserved: CharacterSet = {
        var s = CharacterSet.alphanumerics
        s.insert(charactersIn: "-_.!~*'()")
        return s
    }()

    /// Encode one path segment like JS `encodeURIComponent`.
    static func encodeSegment(_ segment: String) -> String {
        segment.addingPercentEncoding(withAllowedCharacters: unreserved) ?? segment
    }

    /// Per-segment encode of a composed path (`/` stays a separator).
    static func encodePath(_ path: String) -> String {
        path.components(separatedBy: "/").map(encodeSegment).joined(separator: "/")
    }
}
