import XCTest
@testable import Bookq

/// Drift guard for `api/openapi.v1.yaml` → `APIModels.swift` (#198).
///
/// The spec file is read straight from the repo (test host runs on macOS),
/// each covered response/request schema is interpreted, a canonical JSON
/// instance is fabricated from it, and every production Codable model must
/// (a) decode that instance and (b) re-encode every spec property. A spec
/// change that drifts from the hand-written models fails here.
///
/// The YAML reader is intentionally a small subset parser scoped to this
/// spec's constructs (block maps/sequences, flow `{}`/`[]`, block scalars).
/// Anything it cannot handle throws — a hard failure, never a silent pass.
final class SpecConformanceTests: XCTestCase {

    // MARK: - Coverage table

    /// components.schemas entries → production model.
    func testComponentSchemas() throws {
        try assertModel(TreeNode.self, schema: "TreeNode")
        try assertModel(TreeResponse.self, schema: "TreeResponse")
        try assertModel(ComicMetadata.self, schema: "ComicMetadata")
        try assertModel(Facets.self, schema: "Facets")
        try assertModel(ReadingPosition.self, schema: "ReadingPosition")
        try assertModel(VisibilitySnapshot.self, schema: "FolderVisibility")
    }

    /// Inline (non-$ref) response/request schemas under paths/.
    func testPathSchemas() throws {
        let images = try schemaAt("paths", "/api/v1/images", "get", "responses",
                                  "200", "content", "application/json", "schema")
        try assertModel(ImagesResponse.self, against: images)

        let positions = try schemaAt("paths", "/api/v1/reading-positions", "get", "responses",
                                     "200", "content", "application/json", "schema")
        try assertModel(PositionsResponse.self, against: positions)

        let putPos = try schemaAt("paths", "/api/v1/reading-positions", "put", "requestBody",
                                  "content", "application/json", "schema")
        try assertModel(PositionBody.self, against: putPos)

        let putStatus = try schemaAt("paths", "/api/v1/reading-positions", "put", "responses",
                                     "200", "content", "application/json", "schema")
        try assertModel(PositionStatus.self, against: putStatus)

        let delStatus = try schemaAt("paths", "/api/v1/reading-positions", "delete", "responses",
                                     "200", "content", "application/json", "schema")
        try assertModel(PositionStatus.self, against: delStatus)

        let search = try schemaAt("paths", "/api/v1/search", "get", "responses",
                                  "200", "content", "application/json", "schema")
        try assertModel(SearchResponse.self, against: search)

        let version = try schemaAt("paths", "/api/v1/version", "get", "responses",
                                   "200", "content", "application/json", "schema")
        try assertModel(VersionResponse.self, against: version)

        let token = try schemaAt("paths", "/api/v1/auth/token", "post", "responses",
                                 "200", "content", "application/json", "schema")
        try assertModel(TokenPairResponse.self, against: token)
    }

    // MARK: - Decode + re-encode assertion

    private func assertModel<T: Codable>(_ type: T.Type,
                                         schema name: String,
                                         file: StaticString = #filePath,
                                         line: UInt = #line) throws {
        try assertModel(type, against: schemaAt("components", "schemas", name), file: file, line: line)
    }

    /// Fabricated instance must decode AND re-encode every spec property —
    /// the re-encode direction is what catches a spec field the model
    /// silently drops (decode alone tolerates unknown keys).
    private func assertModel<T: Codable>(_ type: T.Type,
                                         against schema: SpecSchema,
                                         file: StaticString = #filePath,
                                         line: UInt = #line) throws {
        let json = fabricate(schema)
        let data = try JSONSerialization.data(withJSONObject: json)
        let model = try JSONDecoder().decode(T.self, from: data)
        // Spec-optional properties may be absent on the wire — the model
        // must still decode an instance containing required keys only.
        let minimal = fabricate(schema, requiredOnly: true)
        let minData = try JSONSerialization.data(withJSONObject: minimal)
        _ = try JSONDecoder().decode(T.self, from: minData)
        guard case .object(let props, _) = schema else { return }
        let enc = try JSONEncoder().encode(model)
        let dict = try JSONSerialization.jsonObject(with: enc) as? [String: Any]
        for key in props.keys {
            XCTAssertNotNil(dict?[key],
                "\(T.self) does not round-trip spec property \"\(key)\"",
                file: file, line: line)
        }
        for key in dict?.keys ?? [:].keys {
            XCTAssert(props.keys.contains(key),
                "\(T.self) emits \"\(key)\", which is not in the spec schema",
                file: file, line: line)
        }
    }

