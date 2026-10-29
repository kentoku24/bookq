import XCTest
@testable import Bookq

final class APIModelTests: XCTestCase {
    func testTreeNodeDecodesTreeArray() throws {
        let json = """
        [{"name":"a","type":"folder","children":[
          {"name":"b/c","type":"folder","children":[{"name":"p1.jpg","type":"image"}],
            "thumb":"b/c/p1.jpg","thumbs":["b/c/p1.jpg"]}
        ],"thumb":"b/c/p1.jpg"}]
        """
        let nodes = try JSONDecoder().decode([TreeNode].self, from: Data(json.utf8))
        XCTAssertEqual(nodes.count, 1)
        XCTAssertEqual(nodes[0].children?[0].name, "b/c")
        XCTAssertEqual(nodes[0].children?[0].thumb, "b/c/p1.jpg")
    }

    func testFoldersPayloadBareArray() throws {
        let json = #"[{"name":"a","type":"folder"}]"#
        let payload = try JSONDecoder().decode(FoldersPayload.self, from: Data(json.utf8))
        guard case .full(let tree, _) = payload else { return XCTFail() }
        XCTAssertEqual(tree.count, 1)
    }

    func testFoldersPayloadDelta() throws {
        let json = #"{"tree":[{"name":"a","type":"folder"}],"generation":42}"#
        let payload = try JSONDecoder().decode(FoldersPayload.self, from: Data(json.utf8))
        guard case .delta(let tree, let gen) = payload else { return XCTFail() }
        XCTAssertEqual(gen, 42)
        XCTAssertEqual(tree.count, 1)
    }

    func testReadingPositionEnvelope() throws {
        let json = #"{"position":{"folder":"a/b","page":3,"total":10,"ts":1000,"updated_at":5.0}}"#
        let env = try JSONDecoder().decode(PositionEnvelope.self, from: Data(json.utf8))
        XCTAssertEqual(env.position?.folder, "a/b")
        XCTAssertEqual(env.position?.page, 3)
        XCTAssertEqual(env.position?.ts, 1000)
    }

    func testReadingPositionNull() throws {
        let env = try JSONDecoder().decode(PositionEnvelope.self,
                                           from: Data(#"{"position":null}"#.utf8))
        XCTAssertNil(env.position)
    }

    func testReadingPositionEncodesPutBody() throws {
        let pos = ReadingPosition(folder: "a/b", page: 3, total: 10, ts: 1234)
        let data = try JSONEncoder().encode(pos)
        let obj = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        XCTAssertEqual(obj["folder"] as? String, "a/b")
        XCTAssertEqual(obj["page"] as? Int, 3)
        XCTAssertEqual(obj["total"] as? Int, 10)
        XCTAssertEqual(obj["ts"] as? Int, 1234)
    }
}
