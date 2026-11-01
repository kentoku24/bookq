package dev.bookq.data

import dev.bookq.core.api.TokenPair
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

class BookqApiTest {

    private val server = MockWebServer()

    @Before fun setUp() = server.start()

    @After fun tearDown() = server.close()

    private fun api(
        client: OkHttpClient = BookqHttp.plainClient(),
        sleeps: MutableList<Long> = mutableListOf(),
    ) = BookqApi(server.url("/"), client, sleep = { sleeps.add(it) })

    private fun json(code: Int, body: String, headers: Map<String, String> = emptyMap()) =
        MockResponse.Builder().code(code).apply {
            headers.forEach { (k, v) -> addHeader(k, v) }
            body(body)
        }.build()

    private val treeEnv = """{"kind":"tree","generation":7,"nodes":[{"name":"a","type":"folder"}]}"""

    // MARK: - tree 202 / 304 / since+etag

    @Test fun tree202Then200HonorsRetryAfter() = runTest {
        val sleeps = mutableListOf<Long>()
        server.enqueue(json(202, "{}", mapOf("Retry-After" to "3")))
        server.enqueue(json(200, treeEnv))
        val r = api(sleeps = sleeps).tree()
        assertTrue(r is TreeResult.Nodes)
        r as TreeResult.Nodes
        assertEquals(2, server.requestCount)
        assertEquals(listOf(3000L), sleeps)
        assertEquals("tree", r.kind)
        assertEquals(7, r.generation)
    }

    @Test fun tree202DefaultRetryAfter() = runTest {
        val sleeps = mutableListOf<Long>()
        server.enqueue(json(202, "{}"))
        server.enqueue(json(200, treeEnv))
        api(sleeps = sleeps).tree()
        assertEquals(listOf(2000L), sleeps)
    }

    @Test fun tree304NotModified() = runTest {
        server.enqueue(json(304, ""))
        val r = api().tree(since = 7)
        assertTrue(r is TreeResult.NotModified)
    }

    @Test fun treeSinceAndEtagOnWire() = runTest {
        server.enqueue(json(304, ""))
        api().tree(since = 3, etag = "\"d-g3\"")
        val req = server.takeRequest()
        assertEquals("3", req.url.queryParameter("since"))
        assertEquals("\"d-g3\"", req.headers["If-None-Match"])
    }

    // MARK: - reading positions

    @Test fun putReadingPositionBodyAndQuery() = runTest {
        server.enqueue(json(200, """{"status":"ok"}"""))
        api().putReadingPosition(
            dev.bookq.core.api.ReadingPosition("a/b", 3, 10, 1234))
        val req = server.takeRequest()
        assertEquals("PUT", req.method)
        assertEquals("a/b", req.url.queryParameter("folder"))
        assertEquals("""{"page":3,"total":10,"ts":1234}""", req.body?.utf8())
    }

    @Test fun readingPositionsFolderQuery() = runTest {
        server.enqueue(json(200, """{"positions":[]}"""))
        api().readingPositions("x")
        assertEquals("x", server.takeRequest().url.queryParameter("folder"))
    }

    // MARK: - visibility

    @Test fun visibilityOwnerHeader() = runTest {
        server.enqueue(json(200, """{"owner":"o","revision":2,"hiddenRoots":["r"]}"""))
        val snap = api().setFolderVisibility("r", true, "owner-token")
        val req = server.takeRequest()
        assertEquals("owner-token", req.headers["X-Bookq-Visibility-Owner"])
        assertEquals("""{"root":"r","hidden":true}""", req.body?.utf8())
        assertEquals(listOf("r"), snap.hiddenRoots)
    }

    // MARK: - search / facets building backoff

    @Test fun searchBuildingBackoffThen200() = runTest {
        val sleeps = mutableListOf<Long>()
        server.enqueue(json(202, "{}"))
        server.enqueue(json(200, """{"query":"x","total":0,"results":[]}"""))
        val r = api(sleeps = sleeps).search("x", emptyList(), emptyList(), false)
        assertEquals(2, server.requestCount)
        assertEquals(listOf(1500L), sleeps)
        assertEquals(0, r?.total)
    }

    @Test fun searchStillBuildingReturnsNull() = runTest {
        val sleeps = mutableListOf<Long>()
        repeat(10) { server.enqueue(json(202, "{}")) }
        val r = api(sleeps = sleeps).facets(includeHidden = false)
        assertNull(r)
        assertEquals(10, server.requestCount)
        assertEquals(
            listOf(1500L, 3000L, 4500L, 5000L, 5000L, 5000L, 5000L, 5000L, 5000L, 5000L),
            sleeps)
    }

    // MARK: - URL builders (byte-exact)

    @Test fun mediaUrls() = runTest {
        val a = api()
        assertEquals(
            server.url("/").toString() + "thumb/A%20B/p%201.jpg?w=400",
            a.thumbUrl("A B", "p 1.jpg").toString())
        assertEquals(
            server.url("/").toString() +
                "view/A%20B/%E7%AC%AC1%E5%B7%BB/p%201.png",
            a.pageUrl("A B/第1巻", "p 1.png", original = false).toString())
        assertEquals(
            server.url("/").toString() + "image/A/p.png",
            a.pageUrl("A", "p.png", original = true).toString())
        assertEquals(
            server.url("/").toString() +
                "api/v1/thumb-sprite?folder=F&w=400&v=2&rows=1,1&names=a%20b.jpg,c.jpg",
            a.thumbSpriteUrl("F", listOf("a b.jpg", "c.jpg"), listOf(1, 1)).toString())
        val ws = a.readerSyncUrl().toString()
        assertTrue(ws.startsWith("ws://"))
        assertTrue(ws.endsWith("/ws/reader-sync"))
    }

    /** Base URL with a path prefix must resolve media under it. */
    @Test fun urlsWithBasePathPrefix() = runTest {
        val prefixed = BookqApi(server.url("/sub"), BookqHttp.plainClient())
        assertEquals(
            server.url("/sub/thumb/A/p.jpg?w=200").toString(),
            prefixed.thumbUrl("A", "p.jpg", w = 200).toString())
        server.enqueue(json(200, treeEnv))
        prefixed.tree()
        assertEquals("/sub/api/v1/tree", server.takeRequest().target)
    }

    // MARK: - error mapping

    @Test fun serverErrorMapsToStatus() = runTest {
        server.enqueue(json(500, "{}"))
        try {
            api().version()
            fail("expected Status")
        } catch (e: ApiException.Status) {
            assertEquals(500, e.code)
        }
    }

    @Test fun forbiddenMapsToUnauthorized() = runTest {
        server.enqueue(json(403, "{}"))
        try {
            api().version()
            fail("expected Unauthorized")
        } catch (e: ApiException.Unauthorized) {
            assertEquals(403, e.status)
        }
    }
}