    // MARK: - Spec navigation

    private func specYaml() throws -> Yaml {
        // ios/BookqTests/SpecConformanceTests.swift → repo root is ../..
        let thisFile = URL(fileURLWithPath: #filePath)
        let specURL = thisFile
            .deletingLastPathComponent()   // BookqTests/
            .deletingLastPathComponent()   // ios/
            .deletingLastPathComponent()   // worktree root
            .appendingPathComponent("api/openapi.v1.yaml")
        guard FileManager.default.fileExists(atPath: specURL.path) else {
            throw NSError(domain: "spec", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "spec not found at \(specURL.path)"])
        }
        let text = try String(contentsOf: specURL, encoding: .utf8)
        return try Yaml.parse(text)
    }

    private func schemaAt(_ path: String...) throws -> SpecSchema {
        var node = try specYaml()
        for key in path {
            guard let next = node[key] else {
                throw NSError(domain: "spec", code: 2,
                              userInfo: [NSLocalizedDescriptionKey: "missing key \"\(key)\" under \(path)"])
            }
            node = next
        }
        return try interpret(node, depth: 0)
    }

    // MARK: - Schema interpretation

    private indirect enum SpecSchema {
        case object(props: [String: SpecSchema], required: Set<String>)
        case array(SpecSchema)
        case oneOf([SpecSchema])
        case prim(String, literal: String?)   // string/integer/boolean/number/free-form
        case nullable(SpecSchema)
        case cyclic                         // recursive $ref — fabricates as []
    }

    private func interpret(_ y: Yaml, depth: Int,
                           resolving: Set<String> = []) throws -> SpecSchema {
        struct SpecError: Error { let msg: String }
        guard case .map = y else {
            throw SpecError(msg: "schema node is not a map: \(y)")
        }
        if depth > 20 { throw SpecError(msg: "ref depth exceeded") }
        if let ref = y["$ref"]?.scalar {
            guard let name = ref.split(separator: "/").last.map(String.init) else {
                throw SpecError(msg: "bad $ref \(ref)")
            }
            if resolving.contains(name) { return .cyclic }
            guard let target = try specYaml()["components"]?["schemas"]?[name] else {
                throw SpecError(msg: "unresolvable $ref \(ref)")
            }
            return try interpret(target, depth: depth + 1,
                                 resolving: resolving.union([name]))
        }
        if let oneOf = y["oneOf"]?.list ?? y["anyOf"]?.list {
            return .oneOf(try oneOf.map { try interpret($0, depth: depth + 1, resolving: resolving) })
        }
        if let allOf = y["allOf"]?.list {
            // merge object members
            var merged: [String: SpecSchema] = [:]
            var req = Set<String>()
            for m in allOf {
                if case .object(let p, let r) = try interpret(m, depth: depth + 1, resolving: resolving) {
                    for (k, v) in p { merged[k] = v }
                    req.formUnion(r)
                }
            }
            return .object(props: merged, required: req)
        }
        let typeNode = y["type"]
        if let tList = typeNode?.list {                    // [type, "null"]
            let nonNull = tList.filter { $0.scalar != "null" }
            guard nonNull.count == 1 else {
                return .nullable(.prim("any", literal: nil))
            }
            var rebuilt: [(String, Yaml)] = [("type", nonNull[0])]
            for key in ["items", "properties", "required"] {
                if let v = y[key], !v.isNull { rebuilt.append((key, v)) }
            }
            return .nullable(try interpret(.map(rebuilt), depth: depth, resolving: resolving))
        }
        let literal = y["const"]?.scalar ?? y["enum"]?.list?.first?.scalar
        let hasProps = y["properties"].map { !$0.isNull } ?? false
        switch (typeNode?.scalar, hasProps) {
        case (_, true):
            var props: [String: SpecSchema] = [:]
            if case .map(let pps) = y["properties"] ?? .null {
                for (k, v) in pps { props[k] = try interpret(v, depth: depth + 1, resolving: resolving) }
            }
            var req = Set<String>()
            if let r = y["required"]?.list {
                for item in r { if let s = item.scalar { req.insert(s) } }
            }
            return .object(props: props, required: req)
        case ("array", false):
            return .array(try interpret(y["items"] ?? .scalar("string"), depth: depth + 1, resolving: resolving))
        case ("object", false):
            return .prim("object", literal: literal)       // free-form
        case (let t?, false):
            return .prim(t, literal: literal)
        case (nil, false):
            return .prim("any", literal: literal)
        }
    }

