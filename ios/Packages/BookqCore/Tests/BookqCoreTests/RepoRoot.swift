import Foundation

/// Repo-root lookup for tests that read shared fixtures (`api/vectors/*`,
/// `tests/fixtures/*`). Walks up from the calling test file until the
/// `api/vectors` marker exists, so the same helper works whether the test
/// lives in `ios/BookqTests/` or
/// `ios/Packages/BookqCore/Tests/BookqCoreTests/`.
enum TestRepo {
    static func root(from filePath: String) -> URL {
        var url = URL(fileURLWithPath: filePath)
        for _ in 0..<12 {
            url.deleteLastPathComponent()
            let marker = url.appendingPathComponent("api/vectors")
            if FileManager.default.fileExists(atPath: marker.path) {
                return url
            }
        }
        fatalError("repo root (api/vectors) not found from \(filePath)")
    }

    /// Parsed `api/vectors/<name>` document.
    static func vector(_ name: String, from filePath: String) -> [String: Any] {
        let url = root(from: filePath).appendingPathComponent("api/vectors/\(name)")
        let data = try! Data(contentsOf: url)
        return try! JSONSerialization.jsonObject(with: data) as! [String: Any]
    }

    /// Parsed JSON fixture at a repo-relative path
    /// (e.g. `tests/fixtures/reader_sync_cases.json`).
    static func fixture(_ path: String, from filePath: String) -> Any {
        let url = root(from: filePath).appendingPathComponent(path)
        let data = try! Data(contentsOf: url)
        return try! JSONSerialization.jsonObject(with: data)
    }
}
