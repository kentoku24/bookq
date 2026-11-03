package dev.bookq.data

import dev.bookq.core.api.TokenPair
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.test.runTest
import mockwebserver3.Dispatcher
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import mockwebserver3.RecordedRequest
import okhttp3.OkHttpClient
import org.junit.After
import org.junit.Before
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/** AuthInterceptor + TokenAuthenticator + AuthManager refresh, end-to-end
 *  through MockWebServer. */
class AuthFlowTest {

    private val server = MockWebServer()

    @Before fun setUp() = server.start()

    @After fun tearDown() = server.close()

    private fun json(code: Int, body: String) =
        MockResponse.Builder().code(code).body(body).build()

    private val treeEnv = """{"kind":"tree","generation":1,"nodes":[]}"""
    private fun pairJson(access: String, refresh: String) =
        """{"access_token":"$access","token_type":"Bearer","expires_in":3600,"refresh_token":"$refresh"}"""

    private fun authManager(): AuthManager {
        val exchange = BookqHttp.plainClient()
        return AuthManager(
            store = InMemoryTokenStore(),
            pendingStore = InMemoryPendingLoginStore(),
            exchangeClient = exchange,
            baseUrlProvider = { server.url("/") },
        )
    }

    private fun apiFor(auth: AuthManager): BookqApi {
        val client = BookqHttp.apiClient(
            baseUrl = server.url("/"),
            tokenProvider = { auth.token },
            refresher = { runBlocking { auth.refreshAccessToken() } },
        )
        return BookqApi(server.url("/"), client, sleep = {})
    }

    /** 401 → refresh → retry carries the rotated Bearer. */
    @Test fun refreshThenRetryNewBearer() = runTest {
        val auth = authManager()
        auth.testSeed(TokenPair("t1", "r1"))
        val api = apiFor(auth)

        val tokenPosts = AtomicInteger(0)
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(req: RecordedRequest): MockResponse = when {
                req.target.startsWith("/api/v1/auth/token") -> {
                    tokenPosts.incrementAndGet()
                    json(200, pairJson("t2", "r2"))
                }
                req.headers["Authorization"] == "Bearer t2" -> json(200, treeEnv)
                else -> json(401, "{}")
            }
        }
        val r = api.tree()
        assertTrue(r is TreeResult.Nodes)
        assertEquals(1, tokenPosts.get())
    }

    /** 5 concurrent 401s coalesce into exactly ONE refresh POST. */
    @Test fun concurrent401sSingleRefresh() = runTest {
        val auth = authManager()
        auth.testSeed(TokenPair("t1", "r1"))
        val api = apiFor(auth)
        val tokenPosts = AtomicInteger(0)

        server.dispatcher = object : Dispatcher() {
            override fun dispatch(req: RecordedRequest): MockResponse = when {
                req.target.startsWith("/api/v1/auth/token") -> {
                    tokenPosts.incrementAndGet()
                    MockResponse.Builder().code(200)
                        .body(pairJson("t2", "r2"))
                        .bodyDelay(150, TimeUnit.MILLISECONDS)
                        .build()
                }
                req.headers["Authorization"] == "Bearer t2" -> json(200, treeEnv)
                else -> json(401, "{}")
            }
        }

        val results = (1..5).map {
            async { runCatching { api.tree() } }
        }.awaitAll()
        assertTrue(results.all { it.isSuccess })
        assertEquals(1, tokenPosts.get())
    }

    /** Refresh failure → the 401 propagates as Unauthorized (→ re-login),
     *  no retry loop. */
    @Test fun refreshFailureUnauthorized() = runTest {
        val auth = authManager()
        auth.testSeed(TokenPair("t1", "r1"))
        val api = apiFor(auth)
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(req: RecordedRequest): MockResponse = json(401, "{}")
        }
        try {
            api.tree()
            fail("expected Unauthorized")
        } catch (e: ApiException.Unauthorized) {
            assertEquals(401, e.status)
        }
    }

    /**
     * Stale-token fast path: a request that failed 401 carrying an OLD
     * bearer while auth.token already moved on (another call refreshed)
     * is retried with the current token — zero extra refresh POSTs.
     */
    @Test fun staleBearerRetriesWithCurrentToken() = runTest {
        val auth = authManager()
        auth.testSeed(TokenPair("t2", "r2"))          // already rotated
        var refreshCalls = 0
        val client = OkHttpClient.Builder()
            // authenticator only — we hand-craft the stale Bearer below
            .authenticator(TokenAuthenticator(
                baseUrl = server.url("/"),
                currentToken = { auth.token },
                refresher = { refreshCalls++; "t3" }))
            .build()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(req: RecordedRequest): MockResponse =
                if (req.headers["Authorization"] == "Bearer t2") json(200, treeEnv)
                else json(401, "{}")
        }
        val req = okhttp3.Request.Builder()
            .url(server.url("/api/v1/tree"))
            .header("Authorization", "Bearer t1")   // stale token on the wire
            .build()
        client.newCall(req).execute().use { assertEquals(200, it.code) }
        assertEquals(0, refreshCalls)
        // 2 hits: the original stale-bearer request + the t2 retry
        assertEquals(2, server.requestCount)
        server.takeRequest(); // drain first (401)
        assertEquals("Bearer t2", server.takeRequest().headers["Authorization"])
    }

    /** Bearer must not be sent to another origin. */
    @Test fun bearerNotSentCrossOrigin() = runTest {
        val other = MockWebServer()
        other.start()
        try {
            other.enqueue(json(200, "{}"))
            val client = BookqHttp.apiClient(
                baseUrl = server.url("/"),          // token is scoped to THIS origin
                tokenProvider = { "secret-token" },
            )
            client.newCall(okhttp3.Request.Builder().url(other.url("/")).build())
                .execute().use { assertEquals(200, it.code) }
            assertNull(other.takeRequest().headers["Authorization"])
        } finally {
            other.close()
        }
    }
}
