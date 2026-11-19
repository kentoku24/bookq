import XCTest
@testable import Bookq

/// Client fixtures come from the spec bundle, not hand-written bodies (#237).
///
/// The bundle (`api/dist/examples.json`) is validated against the spec
/// schemas by `scripts/api/validate-examples.py` in CI; these tests prove
/// the iOS models and StubURLProtocol actually consume that bundle — a spec
/// change that drifts from the app fails here instead of forking fixtures.
final class SpecExamplesTests: XCTestCase {

    override func setUp() async throws { StubURLProtocol.reset() }

    /// The bundle covers every major endpoint with a `default` 200 example.
    func testBundleCoversMajorEndpoints() throws {
        let majors = ["getTree", "listImages", "search", "getFacets", "refreshIndex",
                      "listReadingPositions", "putReadingPosition", "deleteReadingPosition",
                      "getFolderVisibility", "putFolderVisibility", "exchangeToken",
                      "getVersion", "getHealth"]
        for op in majors {
            XCTAssertTrue(SpecExamples.names(operationId: op).contains("default"),
                          "bundle missing \(op) 200 [default]")
        }
        XCTAssertTrue(SpecExamples.names(operationId: "getTree").contains("children"))
        XCTAssertTrue(SpecExamples.names(operationId: "putReadingPosition").contains("stale"))
        XCTAssertTrue(SpecExamples.names(operationId: "putFolderVisibility", status: "409")
            .contains("owner_mismatch"))
    }

    /// Bundle 200s decode straight into the production models.
    func testBundleDefaultsDecodeIntoModels() throws {
        func decode<T: Decodable>(_ type: T.Type, _ op: String, _ name: String = "default") throws -> T {
            try JSONDecoder().decode(T.self, from: SpecExamples.data(operationId: op, name: name))
        }
        let tree: TreeResponse = try decode(TreeResponse.self, "getTree")
        XCTAssertEqual(tree.kind, "tree")
        XCTAssertEqual(tree.generation, 7)
        XCTAssertFalse(tree.nodes.isEmpty)

        let children: TreeResponse = try decode(TreeResponse.self, "getTree", "children")
        XCTAssertEqual(children.kind, "children")

        let images: ImagesResponse = try decode(ImagesResponse.self, "listImages")
        XCTAssertEqual(images.folder, "Series/vol1")
        XCTAssertEqual(images.images, ["p00.jpg", "p01.jpg", "p02.jpg"])

        let search: SearchResponse = try decode(SearchResponse.self, "search")
        XCTAssertEqual(search.total, 1)
        XCTAssertEqual(search.results.first?.folder_path, "Series/vol1")

        let facets: Facets = try decode(Facets.self, "getFacets")
        XCTAssertFalse(facets.artists.isEmpty)

        let positions: PositionsResponse = try decode(PositionsResponse.self, "listReadingPositions")
        XCTAssertEqual(positions.positions.count, 2)

        let ok: PositionStatus = try decode(PositionStatus.self, "putReadingPosition", "ok")
        XCTAssertEqual(ok.status, "ok")
        let stale: PositionStatus = try decode(PositionStatus.self, "putReadingPosition", "stale")
        XCTAssertEqual(stale.status, "stale")

        let vis: VisibilitySnapshot = try decode(VisibilitySnapshot.self, "getFolderVisibility")
        XCTAssertEqual(vis.hiddenRoots, ["Secret"])

        let version: VersionResponse = try decode(VersionResponse.self, "getVersion")
        XCTAssertFalse(version.version.isEmpty)

        let token: TokenPairResponse = try decode(TokenPairResponse.self, "exchangeToken")
        XCTAssertEqual(token.token_type, "Bearer")
    }

    /// StubURLProtocol answers from the bundle — tree() unwraps spec nodes.
    func testStubServesSpecExampleTree() async throws {
        StubURLProtocol.handler = { _ in StubURLProtocol.specExample("getTree") }
        let r = try await StubURLProtocol.api().tree()
        guard case .nodes(let kind, let nodes, let gen, _) = r else {
            return XCTFail("expected .nodes")
        }
        let want = try JSONDecoder().decode(
            TreeResponse.self, from: SpecExamples.data(operationId: "getTree"))
        XCTAssertEqual(kind, want.kind)
        XCTAssertEqual(gen, want.generation)
        XCTAssertEqual(nodes.count, want.nodes.count)
        XCTAssertEqual(nodes.first?.name, "Series")
    }

    /// 202 index-building (spec shared example) then 200 — client retries,
    /// mirroring the Python mock-sequence test.
    func testTree202Then200FromSpecExamples() async throws {
        var hits = 0
        StubURLProtocol.handler = { _ in
            hits += 1
            return hits == 1
                ? StubURLProtocol.specExample("getTree", status: 202, name: "index_building")
                : StubURLProtocol.specExample("getTree")
        }
        let r = try await StubURLProtocol.api().tree()
        guard case .nodes(_, _, let gen, _) = r else {
            return XCTFail("expected .nodes")
        }
        XCTAssertEqual(hits, 2)
        XCTAssertEqual(gen, 7)
    }

    /// stale PUT comes from the bundle, not a literal.
    func testPutStaleFromSpecExample() async throws {
        StubURLProtocol.handler = { _ in
            StubURLProtocol.specExample("putReadingPosition", name: "stale")
        }
        let status = try await StubURLProtocol.api().putReadingPosition(
            ReadingPosition(folder: "Series/vol1", page: 1, total: 10, ts: 1))
        XCTAssertEqual(status?.status, "stale")
    }
}
