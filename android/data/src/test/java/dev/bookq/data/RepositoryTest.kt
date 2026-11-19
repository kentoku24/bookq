package dev.bookq.data

import dev.bookq.core.visibility.VisibilityState
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * Two-stage tree load, visibility `_resolveGet` mapping, and progress
 * boot-merge semantics — MockWebServer-backed (BookqApiTest conventions).
 * Repo jobs join()ed for determinism (real HTTP to localhost is fast).
 */
class RepositoryTest {

    private val server = MockWebServer()

    @Before fun setUp() = server.start()
    @After fun tearDown() = server.close()

    private fun api() = BookqApi(server.url("/"), BookqHttp.plainClient(), sleep = {})

    private fun json(code: Int, body: String, headers: Map<String, String> = emptyMap()) =
        MockResponse.Builder().code(code).apply {
            headers.forEach { (k, v) -> addHeader(k, v) }
            body(body)
        }.build()

    private fun tree(gen: Int, vararg names: String) =
        """{"kind":"tree","generation":$gen,"nodes":[${names.joinToString(",") {
            """{"name":"$it","type":"folder"}""" }}]}"""

    private fun shallowTree() =
        """{"kind":"children","generation":1,"nodes":[{"name":"a","type":"folder"}]}"""

    private fun testScope(): CoroutineScope =
        CoroutineScope(Job() + Dispatchers.Default)

    // MARK: - TreeRepository

    @Test fun shallowFirstPaintThenFull() = runBlocking {
        server.enqueue(json(200, shallowTree()))
        server.enqueue(json(200, tree(5, "a", "b")))
        val snap = TreeRepository.MemorySnapshotIO()
        val repo = TreeRepository(api(), java.io.File("."), testScope(), snapshot = snap)
        withTimeout(5_000) { repo.load().join() }
        // Depth param went on the first request; the full request has none.
        assertTrue(server.takeRequest().url.toString().contains("depth=1"))
        assertFalse(server.takeRequest().url.toString().contains("depth"))
        val st = repo.state.value
        assertFalse(st.shallow)
        assertTrue(st.treeLoaded)
        assertEquals(5, st.generation)
        assertEquals(2, st.nodes.size)
        // Snapshot write is post-paint background work — poll for it.
        withTimeout(5_000) { while (snap.text == null) kotlinx.coroutines.delay(10) }
        assertFalse(snap.text!!.contains("\"kind\":\"children\""))
    }

    @Test fun fullFailureAfterShallowKeepsShallowGrid() = runBlocking {
        server.enqueue(json(200, shallowTree()))
        server.enqueue(json(500, "{}"))
        val repo = TreeRepository(api(), java.io.File("."), testScope(),
            snapshot = TreeRepository.MemorySnapshotIO())
        withTimeout(5_000) { repo.load().join() }
        val st = repo.state.value
        assertTrue(st.shallow)
        assertFalse(st.treeLoaded)
        assertNull(st.error) // shallow paint survives — no error banner
    }

    @Test fun totalFailureFallsBackToSnapshot() = runBlocking {
        server.enqueue(json(500, "{}"))
        server.enqueue(json(500, "{}"))
        val snap = TreeRepository.MemorySnapshotIO().apply {
            text = """{"nodes":[{"name":"cached","type":"folder"}],"generation":9}"""
        }
        val repo = TreeRepository(api(), java.io.File("."), testScope(), snapshot = snap)
        withTimeout(5_000) { repo.load().join() }
        val st = repo.state.value
        assertTrue(st.treeLoaded)
        assertEquals("cached", st.nodes[0].name)
        assertEquals(9, st.generation)
    }

    @Test fun totalFailureNoSnapshotErrors() = runBlocking {
        server.enqueue(json(500, "{}"))
        server.enqueue(json(500, "{}"))
        val repo = TreeRepository(api(), java.io.File("."), testScope(),
            snapshot = TreeRepository.MemorySnapshotIO())
        withTimeout(5_000) { repo.load().join() }
        assertNotNull(repo.state.value.error)
        assertFalse(repo.state.value.treeLoaded)
    }

