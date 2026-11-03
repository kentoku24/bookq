package dev.bookq.data

import dev.bookq.core.api.BookqJson
import dev.bookq.core.api.Facets
import dev.bookq.core.api.ImagePath
import dev.bookq.core.api.ImagesResponse
import dev.bookq.core.api.PositionBody
import dev.bookq.core.api.PositionStatus
import dev.bookq.core.api.PositionsResponse
import dev.bookq.core.api.ReadingPosition
import dev.bookq.core.api.RefreshIndexResponse
import dev.bookq.core.api.SearchResponse
import dev.bookq.core.api.TreeNode
import dev.bookq.core.api.TreeResponse
import dev.bookq.core.api.VersionResponse
import dev.bookq.core.api.VisibilitySnapshot
import kotlinx.coroutines.delay
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import okhttp3.HttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.coroutines.executeAsync
import java.io.IOException

/** Result of `GET /api/v1/tree`. */
sealed interface TreeResult {
    /** `kind` is "tree" (full) or "children" (shallow); generation always
     *  present in v1 envelopes. */
    data class Nodes(
        val kind: String,
        val nodes: List<TreeNode>,
        val generation: Int,
        val etag: String?,
    ) : TreeResult

    data object NotModified : TreeResult
}

/**
 * HTTP client for the bookq API (BookqAPI.swift port). Attach Bearer via
 * [AuthInterceptor]; 401 recovery via [TokenAuthenticator] — both live on
 * the [OkHttpClient], so this class only encodes/decodes.
 */
