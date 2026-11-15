import XCTest
@testable import Bookq

/// Conformance vectors (#280) — Swift side.
///
/// `api/vectors/*.json` pins the domain rules shared by Python, JS and
/// Swift. Each file declares its `languages`; that the declared readers
/// actually reference it is pinned by tests/test_vectors_declaration.py
/// (pure Python). The vector trees decode straight into the production
/// `TreeNode` model — no test-only shapes.
final class ConformanceVectorsTests: XCTestCase {

    // MARK: - loading

    /// `ios/BookqTests/ConformanceVectorsTests.swift` → repo root is ../..
    static func repoRoot() -> URL {
        let thisFile = URL(fileURLWithPath: #filePath)
        return thisFile
            .deletingLastPathComponent()   // BookqTests/
            .deletingLastPathComponent()   // ios/
            .deletingLastPathComponent()   // worktree root
    }

    static func loadVector(_ name: String) -> [String: Any] {
        let url = repoRoot().appendingPathComponent("api/vectors/\(name)")
        let data = try! Data(contentsOf: url)
        return try! JSONSerialization.jsonObject(with: data) as! [String: Any]
    }

    static func treeNodes(_ json: Any) -> [TreeNode] {
        let data = try! JSONSerialization.data(withJSONObject: json)
        return try! JSONDecoder().decode([TreeNode].self, from: data)
    }

    /// JSON numbers arrive as NSNumber — map explicitly instead of
    /// relying on nested bridging casts.
    static func ints(_ json: Any) -> [Int] {
        (json as! [NSNumber]).map(\.intValue)
    }

    static func intMatrix(_ json: Any) -> [[Int]] {
        (json as! [[NSNumber]]).map { $0.map(\.intValue) }
    }

    // MARK: - work-chapters

    /// `ReaderWork.siblingLeaves/chapters` against work-chapters.json.
    func testWorkChapters() throws {
        let vec = Self.loadVector("work-chapters.json")
        XCTAssertTrue((vec["languages"] as! [String]).contains("swift"))
        for raw in vec["cases"] as! [[String: Any]] {
            let name = raw["name"] as! String
            let roots = Self.treeNodes(raw["tree"]!)
            let folder = raw["folder"] as! String
            let (list, cross) = ReaderWork.chapters(for: folder, in: roots)
            XCTAssertEqual(cross, raw["expectCross"] as! Bool, name)
            XCTAssertEqual(list.map(\.fullPath), raw["expectChapterFolders"] as! [String], name)
            let expect = raw["expectChapters"] as! [[String: Any]]
            XCTAssertEqual(list.count, expect.count, name)
            for (ch, e) in zip(list, expect) {
                XCTAssertEqual(ch.name, e["name"] as! String, name)
                XCTAssertEqual(ch.fullPath, e["folder"] as! String, name)
                XCTAssertEqual(ch.images.map(\.name), e["pages"] as! [String], name)
                XCTAssertTrue(ch.images.allSatisfy { $0.folder == ch.fullPath }, name)
            }
        }
    }

    // MARK: - path-lookup

    /// `BrowseModel.node(at:)` + structural parent against path-lookup.json.
    func testPathLookup() throws {
        let vec = Self.loadVector("path-lookup.json")
        XCTAssertTrue((vec["languages"] as! [String]).contains("swift"))
        for raw in vec["cases"] as! [[String: Any]] {
            let name = raw["name"] as! String
            let roots = Self.treeNodes(raw["tree"]!)
            let target = raw["target"] as! String
            let node = target.isEmpty ? nil : BrowseModel.node(at: target, in: roots)
            if let expect = raw["expectNode"] as? [String: Any] {
                let hit = try XCTUnwrap(node, name)
                XCTAssertEqual(hit.name, expect["name"] as! String, name)
                XCTAssertEqual(hit.type, expect["type"] as! String, name)
            } else {
                XCTAssertNil(node, name)
            }
            if let parent = raw["expectParent"] as? String {
                // The parent resolves and lists the target among its folders.
                XCTAssertNotNil(BrowseModel.node(at: parent, in: roots), name)
                XCTAssertTrue(
                    BrowseModel.folders(at: parent, in: roots).map(\.path).contains(target),
                    name)
            } else if let hit = node {
                // Root-level match: the target is a root's own name.
                XCTAssertTrue(roots.contains(where: { $0.name == hit.name }), name)
            }
        }
    }

    // MARK: - encode-path

    /// `ImageURL.encodePath` (+ `/image/` join) against encode-path.json.
    func testEncodePath() throws {
        let vec = Self.loadVector("encode-path.json")
        XCTAssertTrue((vec["languages"] as! [String]).contains("swift"))
        for raw in vec["cases"] as! [[String: Any]] {
            let name = raw["name"] as! String
            let path = raw["path"] as! String
            XCTAssertEqual(ImageURL.encodePath(path), raw["expect"] as! String, name)
            if let folder = raw["folder"] as? String {
                let file = raw["file"] as! String
                let inner = folder.isEmpty ? file : folder + "/" + file
                XCTAssertEqual(ImageURL.encodePath(inner), raw["expect"] as! String, name)
                XCTAssertEqual("/image/" + ImageURL.encodePath(inner),
                               raw["expectImageUrl"] as! String, name)
            }
        }
    }

    // MARK: - solo-spreads

    /// `ReaderWork.rebuildSpreads` against solo-spreads.json.
    func testSoloSpreads() throws {
        let vec = Self.loadVector("solo-spreads.json")
        XCTAssertTrue((vec["languages"] as! [String]).contains("swift"))
        for raw in vec["cases"] as! [[String: Any]] {
            let name = raw["name"] as! String
            let images: [PageRef] = (raw["images"] as! [[String: Any]]).map {
                PageRef(name: $0["name"] as! String,
                        folder: $0["folder"] as! String,
                        isSpacer: ($0["isSpacer"] as? Bool) ?? false)
            }
            let boundaries = Self.ints(raw["chapterBoundaries"]!)
            let mode: ReaderPageMode = (raw["pageMode"] as! String) == "spread" ? .spread : .single
            let offset = (raw["spreadOffset"] as! NSNumber).intValue
            let work = ReaderWork(clickedPath: "", images: images, chapters: [],
                                  boundaries: boundaries, globalToReal: [],
                                  realCount: 0, spreads: [], idxToSpread: [],
                                  pageMode: mode)
            let built = work.rebuildSpreads(offset: offset)
            let expectSpreads = Self.intMatrix(raw["expectSpreads"]!)
            XCTAssertEqual(built.spreads, expectSpreads, name)
            XCTAssertEqual(built.idxToSpread, Self.ints(raw["expectIdxToSpread"]!), name)
            let expectStarts = raw["expectChapterStart"] as! [Bool]
            XCTAssertEqual(expectStarts.count, expectSpreads.count, name)
            for s in expectSpreads.indices {
                XCTAssertEqual(built.chapterStartSpread(s), expectStarts[s], "\(name) spread \(s)")
            }
        }
    }

    // MARK: - chapter-padding

    /// `ReaderWork.assemble` (spacer padding + rebuildSpreads) against
    /// chapter-padding.json — pins the post-shift pad rule shared with
    /// app.js `padChapterSpacers` and Android `ReaderWork.assemble` (#441).
    func testChapterPadding() throws {
        let vec = Self.loadVector("chapter-padding.json")
        XCTAssertTrue((vec["languages"] as! [String]).contains("swift"))
        for raw in vec["cases"] as! [[String: Any]] {
            let name = raw["name"] as! String
            let chapters = (raw["chapters"] as! [[String: Any]]).map { ch -> ReaderChapter in
                let folder = ch["folder"] as! String
                return ReaderChapter(
                    name: folder, fullPath: folder,
                    images: (ch["pages"] as! [String]).map {
                        PageRef(name: $0, folder: folder)
                    })
            }
            let mode: ReaderPageMode = (raw["pageMode"] as! String) == "spread" ? .spread : .single
            let offset = (raw["spreadOffset"] as! NSNumber).intValue
            let work = ReaderWork.assemble(
                clickedPath: chapters.first?.fullPath ?? "",
                chapters: chapters, pageMode: mode, spreadOffset: offset)

            let gotImages = work.images.map {
                [$0.folder, $0.name, $0.isSpacer ? "1" : "0"]
            }
            let expectImages = (raw["expectImages"] as! [[String: Any]]).map {
                [$0["folder"] as! String, $0["name"] as! String,
                 ($0["isSpacer"] as? Bool) == true ? "1" : "0"]
            }
            XCTAssertEqual(gotImages, expectImages, name)
            XCTAssertEqual(work.boundaries, Self.ints(raw["expectBoundaries"]!), name)
            XCTAssertEqual(work.spreads, Self.intMatrix(raw["expectSpreads"]!), name)
            XCTAssertEqual(work.idxToSpread, Self.ints(raw["expectIdxToSpread"]!), name)
            let expectStarts = raw["expectChapterStart"] as! [Bool]
            XCTAssertEqual(expectStarts.count, work.spreads.count, name)
            for s in expectStarts.indices {
                XCTAssertEqual(work.chapterStartSpread(s), expectStarts[s], "\(name) spread \(s)")
            }
        }
    }
}