    @Test fun shallowDoesNotClobberRestoredSnapshot() = runBlocking {
        // A restored FULL snapshot must never be swapped for the shallow
        // envelope — if stage 2 then fails the grid would lose data and
        // stay shallow forever (polling only runs when treeLoaded).
        server.enqueue(json(500, "{}"))
        val snap = TreeRepository.MemorySnapshotIO().apply {
            text = """{"nodes":[{"name":"cached","type":"folder"}],"generation":9}"""
        }
        val repo = TreeRepository(api(), java.io.File("."), testScope(), snapshot = snap)
        assertTrue(repo.restoreSnapshot())
        withTimeout(5_000) { repo.load().join() }
        val st = repo.state.value
        assertTrue(st.treeLoaded)
        assertFalse(st.shallow)
        assertEquals("cached", st.nodes[0].name)
        assertEquals(9, st.generation)
        // The depth=1 stage was skipped entirely — one full fetch only.
        assertFalse(server.takeRequest().url.toString().contains("depth"))
        assertEquals(1, server.requestCount)
    }

    @Test fun refreshJobCompletesOnIndexFailure() = runBlocking {
        // The pull-to-refresh spinner is cleared via Job completion, so
        // a refreshIndex failure must still complete the Job.
        server.enqueue(json(500, "{}"))
        val repo = TreeRepository(api(), java.io.File("."), testScope(),
            snapshot = TreeRepository.MemorySnapshotIO())
        withTimeout(5_000) { repo.refresh().join() }
        assertNotNull(repo.state.value.error)
    }

    @Test fun staleLoadResultDiscardedByEpoch() = runBlocking {
        // A second load() supersedes the first — its full-tree result must
        // not clobber the newer state (app.js `_loadFoldersGen`). Each
        // load holds the mutex for both stages, so request order is
        // shallow/full/shallow/full. Response #2's body is delayed so
        // j1's full apply lands AFTER j2's `++loadGen` — deterministic.
        server.enqueue(json(200, shallowTree()))
        server.enqueue(MockResponse.Builder().code(200)
            .bodyDelay(300, java.util.concurrent.TimeUnit.MILLISECONDS)
            .body(tree(1, "old")).build())
        server.enqueue(json(200, shallowTree()))
        server.enqueue(json(200, tree(9, "new")))
        val repo = TreeRepository(api(), java.io.File("."), testScope(),
            snapshot = TreeRepository.MemorySnapshotIO())
        val j1 = repo.load()
        // Wait for j1's shallow paint, then bump the epoch with j2 —
        // j1's delayed full result is now stale and must be discarded.
        withTimeout(5_000) { while (!repo.state.value.shallow) kotlinx.coroutines.delay(10) }
        val j2 = repo.load()
        withTimeout(5_000) { j1.join(); j2.join() }
        val st = repo.state.value
        assertTrue(st.treeLoaded)
        assertEquals(9, st.generation)
        assertEquals("new", st.nodes[0].name)
    }

    // MARK: - VisibilityRepository

    private fun visBody(owner: String, rev: Int, vararg roots: String) =
        """{"owner":"$owner","revision":$rev,"hiddenRoots":[${roots.joinToString(",") { "\"$it\"" }}]}"""

    @Test fun visibilityGetAppliesReady() = runBlocking {
        server.enqueue(json(200, visBody("u@x", 3, "Secret")))
        val cache = VisibilityRepository.MemoryCacheIO()
        val repo = VisibilityRepository(api(), testScope(), cache)
        withTimeout(5_000) { repo.refresh().join() }
        assertEquals(VisibilityState.Status.READY, repo.vis.status)
        assertFalse(repo.vis.readOnly)
        assertTrue(repo.vis.isHidden("Secret"))
        assertTrue(repo.vis.isVisible("Other"))
        assertNotNull(cache.text)
    }

