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
///
/// Moved verbatim from `ios/Bookq/Sync/CanonicalJSON.swift`, except the
/// SHA-256: CryptoKit is Apple-only, and BookqCore must also build on
/// Linux (`swift test` without Xcode). The bundled `SHA256` below is a
/// self-contained FIPS 180-4 implementation — byte-identical digests on
/// every platform.
public enum CanonicalJSON {

    /// Serialize a JSONSerialization-parsed value canonically.
    public static func encode(_ value: Any) -> String {
        var out = ""
        write(value, into: &out)
        return out
    }

    private static func write(_ v: Any, into out: inout String) {
        switch v {
        case is NSNull:
            out += "null"
        case let n as NSNumber:
            // Bool-vs-number: JSON `true` must encode as `true`, never
            // `1` (and NSNumber(1) never as `true`). The original app code
            // used CFGetTypeID/CFBooleanGetTypeID, which don't exist on
            // Linux — instead match native `Bool` exactly, or the
            // `__NSCFBoolean` dynamic type both platforms use for parsed
            // JSON booleans (NSNumber(value: Bool) included).
            if isBoolValue(v) {
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
                // k comes from d.keys, so the subscript is never nil —
                // unwrap here: passing the Optional through `as Any`
                // would hide the dynamic type (e.g. Bool) from isBoolValue.
                write(d[k]!, into: &out)
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

    /// True for native Swift `Bool` and `__NSCFBoolean` (parsed JSON
    /// booleans, `NSNumber(value: Bool)`) — false for numeric NSNumbers,
    /// which bridge to `Bool` but must encode as numbers.
    private static func isBoolValue(_ v: Any) -> Bool {
        if type(of: v) == Bool.self { return true }
        return String(describing: type(of: v)).contains("Boolean")
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
    public static func manifestDigest(_ obj: [String: Any]) -> String? {
        let keys = ["v", "folder", "offset", "chapters", "pageRefs", "spreadTable"]
        var content: [String: Any] = [:]
        for k in keys {
            guard let v = obj[k] else { return nil }
            content[k] = v
        }
        let canon = encode(content)
        let digest = SHA256.hash(Data(canon.utf8))
        return "sha256:" + digest.map { String(format: "%02x", $0) }.joined()
    }
}

/// Minimal FIPS 180-4 SHA-256 (no CryptoKit dependency so BookqCore
/// builds on Linux). Verified against the shared
/// `tests/fixtures/reader_sync_cases.json` golden digests in
/// `CanonicalJSONTests`.
enum SHA256 {
    private static let k: [UInt32] = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5,
        0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
        0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
        0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
        0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
        0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
        0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
        0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
        0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
        0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
        0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3,
        0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
        0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5,
        0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
        0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
    ]

    @inline(__always)
    private static func rotr(_ x: UInt32, _ n: UInt32) -> UInt32 {
        (x >> n) | (x << (32 - n))
    }

    static func hash(_ data: Data) -> [UInt8] {
        var h: [UInt32] = [
            0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
            0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
        ]
        var msg = Array(data)
        let bitLen = UInt64(msg.count) * 8
        msg.append(0x80)
        while msg.count % 64 != 56 { msg.append(0) }
        for i in (0..<8).reversed() {
            msg.append(UInt8((bitLen >> (i * 8)) & 0xff))
        }
        for chunk in stride(from: 0, to: msg.count, by: 64) {
            var w = [UInt32](repeating: 0, count: 64)
            for i in 0..<16 {
                let o = chunk + i * 4
                w[i] = (UInt32(msg[o]) << 24) | (UInt32(msg[o + 1]) << 16)
                    | (UInt32(msg[o + 2]) << 8) | UInt32(msg[o + 3])
            }
            for i in 16..<64 {
                let s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >> 3)
                let s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >> 10)
                w[i] = w[i - 16] &+ s0 &+ w[i - 7] &+ s1
            }
            var (a, b, c, d, e, f, g, hh) =
                (h[0], h[1], h[2], h[3], h[4], h[5], h[6], h[7])
            for i in 0..<64 {
                let s1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)
                let ch = (e & f) ^ (~e & g)
                let t1 = hh &+ s1 &+ ch &+ k[i] &+ w[i]
                let s0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)
                let maj = (a & b) ^ (a & c) ^ (b & c)
                let t2 = s0 &+ maj
                hh = g; g = f; f = e; e = d &+ t1
                d = c; c = b; b = a; a = t1 &+ t2
            }
            h[0] = h[0] &+ a; h[1] = h[1] &+ b
            h[2] = h[2] &+ c; h[3] = h[3] &+ d
            h[4] = h[4] &+ e; h[5] = h[5] &+ f
            h[6] = h[6] &+ g; h[7] = h[7] &+ hh
        }
        return h.flatMap { v in
            [UInt8((v >> 24) & 0xff), UInt8((v >> 16) & 0xff),
             UInt8((v >> 8) & 0xff), UInt8(v & 0xff)]
        }
    }
}
