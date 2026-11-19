package dev.bookq.data

import dev.bookq.core.api.TokenPair
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.withContext
import mockwebserver3.Dispatcher
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import mockwebserver3.RecordedRequest
import org.junit.After
import org.junit.Before
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.fail
import org.junit.Test
import java.util.Base64
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

@OptIn(ExperimentalCoroutinesApi::class)
class AuthManagerTest {

    private val server = MockWebServer()

    @Before fun setUp() = server.start()

    @After fun tearDown() = server.close()

    private fun b64(s: String) =
        Base64.getUrlEncoder().withoutPadding().encodeToString(s.toByteArray())
    private fun jwt(sub: String = "u@x.jp", name: String = "U") =
        "${b64("{}")}.${b64("{\"sub\":\"$sub\",\"name\":\"$name\",\"exp\":9999999999}")}.sig"
    private fun pairJson(access: String = jwt(), refresh: String = "r2") =
        """{"access_token":"$access","token_type":"Bearer","expires_in":3600,"refresh_token":"$refresh"}"""

    private fun manager(): Pair<AuthManager, InMemoryTokenStore> {
        val store = InMemoryTokenStore()
        val auth = AuthManager(
            store = store,
            pendingStore = InMemoryPendingLoginStore(),
            exchangeClient = BookqHttp.plainClient(),
            baseUrlProvider = { server.url("/") },
        )
        return auth to store
    }

    // MARK: - callback validation

    private suspend fun seeded(): Pair<AuthManager, InMemoryPendingLoginStore> {
        val pending = InMemoryPendingLoginStore()
        val auth = AuthManager(
            store = InMemoryTokenStore(),
            pendingStore = pending,
            exchangeClient = BookqHttp.plainClient(),
            baseUrlProvider = { server.url("/") },
        )
        auth.beginLogin(server.url("/"))
        return auth to pending
    }

    @Test fun wrongSchemeRejected() = runTest {
        val (auth, _) = seeded()
        try {
            auth.completeLogin("https://auth?code=c&state=x")
            fail("expected MalformedCallback")
        } catch (e: AuthManager.AuthError.MalformedCallback) { }
    }

    @Test fun missingCodeRejected() = runTest {
        val (auth, _) = seeded()
        try {
            auth.completeLogin("bookq://auth?state=x")
            fail("expected NoCode")
        } catch (e: AuthManager.AuthError.NoCode) { }
    }

    @Test fun stateMismatchRejected() = runTest {
        val (auth, pending) = seeded()
        try {
            auth.completeLogin("bookq://auth?code=c&state=WRONG")
            fail("expected StateMismatch")
        } catch (e: AuthManager.AuthError.StateMismatch) { }
    }

    @Test fun noPendingLoginRejected() = runTest {
        val (auth, _) = manager()
        try {
            auth.completeLogin("bookq://auth?code=c&state=x")
            fail("expected NoPendingLogin")
        } catch (e: AuthManager.AuthError.NoPendingLogin) { }
    }

    // MARK: - exchange + persistence

    @Test fun successfulExchangePersistsPairAndUser() = runTest {
        val (auth, pending) = seeded()
        val store = InMemoryTokenStore()
        // rebuild with shared store so we can inspect persistence
        val auth2 = AuthManager(
            store = store,
            pendingStore = pending,
            exchangeClient = BookqHttp.plainClient(),
            baseUrlProvider = { server.url("/") },
        )
        val state = pending.load()!!.state
        server.enqueue(MockResponse.Builder().code(200).body(pairJson()).build())
        auth2.completeLogin("bookq://auth?code=abc&state=$state")

        val req = server.takeRequest()
        assertEquals("POST", req.method)
        val body = req.body?.utf8() ?: ""
        assertTrue(body.contains("\"grant_type\":\"authorization_code\""))
        assertTrue(body.contains("\"code\":\"abc\""))
        assertTrue(body.contains("code_verifier"))

        val pair = store.load()
        assertNotNull(pair)
        assertEquals("r2", pair?.refreshToken)
        assertEquals(pair?.accessToken, auth2.token)
        assertEquals("u@x.jp", auth2.user.value?.email)
        assertNull(pending.load())
    }

    // MARK: - refresh

    @Test fun refreshNoTokenReturnsNull() = runTest {
        val (auth, _) = manager()
        assertNull(auth.refreshAccessToken())
        assertEquals(0, server.requestCount)
    }

    @Test fun refreshRotationSwapsPair() = runTest {
        val store = InMemoryTokenStore()
        val auth = AuthManager(store, InMemoryPendingLoginStore(),
            BookqHttp.plainClient(), { server.url("/") })
        auth.testSeed(TokenPair(jwt(), "r1"))
        server.enqueue(MockResponse.Builder().code(200).body(pairJson(refresh = "r2")).build())
        val tok = auth.refreshAccessToken()
        assertNotNull(tok)
        val body = server.takeRequest().body?.utf8() ?: ""
        assertTrue(body.contains("\"grant_type\":\"refresh_token\""))
        assertTrue(body.contains("\"refresh_token\":\"r1\""))
        assertEquals("r2", store.load()?.refreshToken)
        assertEquals(tok, auth.token)
        assertEquals("u@x.jp", auth.user.value?.email)
    }

    @Test fun concurrentRefreshCoalesced() = runTest {
        val (auth, _) = manager()
        auth.testSeed(TokenPair(jwt(), "r1"))
        val hits = AtomicInteger(0)
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(req: RecordedRequest): MockResponse {
                hits.incrementAndGet()
                return MockResponse.Builder().code(200)
                    .body(pairJson(refresh = "r2"))
                    .bodyDelay(150, TimeUnit.MILLISECONDS)
                    .build()
            }
        }
        val results = (1..5).map { async { auth.refreshAccessToken() } }.awaitAll()
        assertEquals(1, hits.get())
        assertTrue(results.all { it != null })
        assertTrue(results.all { it == results[0] })
    }

    @Test fun refreshFailureReturnsNull() = runTest {
        val (auth, _) = manager()
        auth.testSeed(TokenPair(jwt(), "r1"))
        server.enqueue(MockResponse.Builder().code(401).body("{}").build())
        assertNull(auth.refreshAccessToken())
    }

    /** Refresh rejected 401 → session dead: store cleared, token gone,
     *  sessionExpired emitted. */
    @Test fun refresh401LogsOutAndEmitsExpired() = runTest {
        val store = InMemoryTokenStore()
        val auth = AuthManager(store, InMemoryPendingLoginStore(),
            BookqHttp.plainClient(), { server.url("/") })
        auth.testSeed(TokenPair(jwt(), "r1"))
        server.enqueue(MockResponse.Builder().code(401).body("{}").build())
        // replay-0 flow — subscribe BEFORE the refresh emits or the
        // event is dropped (the unconfined collector resumes inline on
        // the emitting IO thread during tryEmit).
        var fired = false
        backgroundScope.launch(UnconfinedTestDispatcher(testScheduler)) {
            auth.sessionExpired.collect { fired = true }
        }
        assertNull(auth.refreshAccessToken())
        assertTrue(fired)
        assertNull(auth.token)
        assertNull(store.load())
    }

    /** Refresh rejected 400 → same definitive-death routing. */
    @Test fun refresh400AlsoExpires() = runTest {
        val store = InMemoryTokenStore()
        val auth = AuthManager(store, InMemoryPendingLoginStore(),
            BookqHttp.plainClient(), { server.url("/") })
        auth.testSeed(TokenPair(jwt(), "r1"))
        server.enqueue(MockResponse.Builder().code(400).body("{}").build())
        var fired = false
        backgroundScope.launch(UnconfinedTestDispatcher(testScheduler)) {
            auth.sessionExpired.collect { fired = true }
        }
        assertNull(auth.refreshAccessToken())
        assertTrue(fired)
        assertNull(store.load())
    }

    /** Network failure during refresh → transient: no logout, no event. */
    @Test fun refreshNetworkErrorKeepsSession() = runTest {
        val dead = MockWebServer()
        dead.start()
        val deadUrl = dead.url("/")
        dead.close()                              // connection refused
        val store = InMemoryTokenStore()
        val auth = AuthManager(store, InMemoryPendingLoginStore(),
            BookqHttp.plainClient(), { deadUrl })
        auth.testSeed(TokenPair(jwt(), "r1"))
        var fired = false
        backgroundScope.launch(UnconfinedTestDispatcher(testScheduler)) {
            auth.sessionExpired.collect { fired = true }
        }
        assertNull(auth.refreshAccessToken())
        // Real-time grace — a stray emit on the IO thread would land on
        // the unconfined collector. (Virtual time can't prove absence.)
        withContext(Dispatchers.Default) { delay(300) }
        assertFalse(fired)
        assertEquals(jwt(), auth.token)           // session kept
    }

    private fun assertTrue(b: Boolean) = org.junit.Assert.assertTrue(b)
    private fun assertFalse(b: Boolean) = org.junit.Assert.assertFalse(b)
}
