package dev.bookq.core.api

import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

/**
 * JSON for all wire payloads: tolerant reads (`ignoreUnknownKeys`,
 * `explicitNulls = false`) matching the iOS client. Re-encode side of the
 * spec-conformance test enables `encodeDefaults` separately.
 */
@OptIn(ExperimentalSerializationApi::class)
val BookqJson: Json = Json {
    ignoreUnknownKeys = true
    explicitNulls = false
}

/** One node of `/api/v1/tree`. `name` may contain "/" (single-child
 *  flattening in `app/scanner.py`) — never split on it. */
@Serializable
data class TreeNode(
    val name: String,
    val type: String, // "folder" | "image"
    val children: List<TreeNode>? = null,
    val thumb: String? = null,
    val thumbs: List<String>? = null,
    val archive: Boolean? = null,
    val pdf: Boolean? = null,
    @SerialName("has_metadata") val hasMetadata: Boolean? = null,
    val metadata: ComicMetadata? = null,
    @SerialName("subtree_facets") val subtreeFacets: Facets? = null,
) {
    val isFolder: Boolean get() = type == "folder"
    val isImage: Boolean get() = type == "image"
}

/** `/api/v1/tree` envelope — `kind` "tree" (full) or "children" (depth). */
@Serializable
data class TreeResponse(
    val kind: String,
    val nodes: List<TreeNode>,
    val generation: Int,
)

@Serializable
data class ComicMetadata(
    @SerialName("folder_path") val folderPath: String,
    @SerialName("folder_name") val folderName: String,
    @SerialName("title_jp") val titleJp: String = "",
    @SerialName("title_en") val titleEn: String = "",
    val artist: List<String>,
    val tags: List<String>,
    val source: String,
    @SerialName("has_metadata") val hasMetadata: Boolean,
    @SerialName("total_pages") val totalPages: Int,
) {
    val displayTitle: String
        get() = titleJp.ifEmpty { titleEn.ifEmpty { folderName } }
}

/** `/api/v1/facets` response — union of subtree_facets ({artists,tags}). */
@Serializable
data class Facets(
    val artists: List<String> = emptyList(),
    val tags: List<String> = emptyList(),
)

/** `/api/v1/images` — direct children only, non-recursive. */
@Serializable
data class ImagesResponse(
    val folder: String,
    val images: List<String>,
)

/** `/api/v1/reading-positions` record — per-folder LWW, opaque folder. */
@Serializable
data class ReadingPosition(
    val folder: String,
    val page: Int,
    val total: Int,
    val ts: Long, // client timestamp (ms epoch)
)

@Serializable
data class PositionsResponse(
    val positions: List<ReadingPosition>,
)

/** PUT body — `folder` travels on the query, never in the body (v1). */
@Serializable
data class PositionBody(
    val page: Int,
    val total: Int,
    val ts: Long,
)

/** PUT/DELETE position response — {"status":"ok"|"stale"}. */
@Serializable
data class PositionStatus(val status: String)

/** `/api/v1/folder-visibility` per-user snapshot. */
@Serializable
data class VisibilitySnapshot(
    val owner: String,
    val revision: Int,
    val hiddenRoots: List<String>,
)

/** `/api/v1/version` — {version, sha, build_date} (sha/build_date tolerant). */
@Serializable
data class VersionResponse(
    val version: String,
    val sha: String? = null,
    @SerialName("build_date") val buildDate: String? = null,
)

/** `/api/v1/auth/token` — short-lived access + rotating refresh. */
@Serializable
data class TokenPairResponse(
    @SerialName("access_token") val accessToken: String,
    @SerialName("token_type") val tokenType: String,
    @SerialName("expires_in") val expiresIn: Int,
    @SerialName("refresh_token") val refreshToken: String? = null,
)

/** `/api/v1/search` — {query, total, results:[ComicMetadata]}. */
@Serializable
data class SearchResponse(
    val query: String,
    val total: Int,
    val results: List<ComicMetadata>,
)

/** `POST /api/v1/index/refresh` — synchronous rebuild result. */
@Serializable
data class RefreshIndexResponse(
    val status: String,
    val folders: Int,
)

/** `/api/v1/health`. */
@Serializable
data class HealthResponse(val status: String)

/** Authenticated user — decoded locally from access-token JWT claims
 *  (`sub`/`name`/`picture`); v1 has no /me endpoint. */
@Serializable
data class AuthUser(
    val email: String,
    val name: String,
    val picture: String? = null,
)

/** Access + refresh pair — persisted as ONE record so the pair is written
 *  atomically (a refresh response must never land half-saved). */
@Serializable
data class TokenPair(
    val accessToken: String,
    val refreshToken: String,
) {
    constructor(r: TokenPairResponse) : this(r.accessToken, r.refreshToken ?: "")
}
