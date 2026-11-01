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

    /// v1 tree envelope — `{kind,nodes,generation}` wraps every mode.
    func testTreeResponseEnvelope() throws {
        let json = #"{"kind":"tree","nodes":[{"name":"a","type":"folder"}],"generation":42}"#
        let env = try JSONDecoder().decode(TreeResponse.self, from: Data(json.utf8))
        XCTAssertEqual(env.kind, "tree")
        XCTAssertEqual(env.generation, 42)
        XCTAssertEqual(env.nodes.count, 1)
    }

    func testTreeResponseChildrenKind() throws {
        let json = #"{"kind":"children","nodes":[],"generation":1}"#
        let env = try JSONDecoder().decode(TreeResponse.self, from: Data(json.utf8))
        XCTAssertEqual(env.kind, "children")
        XCTAssertTrue(env.nodes.isEmpty)
    }

    /// v1 positions list — `{positions[]}` newest-first.
    func testPositionsResponse() throws {
        let json = #"{"positions":[{"folder":"a/b","page":3,"total":10,"ts":1000}]}"#
        let env = try JSONDecoder().decode(PositionsResponse.self, from: Data(json.utf8))
        XCTAssertEqual(env.positions.first?.folder, "a/b")
        XCTAssertEqual(env.positions.first?.page, 3)
        XCTAssertEqual(env.positions.first?.ts, 1000)
    }

    func testPositionsResponseEmpty() throws {
        let env = try JSONDecoder().decode(PositionsResponse.self,
                                           from: Data(#"{"positions":[]}"#.utf8))
        XCTAssertTrue(env.positions.isEmpty)
    }

    /// v1 PUT body carries {page,total,ts} ONLY — `folder` rides the query.
    func testPositionBodyEncodesWithoutFolder() throws {
        let data = try JSONEncoder().encode(PositionBody(page: 3, total: 10, ts: 1234))
        let obj = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        XCTAssertNil(obj["folder"])
        XCTAssertEqual(obj["page"] as? Int, 3)
        XCTAssertEqual(obj["total"] as? Int, 10)
        XCTAssertEqual(obj["ts"] as? Int, 1234)
    }
}