    @Test fun visibilityAuthFailIsError() = runBlocking {
        server.enqueue(json(403, "{}"))
        val repo = VisibilityRepository(api(), testScope(),
            VisibilityRepository.MemoryCacheIO())
        withTimeout(5_000) { repo.refresh().join() }
        assertEquals(VisibilityState.Status.ERROR, repo.vis.status)
        assertFalse(repo.vis.hasSnapshot)
        // Fail-closed: nothing is visible without a snapshot.
        assertFalse(repo.vis.isVisible("Anything"))
    }

    @Test fun visibilityNetErrWithCacheIsReadonly() = runBlocking {
        server.enqueue(json(503, "{}"))
        val cache = VisibilityRepository.MemoryCacheIO().apply {
            text = """{"v":1,"owner":"u@x","revision":2,"hiddenRoots":["Secret"]}"""
        }
        val repo = VisibilityRepository(api(), testScope(), cache)
        withTimeout(5_000) { repo.refresh().join() }
        assertEquals(VisibilityState.Status.READONLY, repo.vis.status)
        assertTrue(repo.vis.readOnly)
        assertTrue(repo.vis.isHidden("Secret"))
    }

    @Test fun visibilityNetErrNoCacheIsError() = runBlocking {
        server.enqueue(json(503, "{}"))
        val repo = VisibilityRepository(api(), testScope(),
            VisibilityRepository.MemoryCacheIO())
        withTimeout(5_000) { repo.refresh().join() }
        assertEquals(VisibilityState.Status.ERROR, repo.vis.status)
        assertFalse(repo.vis.isVisible("Anything"))
    }

    // MARK: - ProgressRepository

    private fun positionsBody(vararg entries: String): String =
        """{"positions":[${entries.joinToString(",")}]}"""

    private fun pos(folder: String, page: Int, total: Int, ts: Long) =
        """{"folder":"$folder","page":$page,"total":$total,"ts":$ts}"""

    @Test fun progressInitReadsStore() {
        val store = ProgressRepository.MemoryStoreIO().apply {
            text = """{"f/a":{"page":2,"total":30,"ts":100}}"""
        }
        val repo = ProgressRepository(api(), testScope(), store)
        val p = repo.book["f/a"]
        assertNotNull(p)
        assertEquals(2, p!!.page)
        assertEquals(30, p.total)
        assertEquals(100, p.ts)
    }

    @Test fun progressSaveDedupesSamePosition() = runBlocking {
        server.enqueue(json(200, """{"status":"ok"}"""))
        val store = ProgressRepository.MemoryStoreIO()
        val repo = ProgressRepository(api(), testScope(), store,
            clock = { 1000L }, saveDebounceMs = 0, pushDebounceMs = 1)
        repo.save("f/a", 3, 30)
        repo.save("f/a", 3, 30) // identical — second save is a no-op
        // Await the armed send — flush() can't join a send that already
        // took `pending` (its sendNow returns early on inFlight), so a
        // flush-first assert can run before the PUT lands.
        withTimeout(5_000) { while (server.requestCount == 0) kotlinx.coroutines.delay(10) }
        repo.flush().join() // ON_STOP flush must not duplicate the send
        assertEquals(1, repo.book.all().size)
        // Exactly one PUT — the deduped second save never re-queues.
        assertEquals(1, server.requestCount)
        withTimeout(5_000) { while (store.text == null) kotlinx.coroutines.delay(10) }
    }

    @Test fun progressPullRemoteNewerMerges() = runBlocking {
        server.enqueue(json(200, positionsBody(pos("f/b", 9, 40, 5000))))
        val store = ProgressRepository.MemoryStoreIO().apply {
            text = """{"f/b":{"page":1,"total":40,"ts":100}}"""
        }
        val repo = ProgressRepository(api(), testScope(), store)
        assertTrue(repo.pull())
        assertEquals(9, repo.book["f/b"]!!.page)
        assertEquals(5000, repo.book["f/b"]!!.ts)
        assertNotNull(store.text)
    }

