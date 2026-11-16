import XCTest
@testable import BookqCore

/// Canonical-JSON + manifest-digest golden tests — the fixture is read from
/// `tests/fixtures/reader_sync_cases.json` `manifestDigestCases` (#280), the
/// same file the Python side pins in test_conformance_vectors.py. The digest
/// is never hand-copied here: any drift between the two languages fails.
///
/// Moved from `ios/BookqTests/PairSyncTests.swift` — pure domain rules, no
/// simulator needed.
final class CanonicalJSONTests: XCTestCase {

    /// (manifest content + initial, expected digest) cases from the shared
    /// fixture — every entry, matching the Python side's full iteration.
    static func digestCases() -> [(manifest: [String: Any], digest: String)] {
        let fixture = TestRepo.fixture(
            "tests/fixtures/reader_sync_cases.json", from: #filePath)
            as! [String: Any]
        return (fixture["manifestDigestCases"] as! [[String: Any]]).map {
            ($0["manifest"] as! [String: Any], $0["expectDigest"] as! String)
        }
    }

    /// First case — used by tests that need any single fixture pair.
    static func digestCase() -> (manifest: [String: Any], digest: String) {
        digestCases()[0]
    }

    /// Python: {"b": 1, "a": true, "u": "作品"}
    /// → {"a":true,"b":1,"u":"作品"} — sorted, compact, ASCII-escaped.
    func testCanonicalEncoding() {
        let s = CanonicalJSON.encode(["b": 1, "a": true, "u": "作品"])
        XCTAssertEqual(s, "{\"a\":true,\"b\":1,\"u\":\"\\u4f5c\\u54c1\"}")
    }

    /// NSNumber bridging must NOT collapse 1 → true / false → 0.
    /// (JSONSerialization decodes all JSON scalars as NSNumber.)
    func testBoolVersusNumberRoundTrip() throws {
        let src = "{\"a\":true,\"b\":1,\"c\":0,\"d\":false}"
        let obj = try JSONSerialization.jsonObject(
            with: Data(src.utf8)) as! [String: Any]
        XCTAssertEqual(CanonicalJSON.encode(obj), src)
    }

    /// Golden vector from the shared fixture — content keys only
    /// (v/folder/offset/chapters/pageRefs/spreadTable).
    func testManifestDigestGolden() {
        for (manifest, digest) in Self.digestCases() {
            XCTAssertEqual(CanonicalJSON.manifestDigest(manifest), digest)
        }
    }

    /// The digest must change when content changes (typo/classification).
    func testManifestDigestSensitive() {
        let (manifest, digest) = Self.digestCase()
        var m = manifest
        m["offset"] = 0
        XCTAssertNotEqual(CanonicalJSON.manifestDigest(m), digest)
    }

    static var manifestFixture: [String: Any] {
        var (manifest, digest) = digestCase()
        manifest["digest"] = digest
        return manifest
    }
}

/// PairManifest.parse — wire-dict → typed manifest.
final class PairManifestTests: XCTestCase {
    func testParse() {
        let m = PairManifest.parse(CanonicalJSONTests.manifestFixture)
        XCTAssertNotNil(m)
        XCTAssertEqual(m?.spreadTable.count, 2)
        XCTAssertEqual(m?.spreadIndex(byId: "s1"), 1)
        XCTAssertNil(m?.spreadTable[1].left)   // trailing blank side
    }

    func testParseRejectsMalformed() {
        var bad = CanonicalJSONTests.manifestFixture
        bad["spreadTable"] = [["id": 42]]
        XCTAssertNil(PairManifest.parse(bad))
        bad = CanonicalJSONTests.manifestFixture
        bad.removeValue(forKey: "chapters")
        XCTAssertNil(PairManifest.parse(bad))
    }
}
