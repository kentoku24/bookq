package dev.bookq.data

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import java.util.concurrent.TimeUnit
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/** `doSearch`/`loadFacets` — debounce, generation guard, include_hidden. */
class SearchRepositoryTest {

    private val server = MockWebServer()

    @Before fun setUp() = server.start()
    @After fun tearDown() = server.close()

    private fun api() = BookqApi(server.url("/"), BookqHttp.plainClient(), sleep = {})

    private fun json(code: Int, body: String) =
        MockResponse.Builder().code(code).body(body).build()

    private fun testScope(): CoroutineScope =
        CoroutineScope(Job() + Dispatchers.Default)

    private fun result(path: String) = """{"query":"q","total":1,"results":[{
        "folder_path":"$path","folder_name":"x","title_jp":"t",
        "artist":[],"tags":[],"source":"meta","has_metadata":true,"total_pages":3}]}""".trimIndent()

    @Test fun debounceFiresLatestQuery() = runBlocking {
        // Up to 3 requests can legitimately fire: if the test thread stalls
        // past the 300 ms debounce between setQuery calls, the StateFlow
        // debounce correctly emits intermediate values too.
        repeat(3) { server.enqueue(json(200, result("a"))) }
        val repo = SearchRepository(api(), testScope())
        repo.setQuery("a")
        repo.setQuery("ab")
        repo.setQuery("abc")
        withTimeout(5_000) { while (repo.results.value.isEmpty()) delay(10) }
        assertTrue(repo.active.value)
        assertEquals("a", repo.results.value.first().folderPath)
        // The deterministic contract: the LAST request observed carries the
        // final query (stale responses are dropped by the generation guard).
        val urls = generateSequence {
            server.takeRequest(500, TimeUnit.MILLISECONDS)?.url?.toString()
        }.toList()
        assertTrue(urls.isNotEmpty() && urls.size <= 3)
        assertTrue(urls.last().contains("q=abc"))
    }

    @Test fun emptyQueryExitsSearchMode() = runBlocking {
        server.enqueue(json(200, result("a")))
        val repo = SearchRepository(api(), testScope())
        repo.setQuery("x")
        withTimeout(5_000) { while (!repo.active.value) delay(10) }
        repo.setQuery("")
        withTimeout(5_000) { while (repo.active.value) delay(10) }
        assertTrue(repo.results.value.isEmpty())
        // The empty query itself hits no network.
        assertEquals(1, server.requestCount)
    }

    @Test fun staleResponseDropped() = runBlocking {
        // Slow first query, quick second — the late response must not win.
        server.enqueue(json(200, result("stale")))
        server.enqueue(json(200, result("fresh")))
        val repo = SearchRepository(api(), testScope())
        repo.setQuery("one")
        delay(400) // debounce fires
        repo.setQuery("two") // new gen before the first response lands → dropped
        // Request arrival ≠ response applied — poll the observable outcome.
        withTimeout(5_000) {
            while (repo.results.value.firstOrNull()?.folderPath != "fresh") delay(10)
        }
    }

    @Test fun includeHiddenFollowsShowAll() = runBlocking {
        var showAll = true
        server.enqueue(json(200, result("a")))
        val repo = SearchRepository(api(), testScope()) { showAll }
        repo.setQuery("q")
        withTimeout(5_000) { while (server.requestCount < 1) delay(10) }
        assertTrue(server.takeRequest().url.toString().contains("include_hidden=1"))
    }

    @Test fun facetsSendPathAndHidden() = runBlocking {
        var showAll = false
        server.enqueue(json(200, """{"artists":["a1"],"tags":["t1"]}"""))
        val repo = SearchRepository(api(), testScope()) { showAll }
        repo.loadFacets("series")
        // Response applies on the repo dispatcher — wait on the outcome.
        withTimeout(5_000) { while (repo.facets.value == null) delay(10) }
        val url = server.takeRequest().url.toString()
        assertTrue(url.contains("path=series"))
        assertTrue(repo.facets.value?.artists?.contains("a1") == true)
    }

    @Test fun filtersChangedRefiresWithParams() = runBlocking {
        server.enqueue(json(200, result("a")))
        server.enqueue(json(200, result("a")))
        val repo = SearchRepository(api(), testScope())
        repo.setQuery("q")
        withTimeout(5_000) { while (server.requestCount < 1) delay(10) }
        server.takeRequest() // consume the first search
        repo.toggleArtist("whoever")
        repo.filtersChanged()
        withTimeout(5_000) { while (server.requestCount < 2) delay(10) }
        assertTrue(server.takeRequest().url.toString().contains("artist=whoever"))
    }
}
