package dev.bookq.data

import dev.bookq.core.api.AuthUser
import dev.bookq.core.api.BookqJson
import dev.bookq.core.api.TokenPair
import dev.bookq.core.api.TokenPairResponse
import dev.bookq.core.auth.JwtClaims
import dev.bookq.core.auth.Pkce
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.withContext
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.coroutines.executeAsync
import java.io.IOException

/**
 * Holds the native bearer token pair and runs the RFC 8252 PKCE login:
 * `GET /auth/login?client=native&code_challenge=…` → `bookq://auth?code=…`
 * → `POST /api/v1/auth/token` (see `app/auth.py`, `app/auth_token.py`).
 */
class AuthManager(
    private val store: TokenStore,
    private val pendingStore: PendingLoginStore,
    /** Client for the token exchange — must NOT carry the auth interceptor
     *  (the exchange is the Bearer bootstrap; no cookie mixing either). */
    private val exchangeClient: OkHttpClient,
    private val baseUrlProvider: () -> HttpUrl?,
    private val scope: CoroutineScope =
        CoroutineScope(SupervisorJob() + Dispatchers.IO),
) {

    /** Result of [beginLogin] — hand [authorizeUrl] to the browser. */
    data class LoginRequest(
        val authorizeUrl: String,
        val state: String,
        val verifier: String,
    )

    sealed class AuthError(message: String) : Exception(message) {
        data object Cancelled : AuthError("ログインがキャンセルされました")
        data object NoToken : AuthError("認証トークンを取得できませんでした")
        data object MalformedCallback : AuthError("認証トークンを取得できませんでした")
        data object NoCode : AuthError("認証トークンを取得できませんでした")
        data object NoPendingLogin : AuthError("認証トークンを取得できませんでした")
        data object StateMismatch : AuthError("認証状態が一致しません (state)")
        class Exchange(val status: Int) :
            AuthError("トークン交換に失敗しました (HTTP $status)")
        class Network(cause: Throwable) :
            AuthError("ネットワークエラー: ${cause.message}")
    }

    /** Bearer for the interceptor — thread-safe, readable off-coroutine. */
    @Volatile
    var token: String? = null
        private set

    private val _user = MutableStateFlow<AuthUser?>(null)
    val user: StateFlow<AuthUser?> = _user.asStateFlow()

    @Volatile
    private var refreshToken: String? = null

    /** Emitted once when the session is definitively dead: the refresh
     *  exchange was rejected (400/401) — the presented refresh is consumed
     *  server-side either way, so we also log out. Network failures during
     *  refresh do NOT emit (transient — the session may still be fine). */
    private val _sessionExpired = MutableSharedFlow<Unit>(extraBufferCapacity = 1)
    val sessionExpired: SharedFlow<Unit> = _sessionExpired.asSharedFlow()

    /**
     * IO hop for keystore/file work. `scope.coroutineContext` itself
     * would reparent the block to the scope's supervisor (caller
     * cancellation could no longer reach it); dropping the Job keeps
     * the caller's structured concurrency while only borrowing the
     * dispatcher (IO in prod, the test scheduler under runTest).
     */
    private val ioContext = scope.coroutineContext.minusKey(Job)

    /** Restore the persisted pair at launch. */
    suspend fun restore() = withContext(ioContext) {
        val pair = store.load() ?: return@withContext
        token = pair.accessToken
        refreshToken = pair.refreshToken
        _user.value = JwtClaims.userFromAccessToken(pair.accessToken)
    }

    /**
     * Step 1 of RFC 8252: build the authorize URL and persist the pending
     * login so a process death during the browser round-trip can still
     * complete. Hand `authorizeUrl` to Auth Tab / a Custom Tab.
     */
    suspend fun beginLogin(base: HttpUrl): LoginRequest {
        val verifier = Pkce.makeVerifier()
        val state = Pkce.makeVerifier()
        val url = base.withTrailingSlash().newBuilder()
            .addPathSegments("auth/login")
            .addQueryParameter("client", "native")
            .addQueryParameter("code_challenge", Pkce.challenge(verifier))
            .addQueryParameter("code_challenge_method", "S256")
            .addQueryParameter("state", state)
            .build()
        withContext(ioContext) {
            pendingStore.save(PendingLogin(base.toString(), state, verifier))
        }
        return LoginRequest(url.toString(), state, verifier)
    }

    /**
     * Step 2: consume `bookq://auth?code=…&state=…`, verify the echoed
     * state against the persisted pending login, exchange the code for a
     * token pair, persist it, and expose the decoded user.
     */
    suspend fun completeLogin(callbackUri: String): AuthUser {
        // java.net.URI — OkHttp HttpUrl only accepts http(s) schemes, so
        // it cannot parse the `bookq://` custom-scheme callback.
        val uri = try {
            java.net.URI(callbackUri)
        } catch (e: java.net.URISyntaxException) {
            throw AuthError.MalformedCallback
        }
        if (uri.scheme != CALLBACK_SCHEME || uri.host != "auth") {
            throw AuthError.MalformedCallback
        }
        val params = parseQuery(uri.rawQuery)
        val code = params["code"]?.takeIf { it.isNotEmpty() }
            ?: throw AuthError.NoCode
        val pending = withContext(ioContext) { pendingStore.load() }
            ?: throw AuthError.NoPendingLogin
        // CSRF guard: the echoed state must match the one we sent.
        val state = params["state"]
        if (state != pending.state) throw AuthError.StateMismatch
        val base = pending.baseUrl.toHttpUrlOrNull() ?: throw AuthError.MalformedCallback
        val resp = exchange(base, mapOf(
            "grant_type" to "authorization_code",
            "code" to code,
            "code_verifier" to pending.verifier,
        ))
        applyPair(TokenPair(resp))
        withContext(ioContext) { pendingStore.clear() }
        return _user.value ?: throw AuthError.NoToken
    }

    // MARK: - Refresh

    /**
     * Return a fresh access token, or null when refresh is impossible /
     * failed (caller then drops to the login screen). Single-flight:
     * concurrent callers share one in-flight exchange — the rotation
     * contract consumes the presented refresh either way, so replaying it
     * would kill the session.
     */
    private val refreshMutex = Mutex()
    private var refreshJob: Deferred<String?>? = null

    suspend fun refreshAccessToken(): String? {
        val job = refreshMutex.withLock {
            refreshJob ?: scope.async { doRefresh() }.also { refreshJob = it }
        }
        return job.await()
    }

    private suspend fun doRefresh(): String? {
        try {
            val rt = refreshToken ?: return null
            val base = baseUrlProvider() ?: return null
            val resp = exchange(base, mapOf(
                "grant_type" to "refresh_token",
                "refresh_token" to rt,
            ))
            val pair = TokenPair(resp)
            applyPair(pair)
            return pair.accessToken
        } catch (e: AuthError.Exchange) {
            if (e.status == 400 || e.status == 401) {
                // Definitive session death — drop local state and tell the
                // app to route to Login.
                logout()
                _sessionExpired.tryEmit(Unit)
            }
            return null
        } catch (e: Exception) {
            return null
        } finally {
            refreshMutex.withLock { refreshJob = null }
        }
    }

    private suspend fun applyPair(pair: TokenPair) {
        withContext(ioContext) { store.save(pair) }
        token = pair.accessToken
        refreshToken = pair.refreshToken
        _user.value = JwtClaims.userFromAccessToken(pair.accessToken)
    }

    /** POST /api/v1/auth/token — unauthenticated bootstrap endpoint. */
    private suspend fun exchange(base: HttpUrl, body: Map<String, String>): TokenPairResponse {
        val url = base.withTrailingSlash().newBuilder()
            .addPathSegments("api/v1/auth/token").build()
        val jsonBody = JsonObject(body.mapValues { JsonPrimitive(it.value) })
            .toString().toRequestBody(JSON_MEDIA)
        val request = Request.Builder().url(url).post(jsonBody).build()
        val resp = try {
            exchangeClient.newCall(request).executeAsync()
        } catch (e: IOException) {
            throw AuthError.Network(e)
        }
        resp.use {
            if (it.code !in 200..299) throw AuthError.Exchange(it.code)
            return try {
                BookqJson.decodeFromString<TokenPairResponse>(it.body.string())
            } catch (e: Exception) {
                throw AuthError.NoToken
            }
        }
    }

    suspend fun logout() {
        // Volatile state drops first so the very next request stops
        // carrying the old bearer; the file clears ride IO.
        token = null
        refreshToken = null
        _user.value = null
        withContext(ioContext) {
            store.clear()
            pendingStore.clear()
        }
    }

    /** Test hook — inject a pair as if login/refresh had returned it. */
    fun testSeed(pair: TokenPair) {
        token = pair.accessToken
        refreshToken = pair.refreshToken
        _user.value = JwtClaims.userFromAccessToken(pair.accessToken)
    }

    companion object {
        const val CALLBACK_SCHEME = "bookq"
        private val JSON_MEDIA = "application/json; charset=utf-8".toMediaType()

        /** Percent-decoded query map for the custom-scheme callback. */
        private fun parseQuery(raw: String?): Map<String, String> {
            if (raw.isNullOrEmpty()) return emptyMap()
            val out = LinkedHashMap<String, String>()
            for (pair in raw.split('&')) {
                val eq = pair.indexOf('=')
                val k = if (eq >= 0) pair.substring(0, eq) else pair
                val v = if (eq >= 0) pair.substring(eq + 1) else ""
                out[java.net.URLDecoder.decode(k, Charsets.UTF_8)] =
                    java.net.URLDecoder.decode(v, Charsets.UTF_8)
            }
            return out
        }
    }
}
