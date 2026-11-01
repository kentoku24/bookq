import Foundation

/// One node of the `/api/v1/tree` tree. `name` may itself contain "/"
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

/// Authenticated user identity — decoded locally from the access-token
/// JWT claims (`sub`/`name`/`picture`); v1 has no /me endpoint.
struct AuthUser: Codable, Hashable {
    var email: String
    var name: String
    var picture: String?
}

/// `/api/v1/version` — {version, sha, build_date}.
struct VersionResponse: Codable, Hashable {
    var version: String
    var sha: String?
    var build_date: String?
}

/// `/api/v1/folder-visibility` response (per-user snapshot).
/// Wire shape: {owner, revision, hiddenRoots} — verified against
/// `app/folder_visibility_routes.py` `_respond`.
struct VisibilitySnapshot: Codable, Hashable {
    var owner: String
    var revision: Int
    var hiddenRoots: [String]
}

/// `/api/v1/reading-positions` record — per-folder LWW, opaque folder
/// string. GET wraps in {positions:[…]} newest-first; PUT takes `folder`
/// on the query and {page,total,ts} in the body.
struct ReadingPosition: Codable, Hashable {
    var folder: String
    var page: Int
    var total: Int
    var ts: Int                    // client timestamp (ms)
}

struct PositionsResponse: Codable {
    var positions: [ReadingPosition]
}

/// PUT body — `folder` travels on the query, never in the body (v1).
struct PositionBody: Codable {
    var page: Int
    var total: Int
    var ts: Int
}

/// PUT/DELETE position response — {"status":"ok"|"stale"}.
struct PositionStatus: Codable {
    var status: String
}

/// `/api/v1/search` response — {query, total, results:[ComicMetadata]}.
/// Results serialize `ComicMetadata.to_dict()` (snake_case keys).
struct SearchResponse: Codable {
    var query: String
    var total: Int
    var results: [ComicMetadata]
}

/// `/api/v1/facets` response — union of subtree_facets.
struct Facets: Codable {
    var artists: [String] = []
    var tags: [String] = []
}

/// `/api/v1/tree` response — every mode returns the same envelope:
/// `kind` "tree" (全木 / since-mismatch re-send) or "children" (depth
/// mode). `since` unchanged → bare 304 with no body.
struct TreeResponse: Codable {
    var kind: String
    var nodes: [TreeNode]
    var generation: Int
}

/// `/api/v1/images` response — direct children only, non-recursive.
struct ImagesResponse: Codable {
    var folder: String
    var images: [String]
}

/// `/api/v1/auth/token` response — short-lived access + rotating refresh.
struct TokenPairResponse: Codable {
    var access_token: String
    var token_type: String
    var expires_in: Int
    var refresh_token: String?
}