    // MARK: - Instance fabrication

    private func fabricate(_ s: SpecSchema, depth: Int = 4,
                           requiredOnly: Bool = false) -> Any {
        switch s {
        case .prim(_, .some(let lit)): return lit
        case .prim(let t, _) where t == "integer" || t == "number": return 1
        case .prim(let t, _) where t == "boolean": return true
        case .prim(let t, _) where t == "object": return ["k": "spec"]
        case .prim:              return "spec"
        case .object(let props, let required):
            var d: [String: Any] = [:]
            for (k, v) in props where !requiredOnly || required.contains(k) {
                d[k] = fabricate(v, depth: depth, requiredOnly: requiredOnly)
            }
            return d
        case .array(let item):
            // A recursive element has no finite instance; [] is spec-legal.
            if case .cyclic = item { return [Any]() }
            return depth <= 0 ? [Any]()
                : [fabricate(item, depth: depth - 1, requiredOnly: requiredOnly)]
        case .nullable(let inner): return fabricate(inner, depth: depth, requiredOnly: requiredOnly)
        case .oneOf(let opts):   return fabricate(opts[0], depth: depth, requiredOnly: requiredOnly)
        case .cyclic:            return [Any]()
        }
    }
}

// MARK: - YAML subset parser

/// Minimal YAML node: block maps/sequences, flow collections, quoted/plain
/// scalars, block scalars, comments. Not a general YAML implementation —
/// enough for openapi.v1.yaml's shape; unsupported constructs throw.
private indirect enum Yaml {
    case scalar(String)
    case list([Yaml])
    case map([(String, Yaml)])
    case null

    var scalar: String? { if case .scalar(let s) = self { return s }; return nil }
    var list: [Yaml]?   { if case .list(let l) = self { return l }; return nil }
    var isNull: Bool    { if case .null = self { return true }; return false }
    subscript(key: String) -> Yaml? {
        guard case .map(let pairs) = self else { return nil }
        return pairs.first { $0.0 == key }?.1
    }

    struct ParseError: Error { let msg: String }

    private struct Line {
        var indent: Int
        var text: String
    }

    static func parse(_ text: String) throws -> Yaml {
        var lines: [Line] = []
        for raw in text.split(separator: "\n", omittingEmptySubsequences: false) {
            var s = String(raw)
            if s.hasSuffix("\r") { s.removeLast() }
            guard let stripped = stripComment(s) else { continue }
            let indent = stripped.prefix { $0 == " " }.count
            let content = String(stripped.dropFirst(indent))
            if content.isEmpty || content == "---" || content == "..." { continue }
            if stripped.contains("\t") {
                throw ParseError(msg: "tab in yaml: \(content)")
            }
            lines.append(Line(indent: indent, text: content))
        }
        guard !lines.isEmpty else { throw ParseError(msg: "empty yaml") }
        let (v, used) = try parseBlock(lines, 0, lines[0].indent)
        if used != lines.count { throw ParseError(msg: "trailing content at line \(used)") }
        return v
    }

    /// Cut ` #` comments outside quotes. nil = whole line is a comment/blank.
    private static func stripComment(_ s: String) -> String? {
        var quote: Character? = nil
        var prev: Character = " "
        for (i, ch) in s.enumerated() {
            let idx = s.index(s.startIndex, offsetBy: i)
            if let q = quote {
                if ch == q, prev != "\\" { quote = nil }
            } else if ch == "\"" || ch == "'" {
                quote = ch
            } else if ch == "#", (i == 0 || prev == " ") {
                let cut = String(s[..<idx])
                let trimmed = cut.trimmingCharacters(in: .whitespaces)
                return trimmed.isEmpty ? nil : cut
            }
            prev = ch
        }
        let trimmed = s.trimmingCharacters(in: .whitespaces)
        return trimmed.isEmpty ? nil : s
    }

    private static func parseBlock(_ lines: [Line], _ start: Int, _ indent: Int) throws -> (Yaml, Int) {
        if lines[start].text == "-" || lines[start].text.hasPrefix("- ") {
            return try parseList(lines, start, indent)
        }
        return try parseMap(lines, start, indent)
    }

    private static func parseMap(_ lines: [Line], _ start: Int, _ indent: Int) throws -> (Yaml, Int) {
        var pairs: [(String, Yaml)] = []
        var i = start
        while i < lines.count, lines[i].indent == indent, !lines[i].text.hasPrefix("- ") {
            guard let (key, rest) = splitKey(lines[i].text) else {
                throw ParseError(msg: "expected key: \(lines[i].text)")
            }
            i += 1
            if rest.isEmpty {
                if i < lines.count, lines[i].indent > indent {
                    let (v, ni) = try parseBlock(lines, i, lines[i].indent)
                    pairs.append((key, v)); i = ni
                } else {
                    pairs.append((key, .null))
                }
            } else if rest.hasPrefix("|") || rest.hasPrefix(">") {
                while i < lines.count, lines[i].indent > indent { i += 1 }
                pairs.append((key, .scalar("<block>")))
            } else {
                pairs.append((key, try parseValue(rest, lines: lines, index: &i, indent: indent)))
            }
        }
        return (.map(pairs), i)
    }

    private static func parseList(_ lines: [Line], _ start: Int, _ indent: Int) throws -> (Yaml, Int) {
        var items: [Yaml] = []
        var i = start
        while i < lines.count, lines[i].indent == indent,
              lines[i].text == "-" || lines[i].text.hasPrefix("- ") {
            let content = lines[i].text == "-" ? "" : String(lines[i].text.dropFirst(2))
            i += 1
            if content.isEmpty {
                guard i < lines.count, lines[i].indent > indent else {
                    items.append(.null); continue
                }
                let (v, ni) = try parseBlock(lines, i, lines[i].indent)
                items.append(v); i = ni
            } else if splitKey(content) != nil {
                // "- key: v" — map item; continuation lives at indent+2
                var sub = [Line(indent: indent + 2, text: content)]
                while i < lines.count, lines[i].indent >= indent + 2 {
                    sub.append(lines[i]); i += 1
                }
                let (v, _) = try parseMap(sub, 0, indent + 2)
                items.append(v)
            } else {
                items.append(try parseValue(content, lines: lines, index: &i, indent: indent))
            }
        }
        return (.list(items), i)
    }

    /// "key: rest" split — first ':' followed by space/EOL, outside quotes.
    private static func splitKey(_ s: String) -> (String, String)? {
        var quote: Character? = nil
        var prev: Character = " "
        for (i, ch) in s.enumerated() {
            if let q = quote {
                if ch == q, prev != "\\" { quote = nil }
            } else if ch == "\"" || ch == "'" {
                quote = ch
            } else if ch == ":", i + 1 == s.count || s[s.index(s.startIndex, offsetBy: i + 1)] == " " {
                let key = String(s.prefix(i)).trimmingCharacters(in: .whitespaces)
                let rest = String(s.dropFirst(i + 1)).trimmingCharacters(in: .whitespaces)
                return (unquote(key), rest)
            }
            prev = ch
        }
        return nil
    }

    private static func parseValue(_ s: String, lines: [Line], index: inout Int, indent: Int) throws -> Yaml {
        let t = s.trimmingCharacters(in: .whitespaces)
        if t.hasPrefix("{") || t.hasPrefix("[") {
            var p = FlowParser(t)
            return try p.parse()
        }
        if t.hasPrefix("\"") || t.hasPrefix("'") { return .scalar(unquote(t)) }
        switch t {
        case "null", "~", "Null", "NULL": return .null
        default: return .scalar(t)
        }
    }

    private static func unquote(_ s: String) -> String {
        var t = s.trimmingCharacters(in: .whitespaces)
        if t.count >= 2,
           (t.hasPrefix("\"") && t.hasSuffix("\"")) ||
           (t.hasPrefix("'") && t.hasSuffix("'")) {
            let q = t.removeFirst()
            t.removeLast()
            // YAML escapes a quote by doubling it ('' inside '…', "" inside "…")
            return t.replacingOccurrences(of: String([q, q]), with: String(q))
        }
        return t
    }

    /// Recursive-descent flow collection parser ({...}/[...] with quotes).
    private struct FlowParser {
        let s: String
        var i: String.Index

        init(_ s: String) { self.s = s; self.i = s.startIndex }

        mutating func parse() throws -> Yaml { try value() }

        private mutating func ws() {
            while i < s.endIndex, s[i] == " " { i = s.index(after: i) }
        }

        private mutating func value() throws -> Yaml {
            ws()
            guard i < s.endIndex else { return .null }
            switch s[i] {
            case "{": return try mapLit()
            case "[": return try listLit()
            case "\"", "'": return .scalar(quoted())
            default: return .scalar(bare())
            }
        }

        private mutating func mapLit() throws -> Yaml {
            i = s.index(after: i)                          // {
            var pairs: [(String, Yaml)] = []
            while true {
                ws()
                if i < s.endIndex, s[i] == "}" { i = s.index(after: i); return .map(pairs) }
                let key = (s[i] == "\"" || s[i] == "'") ? quoted() : bare(stopAt: ":,}")
                ws()
                guard i < s.endIndex, s[i] == ":" else {
                    throw ParseError(msg: "flow map missing ':' in \(s)")
                }
                i = s.index(after: i)
                pairs.append((key, try value()))
                ws()
                if i < s.endIndex, s[i] == "," { i = s.index(after: i) }
            }
        }

        private mutating func listLit() throws -> Yaml {
            i = s.index(after: i)                          // [
            var items: [Yaml] = []
            while true {
                ws()
                if i < s.endIndex, s[i] == "]" { i = s.index(after: i); return .list(items) }
                items.append(try value())
                ws()
                if i < s.endIndex, s[i] == "," { i = s.index(after: i) }
            }
        }

        private mutating func quoted() -> String {
            let q = s[i]; var out = ""
            i = s.index(after: i)
            while i < s.endIndex {
                let c = s[i]; i = s.index(after: i)
                if c == "\\", i < s.endIndex { out.append(s[i]); i = s.index(after: i); continue }
                if c == q { return out }
                out.append(c)
            }
            return out
        }

        private mutating func bare(stopAt: String = ",}]") -> String {
            var j = i
            while j < s.endIndex, !stopAt.contains(s[j]) { j = s.index(after: j) }
            defer { i = j }
            return String(s[i..<j]).trimmingCharacters(in: .whitespaces)
        }
    }
}
