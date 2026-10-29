import Foundation

/// One node of the `/api/folders` tree. `name` may itself contain "/"
/// (single-child flattening, `app/scanner.py`) — never split on it.
struct TreeNode: Codable, Hashable {
    var name: String
    var type: String            // "folder" | "image"
    var children: [TreeNode]?
    var thumb: String?
    var thumbs: [String]?
    var archive: Bool?
    var pdf: Bool?
    var hasMetadata: Bool?
    var metadata: ComicMetadata?
    var subtreeFacets: [String: [String]]?

    var isFolder: Bool { type == "folder" }
    var isImage: Bool { type == "image" }
}

struct ComicMetadata: Codable, Hashable {
    var folder_path: String = ""
    var folder_name: String = ""
    var title_jp: String = ""
    var title_en: String = ""
    var artist: [String] = []
    var tags: [String] = []
    var source: String = ""
    var has_metadata: Bool = false
    var total_pages: Int = 0

    var displayTitle: String {
        !title_jp.isEmpty ? title_jp : (!title_en.isEmpty ? title_en : folder_name)
    }
}

struct MeResponse: Codable, Hashable {
    var email: String
    var name: String
    var picture: String?
}

struct VersionResponse: Codable, Hashable {
    var version: String
    var latest: String?
    var updateAvailable: Bool?
    var releaseURL: String?
}

/// `/api/folder-visibility` response (per-user snapshot).
struct VisibilitySnapshot: Codable, Hashable {
    var owner: String
    var revision: Int
    var hidden: [String]
}

/// `/api/reading-position` record — single latest position per user.
/// PUT body: {folder, page, total, ts}; GET wraps it in {"position": rec?}.
struct ReadingPosition: Codable, Hashable {
    var folder: String
    var page: Int
    var total: Int
    var ts: Int                    // client timestamp (ms)
    var updated_at: Double?        // server-assigned write time
}

struct PositionEnvelope: Codable {
    var position: ReadingPosition?
}

/// `/api/search` response: searchPath → matched nodes.
struct SearchResults: Codable {
    var searchPath: String
    var results: [TreeNode]
    enum CodingKeys: String, CodingKey {
        case results
        case searchPath = "search_path"
    }
}

/// `/api/facets` response — union of subtree_facets.
struct Facets: Codable {
    var artists: [String] = []
    var tags: [String] = []
}

/// `/api/folders?since=` incremental payload (full response is a bare array).
struct FoldersDelta: Codable {
    var tree: [TreeNode]
    var generation: Int
}

/// `/api/folders` payload: either bare `[TreeNode]` (full) or delta object.
enum FoldersPayload: Codable {
    case full([TreeNode], generation: Int?)
    case delta(tree: [TreeNode], generation: Int)

    init(from decoder: Decoder) throws {
        if let arr = try? [TreeNode](from: decoder) {
            self = .full(arr, generation: nil)
            return
        }
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self = .delta(tree: try c.decode([TreeNode].self, forKey: .tree),
                      generation: try c.decode(Int.self, forKey: .generation))
    }

    func encode(to encoder: Encoder) throws {
        switch self {
        case .full(let arr, _):
            var c = encoder.unkeyedContainer()
            try c.encode(contentsOf: arr)
        case .delta(let tree, let generation):
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(tree, forKey: .tree)
            try c.encode(generation, forKey: .generation)
        }
    }

    private enum CodingKeys: String, CodingKey {
        case tree, generation
    }
}
