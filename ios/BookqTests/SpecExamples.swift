import Foundation
@testable import Bookq

/// Spec-example fixture source (#237): `api/dist/examples.json` (built by
/// `scripts/api/extract-examples.py`) is the single fixture source — tests
/// below answer StubURLProtocol from this bundle instead of hand-written
/// bodies, so spec drift breaks tests instead of silently forking fixtures.
enum SpecExamples {
    struct LookupError: Error, CustomStringConvertible {
        var description: String
    }

    private static var bundle: [[String: Any]] = {
        let thisFile = URL(fileURLWithPath: #filePath)
        let url = thisFile
            .deletingLastPathComponent()   // BookqTests/
            .deletingLastPathComponent()   // ios/
            .deletingLastPathComponent()   // worktree root
            .appendingPathComponent("api/dist/examples.json")
        guard let data = try? Data(contentsOf: url),
              let obj = try? JSONSerialization.jsonObject(with: data),
              let dict = obj as? [String: Any],
              let examples = dict["examples"] as? [[String: Any]] else {
            fatalError("SpecExamples: cannot load api/dist/examples.json at \(url.path) — run scripts/api/extract-examples.py")
        }
        return examples
    }()

    /// Names available for (operationId, status), e.g. ("getTree","200") →
    /// ["default","children"].
    static func names(operationId: String, status: String = "200") -> [String] {
        bundle.compactMap { e in
            (e["operationId"] as? String) == operationId && "\(e["status"] ?? "")" == status
                ? e["name"] as? String : nil
        }
    }

    /// Raw fixture value; throws LookupError when the bundle lacks it.
    static func value(operationId: String, status: String = "200",
                      name: String = "default") throws -> Any {
        if let e = bundle.first(where: {
            ($0["operationId"] as? String) == operationId
                && "\($0["status"] ?? "")" == status
                && ($0["name"] as? String) == name
        }), let v = e["value"] {
            return v
        }
        throw LookupError(description:
            "no spec example \(operationId) \(status) [\(name)] — check api/dist/examples.json")
    }

    /// Fixture value as JSON body data.
    static func data(operationId: String, status: String = "200",
                     name: String = "default") throws -> Data {
        try JSONSerialization.data(
            withJSONObject: value(operationId: operationId, status: status, name: name))
    }
}

extension StubURLProtocol {
    /// Answer with a spec-example body (#237). 202s carry `Retry-After: 0`
    /// unless overridden, mirroring mock_server.py.
    static func specExample(_ operationId: String, status: Int = 200,
                            name: String = "default",
                            headers: [String: String] = [:]) -> Response {
        let data = try! SpecExamples.data(
            operationId: operationId, status: "\(status)", name: name)
        var h = headers
        if status == 202 && h["Retry-After"] == nil { h["Retry-After"] = "0" }
        return Response(status: status, body: data, headers: h)
    }
}