class BookqApi(
    baseUrl: HttpUrl,
    private val client: OkHttpClient,
    private val sleep: suspend (Long) -> Unit = { delay(it) },
) : VersionProbe {

    /** Base URL normalized to a trailing-slash path so path-append builders
     *  behave like `URL.appendingPathComponent` (works behind a path prefix
     *  such as `https://host/bookq/`). */
    val baseUrl: HttpUrl = baseUrl.withTrailingSlash()

    private val json = BookqJson

    // MARK: - Low level

    private suspend fun send(request: Request): Response =
        try {
            client.newCall(request).executeAsync()
        } catch (e: IOException) {
            throw ApiException.Network(e)
        }

    private fun get(url: HttpUrl): Request =
        Request.Builder().url(url).get().build()

    /** Decode JSON or map a non-2xx status to [ApiException]. */
    private inline fun <reified T> Response.decode(): T {
        use {
            when {
                code == 401 || code == 403 -> throw ApiException.Unauthorized(code)
                code !in 200..299 -> throw ApiException.Status(code)
            }
            return try {
                json.decodeFromString<T>(body.string())
            } catch (e: Exception) {
                throw ApiException.Decode(e)
            }
        }
    }

    // MARK: - Endpoints

    /** GET /api/v1/health — unauthenticated liveness. */
    suspend fun health() {
        val url = baseUrl.newBuilder().addPathSegments("api/v1/health").build()
        send(get(url)).use {
            if (it.code != 200) throw ApiException.Status(it.code)
        }
    }

    /** GET /api/v1/version — also the boot auth probe: 401 = login needed. */
    override suspend fun version(): VersionResponse {
        val url = baseUrl.newBuilder().addPathSegments("api/v1/version").build()
        return send(get(url)).decode()
    }

    /**
     * GET /api/v1/tree. Retries through 202 rebuild responses using
     * `Retry-After` (default 2s, max 20 attempts). 304 → [TreeResult.NotModified].
     */
    suspend fun tree(since: Int? = null, etag: String? = null): TreeResult {
        repeat(20) { attempt ->
            val ub = baseUrl.newBuilder().addPathSegments("api/v1/tree")
            if (since != null) ub.addQueryParameter("since", since.toString())
            val rb = Request.Builder().url(ub.build()).get()
            if (etag != null) rb.header("If-None-Match", etag)
            val resp = send(rb.build())
            when (resp.code) {
                202 -> {
                    val waitS = resp.header("Retry-After")?.toDoubleOrNull() ?: 2.0
                    resp.close()
                    // No trailing sleep after the last 202 (web parity).
                    if (attempt < 19) sleep((waitS * 1000).toLong())
                }
                304 -> {
                    resp.close()
                    return TreeResult.NotModified
                }
                401, 403 -> {
                    resp.close()
                    throw ApiException.Unauthorized(resp.code)
                }
                in 200..299 -> {
                    val env = resp.decode<TreeResponse>()
                    return TreeResult.Nodes(
                        env.kind, env.nodes, env.generation, resp.header("ETag"))
                }
                else -> {
                    resp.close()
                    throw ApiException.Status(resp.code)
                }
            }
        }
        throw ApiException.Status(202)
    }

    /** GET /api/v1/images?folder= — direct image entries of a leaf. */
    suspend fun images(folder: String): List<String> {
        val url = baseUrl.newBuilder().addPathSegments("api/v1/images")
            .addQueryParameter("folder", folder).build()
        return send(get(url)).decode<ImagesResponse>().images
    }

    // MARK: - Reading position / visibility

    /** GET /api/v1/reading-positions — `{positions[]}` newest-first;
     *  `folder` scopes to one work. */
    suspend fun readingPositions(folder: String? = null): List<ReadingPosition> {
        val ub = baseUrl.newBuilder().addPathSegments("api/v1/reading-positions")
        if (folder != null) ub.addQueryParameter("folder", folder)
        return send(get(ub.build())).decode<PositionsResponse>().positions
    }

    /** PUT /api/v1/reading-positions?folder=… — body exactly {page,total,ts}. */
    suspend fun putReadingPosition(pos: ReadingPosition): PositionStatus? {
        val url = baseUrl.newBuilder().addPathSegments("api/v1/reading-positions")
            .addQueryParameter("folder", pos.folder).build()
        val body = json.encodeToString(PositionBody.serializer(),
            PositionBody(pos.page, pos.total, pos.ts))
            .toRequestBody(JSON_MEDIA)
        val req = Request.Builder().url(url).put(body).build()
        val resp = send(req)
        resp.use {
            when {
                it.code == 401 || it.code == 403 ->
                    throw ApiException.Unauthorized(it.code)
                it.code !in 200..299 -> throw ApiException.Status(it.code)
            }
            return try {
                json.decodeFromString<PositionStatus>(it.body.string())
            } catch (e: Exception) {
                null // ok/stale is informational — any 2xx counts as accepted
            }
        }
    }

    /** DELETE /api/v1/reading-positions — `folder` null wipes all records. */
    suspend fun deleteReadingPosition(folder: String? = null) {
        val ub = baseUrl.newBuilder().addPathSegments("api/v1/reading-positions")
        if (folder != null) ub.addQueryParameter("folder", folder)
        val req = Request.Builder().url(ub.build()).delete().build()
        send(req).use {
            if (it.code !in 200..299) throw ApiException.Status(it.code)
        }
    }

    suspend fun folderVisibility(): VisibilitySnapshot {
        val url = baseUrl.newBuilder()
            .addPathSegments("api/v1/folder-visibility").build()
        return send(get(url)).decode()
    }

    /** PUT /api/v1/folder-visibility — body exactly {root,hidden}, owner
     *  proof via `X-Bookq-Visibility-Owner`. Response is the authoritative
     *  post-write snapshot. */
    suspend fun setFolderVisibility(
        root: String,
        hidden: Boolean,
        owner: String,
    ): VisibilitySnapshot {
        val url = baseUrl.newBuilder()
            .addPathSegments("api/v1/folder-visibility").build()
        val body = JsonObject(mapOf(
            "root" to JsonPrimitive(root),
            "hidden" to JsonPrimitive(hidden),
        )).toString().toRequestBody(JSON_MEDIA)
        val req = Request.Builder().url(url).put(body)
            .header("X-Bookq-Visibility-Owner", owner).build()
        return send(req).decode()
    }

    // MARK: - Search / facets

    /**
     * `_fetchBuilding` equivalent (search/facets.js): 202 index-building
     * responses retry on `min(1.5*(n+1),5.0)`s backoff, max 10 attempts.
     * Returns null when still building after all attempts.
     */
    private suspend fun fetchBuilding(url: HttpUrl): Response? {
        repeat(10) { attempt ->
            val resp = send(get(url))
            if (resp.code != 202) return resp
            resp.close()
            // web's `_fetchBuilding` returns after the 10th 202 without
            // sleeping — 9 waits, not 10.
            if (attempt == 9) return null
            val waitS = minOf(1.5 * (attempt + 1), 5.0)
            sleep((waitS * 1000).toLong())
        }
        return null
    }

    /** GET /api/v1/search — filters artist/tag comma-joined; null = still
     *  building after 10 attempts. */
    suspend fun search(
        q: String,
        artist: List<String>,
        tag: List<String>,
        includeHidden: Boolean,
    ): SearchResponse? {
        val ub = baseUrl.newBuilder().addPathSegments("api/v1/search")
            .addQueryParameter("q", q)
        if (artist.isNotEmpty()) ub.addQueryParameter("artist", artist.joinToString(","))
        if (tag.isNotEmpty()) ub.addQueryParameter("tag", tag.joinToString(","))
        ub.addQueryParameter("sort", "relevance")
        if (includeHidden) ub.addQueryParameter("include_hidden", "1")
        val resp = fetchBuilding(ub.build()) ?: return null
        return resp.decode()
    }

    /** GET /api/v1/facets — `path` scopes to a subtree ("" = whole library). */
    suspend fun facets(path: String = "", includeHidden: Boolean): Facets? {
        val ub = baseUrl.newBuilder().addPathSegments("api/v1/facets")
        if (path.isNotEmpty()) ub.addQueryParameter("path", path)
        if (includeHidden) ub.addQueryParameter("include_hidden", "1")
        val resp = fetchBuilding(ub.build()) ?: return null
        return resp.decode()
    }

    /** POST /api/v1/index/refresh — synchronous metadata rebuild. */
    suspend fun refreshIndex(): RefreshIndexResponse {
        val url = baseUrl.newBuilder()
            .addPathSegments("api/v1/index/refresh").build()
        val req = Request.Builder().url(url)
            .post("".toRequestBody(JSON_MEDIA)).build()
        return send(req).decode()
    }

    // MARK: - Media URL builders (byte-exact web/iOS parity)

    /** Build a URL from an *already-encoded* path — going through
     *  `addPathSegment` would re-encode "%" → "%25". */
    private fun encodedPathUrl(encodedPath: String): HttpUrl.Builder {
        // baseUrl.encodedPath always ends with "/" (withTrailingSlash).
        return baseUrl.newBuilder()
            .encodedPath(baseUrl.encodedPath + encodedPath)
    }

    /** `/thumb/<encoded rel>?w=` — `rel` is `folder/name` or bare `name`. */
    fun thumbUrl(folder: String, name: String, w: Int = 400): HttpUrl {
        val rel = if (folder.isEmpty()) name else "$folder/$name"
        return encodedPathUrl("thumb/" + ImagePath.encodePath(rel))
            .addQueryParameter("w", w.toString())
            .build()
    }

    /**
     * Multi-thumb composite sprite — one request for the whole tile grid.
     * `names` are `thumbs[]` entries; each is `encodeSegment`-encoded and
     * joined with literal commas (appended via encoded query parameter so
     * the commas survive).
     */
    fun thumbSpriteUrl(
        folder: String,
        names: List<String>,
        rows: List<Int>,
        w: Int = 400,
    ): HttpUrl {
        val encodedNames = names.joinToString(",", transform = ImagePath::encodeSegment)
        return baseUrl.newBuilder()
            .addPathSegments("api/v1/thumb-sprite")
            .addQueryParameter("folder", folder)
            .addQueryParameter("w", w.toString())
            .addQueryParameter("v", "2")
            // encoded variant so the joined commas stay literal (iOS parity)
            .addEncodedQueryParameter("rows", rows.joinToString(","))
            .addEncodedQueryParameter("names", encodedNames)
            .build()
    }

    /** Reader page URL — `/view/` (normalized) or `/image/` (original). */
    fun pageUrl(folder: String, name: String, original: Boolean): HttpUrl {
        val rel = if (folder.isEmpty()) name else "$folder/$name"
        return encodedPathUrl((if (original) "image/" else "view/") +
            ImagePath.encodePath(rel)).build()
    }

    /**
     * `ws(s)://<host>/ws/reader-sync` — the dual-reader sync endpoint.
     * Returned as a String because OkHttp `HttpUrl` only models
     * http(s) schemes (Bearer rides the upgrade request — M2+).
     */
    fun readerSyncUrl(): String? {
        val http = baseUrl.newBuilder()
            .encodedPath("/ws/reader-sync")
            .query(null)
            .build()
            .toString()
        return when (baseUrl.scheme) {
            "https" -> http.replaceFirst("https://", "wss://")
            "http" -> http.replaceFirst("http://", "ws://")
            else -> null
        }
    }

    companion object {
        private val JSON_MEDIA = "application/json; charset=utf-8".toMediaType()
    }
}

/** Ensure the base path ends with "/" so `addPathSegments`/`encodedPath`
 *  appends instead of replacing the last segment. */
internal fun HttpUrl.withTrailingSlash(): HttpUrl {
    if (encodedPath.endsWith("/")) return this
    return newBuilder().encodedPath("$encodedPath/").build()
}
