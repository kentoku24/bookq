import CryptoKit
import Foundation

/// Canonical JSON serialization matching Python's
/// `json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",",":"))`
/// — the byte-exact encoding `manifest_digest` is computed over
/// (app/reader_sync/catalog.py). Rules:
///   - object keys sorted by code point (ASCII keys in practice)
///   - compact separators, no whitespace
///   - `"` `\` and control chars escaped (\b \f \n \r \t short forms)
///   - every char outside 0x20...0x7E emitted as `\uXXXX` per UTF-16 code
///     unit — astral chars become surrogate-pair escapes, lowercase hex
enum CanonicalJSON {

    /// Serialize a JSONSerialization-parsed value canonically.
    static func encode(_ value: Any) -> String {
        var out = ""
        write(value, into: &out)
        return out
    }

    private static func write(_ v: Any, into out: inout String) {
        switch v {
        case is NSNull:
            out += "null"
        case let n as NSNumber:
            // CFBoolean check first: NSNumber(1) conditionally bridges to
            // Bool and would corrupt the digest ("offset":true).
            if CFGetTypeID(n) == CFBooleanGetTypeID() {
                out += n.boolValue ? "true" : "false"
                break
            }
            let d = n.doubleValue
            if d == d.rounded() && abs(d) < 1e15 {
                out += String(Int64(d))
            } else {
                out += String(d)
            }
        case let b as Bool:
            out += b ? "true" : "false"
        case let s as String:
            writeString(s, into: &out)
        case let a as [Any]:
            out += "["
            for (i, e) in a.enumerated() {
                if i > 0 { out += "," }
                write(e, into: &out)
            }
            out += "]"
        case let d as [String: Any]:
            out += "{"
            for (i, k) in d.keys.sorted().enumerated() {
                if i > 0 { out += "," }
                writeString(k, into: &out)
                out += ":"
                write(d[k] as Any, into: &out)
            }
            out += "}"
        default:
            // JSONSerialization dictionaries arrive as NSDictionary
            if let d = v as? NSDictionary {
                var mapped: [String: Any] = [:]
                for (k, val) in d {
                    if let ks = k as? String { mapped[ks] = val }
                }
                write(mapped, into: &out)
            } else if let a = v as? NSArray {
                write(a.map { $0 }, into: &out)
            } else {
                out += "null"
            }
        }
    }

    private static func writeString(_ s: String, into out: inout String) {
        out += "\""
        for unit in s.utf16 {
            switch unit {
            case 0x22: out += "\\\""
            case 0x5C: out += "\\\\"
            case 0x08: out += "\\b"
            case 0x0C: out += "\\f"
            case 0x0A: out += "\\n"
            case 0x0D: out += "\\r"
            case 0x09: out += "\\t"
            case 0x20...0x7E:
                out += String(UnicodeScalar(unit)!)
            default:
                out += String(format: "\\u%04x", unit)
            }
        }
        out += "\""
    }

    /// sha256 of the canonical encoding of the manifest's content keys —
    /// catalog.manifest_digest. `obj` is the full parsed manifest.
    static func manifestDigest(_ obj: [String: Any]) -> String? {
        let keys = ["v", "folder", "offset", "chapters", "pageRefs", "spreadTable"]
        var content: [String: Any] = [:]
        for k in keys {
            guard let v = obj[k] else { return nil }
            content[k] = v
        }
        let canon = encode(content)
        let digest = SHA256.hash(data: Data(canon.utf8))
        return "sha256:" + digest.map { String(format: "%02x", $0) }.joined()
    }
}