    @Test fun progressPullLocalNewerPushesUp() = runBlocking {
        server.enqueue(json(200, positionsBody(pos("f/b", 1, 40, 100))))
        server.enqueue(json(200, """{"status":"ok"}"""))
        val store = ProgressRepository.MemoryStoreIO().apply {
            text = """{"f/b":{"page":9,"total":40,"ts":9000}}"""
        }
        val repo = ProgressRepository(api(), testScope(), store)
        assertFalse(repo.pull())
        assertEquals(9, repo.book["f/b"]!!.page) // local kept
        val get = server.takeRequest()
        assertEquals("GET", get.method)
        val put = server.takeRequest()
        assertEquals("PUT", put.method)
    }

    @Test fun progressPullMergeStillPushesLocalNewest() = runBlocking {
        // Remote wins f/b while local f/a is newer than anything remote —
        // BOTH must happen: merge f/b AND catch-up PUT f/a. An early
        // return after the merge would swallow the push (sync.js pushes
        // regardless of whether the merge applied).
        server.enqueue(json(200, positionsBody(pos("f/b", 3, 40, 5000))))
        server.enqueue(json(200, """{"status":"ok"}"""))
        val store = ProgressRepository.MemoryStoreIO().apply {
            text = """{"f/a":{"page":7,"total":20,"ts":9000},
                |"f/b":{"page":1,"total":40,"ts":100}}""".trimMargin()
        }
        val repo = ProgressRepository(api(), testScope(), store)
        assertTrue(repo.pull())
        assertEquals(3, repo.book["f/b"]!!.page) // remote merged
        assertEquals(7, repo.book["f/a"]!!.page) // local kept
        assertEquals("GET", server.takeRequest().method)
        val put = server.takeRequest()
        assertEquals("PUT", put.method)
        assertEquals("f/a", put.url.queryParameter("folder"))
    }

    @Test fun progressPullNetworkErrorIsNoop() = runBlocking {
        server.enqueue(json(503, "{}"))
        val store = ProgressRepository.MemoryStoreIO().apply {
            text = """{"f/b":{"page":9,"total":40,"ts":9000}}"""
        }
        val repo = ProgressRepository(api(), testScope(), store)
        assertFalse(repo.pull())
        // Crucially: no catch-up PUT — a stale local must not clobber remote.
        assertEquals(1, server.requestCount)
    }

    @Test fun progressSavePushesOnFlush() = runBlocking {
        server.enqueue(json(200, """{"status":"ok"}"""))
        val repo = ProgressRepository(api(), testScope(),
            ProgressRepository.MemoryStoreIO(), pushDebounceMs = 60_000)
        repo.save("f/a", 4, 20)
        withTimeout(5_000) { repo.flush().join() }
        val req = server.takeRequest()
        assertEquals("PUT", req.method)
    }

    @Test fun flushDuringInFlightPutDoesNotCancel() = runBlocking {
        // PUT1 in flight when a second save + flush lands: the flight
        // must finish (sync.js never cancels a send), then re-arm the
        // queued record — 2 PUTs total, no wedged inFlight.
        server.enqueue(MockResponse.Builder().code(200)
            .bodyDelay(400, java.util.concurrent.TimeUnit.MILLISECONDS)
            .body("""{"status":"ok"}""").build())
        server.enqueue(json(200, """{"status":"ok"}"""))
        val repo = ProgressRepository(api(), testScope(),
            ProgressRepository.MemoryStoreIO(), pushDebounceMs = 1)
        repo.save("f/a", 1, 20)
        assertEquals("PUT", server.takeRequest().method) // PUT1 flying
        repo.save("f/a", 2, 20)                          // queues behind it
        withTimeout(5_000) { repo.flush().join() }       // must not kill PUT1
        val put2 = server.takeRequest()
        assertEquals("PUT", put2.method)
        assertEquals("f/a", put2.url.queryParameter("folder"))
    }
}
