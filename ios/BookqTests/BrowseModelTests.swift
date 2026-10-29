import XCTest
@testable import Bookq

/// Browse rule parity with `app/static/browse/render.js`.
final class BrowseModelTests: XCTestCase {

    private func folder(_ name: String, children: [TreeNode]? = nil,
                        thumb: String? = nil, thumbs: [String]? = nil) -> TreeNode {
        TreeNode(name: name, type: "folder", children: children,
                 thumb: thumb, thumbs: thumbs)
    }

    private func image(_ name: String) -> TreeNode {
        TreeNode(name: name, type: "image")
    }

    // MARK: hasOnlyImages / mixed

    func testHasOnlyImages() {
        XCTAssertTrue(BrowseModel.hasOnlyImages(folder("a", children: [image("1.jpg")])))
        XCTAssertFalse(BrowseModel.hasOnlyImages(folder("a")))
        XCTAssertFalse(BrowseModel.hasOnlyImages(folder("a", children: [])))
        XCTAssertFalse(BrowseModel.hasOnlyImages(folder("a", children: [folder("b")])))
        XCTAssertFalse(BrowseModel.hasOnlyImages(folder("a", children: [image("1"), folder("b")])))
    }

    func testMixed() {
        XCTAssertTrue(BrowseModel.isMixed(folder("a", children: [image("1"), folder("b")])))
        XCTAssertFalse(BrowseModel.isMixed(folder("a", children: [image("1")])))
        XCTAssertFalse(BrowseModel.isMixed(folder("a", children: [folder("b")])))
    }

    // MARK: node lookup — names with "/" are never split

    func testNodeAtPath() {
        let tree = [
            folder("Series/巻1", children: [image("p1"), image("p2")]),
            folder("Series", children: [folder("Sub", children: [image("x")])]),
        ]
        // flattened name resolves as a composed path
        XCTAssertNotNil(BrowseModel.node(at: "Series/巻1", in: tree))
        // flattened wins over drilling "Series"→"巻1" — composed match on name
        XCTAssertNotNil(BrowseModel.node(at: "Series/Sub", in: tree))
        XCTAssertNil(BrowseModel.node(at: "Series/Nothing", in: tree))
        XCTAssertNil(BrowseModel.node(at: "", in: tree))
    }

    func testNodeAtDoesNotMatchNameSplit() {
        // A flattened child "wrap/x" inside root "r": path "r/wrap/x" must hit
        // the flattened node, not some deeper traversal.
        let tree = [folder("r", children: [folder("wrap/x", children: [image("p")])])]
        let hit = BrowseModel.node(at: "r/wrap/x", in: tree)
        XCTAssertEqual(hit?.name, "wrap/x")
    }

    // MARK: level entries & self tiles

    func testSelfTilesMixedOnly() {
        let root = folder("root", children: [
            folder("mixed", children: [image("1"), folder("sub")]),
            folder("leaf", children: [image("2")]),
            folder("nested", children: [folder("deep")]),
        ])
        let tiles = BrowseModel.selfTiles(at: "root", in: [root])
        XCTAssertEqual(tiles.map(\.path), ["root/mixed"])
    }

    func testSelfTileAtRootForLooseImages() {
        XCTAssertEqual(BrowseModel.selfTiles(at: "", in: [image("a.jpg")]).count, 1)
        XCTAssertEqual(BrowseModel.selfTiles(at: "", in: [folder("f")]).count, 0)
    }

    func testDrillsDown() {
        XCTAssertFalse(BrowseModel.drillsDown(folder("leaf", children: [image("1")])))
        XCTAssertTrue(BrowseModel.drillsDown(folder("mix", children: [image("1"), folder("s")])))
        XCTAssertTrue(BrowseModel.drillsDown(folder("nest", children: [folder("s")])))
    }

    // MARK: multiThumbSpec table parity

    func testMultiThumbSpec() {
        XCTAssertNil(BrowseModel.multiThumbSpec(0))
        XCTAssertNil(BrowseModel.multiThumbSpec(1))
        XCTAssertEqual(BrowseModel.multiThumbSpec(2), .init(rows: [2], cols: 2))
        XCTAssertEqual(BrowseModel.multiThumbSpec(3), .init(rows: [1, 2], cols: 2))
        XCTAssertEqual(BrowseModel.multiThumbSpec(4), .init(rows: [2, 2], cols: 2))
        XCTAssertEqual(BrowseModel.multiThumbSpec(5), .init(rows: [2, 3], cols: 6))
        XCTAssertEqual(BrowseModel.multiThumbSpec(6), .init(rows: [2, 2, 2], cols: 2))
        XCTAssertEqual(BrowseModel.multiThumbSpec(7), .init(rows: [2, 3, 2], cols: 6))
        XCTAssertEqual(BrowseModel.multiThumbSpec(8), .init(rows: [3, 2, 3], cols: 6))
        XCTAssertEqual(BrowseModel.multiThumbSpec(9), .init(rows: [3, 3, 3], cols: 3))
        // >9 clamps to 9
        XCTAssertEqual(BrowseModel.multiThumbSpec(12), BrowseModel.multiThumbSpec(9))
    }

    // MARK: display name / filter

    func testSplitDisplayName() {
        XCTAssertEqual(BrowseModel.splitDisplayName("abc").leaf, "abc")
        XCTAssertNil(BrowseModel.splitDisplayName("abc").parent)
        XCTAssertEqual(BrowseModel.splitDisplayName("親/子").leaf, "子")
        XCTAssertEqual(BrowseModel.splitDisplayName("親/子").parent, "親")
    }

    func testFilter() {
        let entries = [
            ("a/One", folder("One")),
            ("a/Two", folder("Two")),
        ]
        XCTAssertEqual(BrowseModel.filter(entries, query: "one").count, 1)
        XCTAssertEqual(BrowseModel.filter(entries, query: "ONE").count, 1) // case-insensitive
        XCTAssertEqual(BrowseModel.filter(entries, query: "a/").count, 2)  // path match
        XCTAssertEqual(BrowseModel.filter(entries, query: "").count, 2)
        XCTAssertEqual(BrowseModel.filter(entries, query: "zzz").count, 0)
    }
}
