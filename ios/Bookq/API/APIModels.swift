import Foundation

/// One node of the `/api/folders` tree. `name` may itself contain "/"
/// (single-child flattening, `app/scanner.py`) — never split on it.
struct TreeNode: Codable, Hashable {
    var name: String
    var type: String            // "folder" | "image"
    var children: [TreeNode]? = nil
    var thumb: String? = nil
    var thumbs: [String]? = nil
    var archive: Bool? = nil
    var pdf: Bool? = nil
    var hasMetadata: Bool? = nil
    var metadata: ComicMetadata? = nil
    var subtreeFacets: [String: [String]]? = nil

    var isFolder: Bool { type == "folder" }
    var isImage: Bool { type == "image" }

    enum CodingKeys: String, CodingKey {
        case name, type, children, thumb, thumbs, archive, pdf, metadata
        case hasMetadata = "has_metadata"
        case subtreeFacets = "subtree_facets"
    }
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
/// Wire shape: {owner, revision, hiddenRoots} — verified against
/// `app/folder_visibility_routes.py` `_respond`.
struct VisibilitySnapshot: Codable, Hashable {
    var owner: String
    var revision: Int
    var hiddenRoots: [String]
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

/// `/api/search` response — {query, total, results:[ComicMetadata]}.
/// Results serialize `ComicMetadata.to_dict()` (snake_case keys).
struct SearchResponse: Codable {
    var query: String
    var total: Int
    var results: [ComicMetadata]
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
