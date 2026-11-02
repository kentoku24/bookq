package dev.bookq.data

import dev.bookq.core.visibility.VisibilityState
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.async
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/** `setHidden` write path — `browse/visibility.js` PUT semantics. */
class VisibilityWriteTest {

    private val server = MockWebServer()

    @Before fun setUp() = server.start()
    @After fun tearDown() = server.close()

    private fun api() = BookqApi(server.url("/"), BookqHttp.plainClient(), sleep = {})

    private fun json(code: Int, body: String) =
        MockResponse.Builder().code(code).body(body).build()

    private fun testScope(): CoroutineScope =
        CoroutineScope(Job() + Dispatchers.Default)

    private fun snapshot(roots: List<String>, rev: Int = 1, owner: String = "u1") =
        """{"owner":"$owner","revision":$rev,"hiddenRoots":[${roots.joinToString(",") { "\"$it\"" }}]}"""

    private fun repoWith(vararg hidden: String): VisibilityRepository {
        // Seed a READY snapshot without network.
        val repo = VisibilityRepository(api(), testScope(),
            VisibilityRepository.MemoryCacheIO())
        repo.vis.applySnapshot("u1", 1, hidden.toList())
        repo.vis.setStatus(VisibilityState.Status.READY)
        return repo
    }

    @Test fun putAppliesResponseSnapshot() = runBlocking {
        server.enqueue(json(200, snapshot(listOf("secret"), rev = 2)))
        val repo = repoWith()
        repo.setHidden("secret", true)
        val req = server.takeRequest()
        assertEquals("PUT", req.method)
        assertEquals("u1", req.headers["X-Bookq-Visibility-Owner"])
        assertTrue(repo.vis.isHidden("secret"))
        assertFalse(repo.vis.readOnly)
        assertEquals(VisibilityState.Status.READY, repo.vis.status)
    }

    @Test fun putConflictRefreshesAndThrows() = runBlocking {
        server.enqueue(json(409, "{}"))
        server.enqueue(json(200, snapshot(listOf("x"), rev = 5, owner = "u2")))
        val repo = repoWith()
        try { repo.setHidden("a", true); error("expected throw") }
        catch (e: VisibilityRepository.SaveException) {
            assertEquals(VisibilityRepository.SaveError.CONFLICT, e.error)
        }
        withTimeout(5_000) { while (server.requestCount < 2) kotlinx.coroutines.delay(10) }
        // 409 → error+owner, convergence GET applied the authoritative owner.
        assertEquals(VisibilityRepository.LastError.OWNER, repo.lastError)
        assertEquals("u2", repo.vis.owner)
        assertTrue(repo.vis.isHidden("x"))
    }

    @Test fun putAuthFailIsError() = runBlocking {
        server.enqueue(json(403, "{}"))
        val repo = repoWith()
        try { repo.setHidden("a", true); error("expected throw") }
        catch (e: VisibilityRepository.SaveException) {
            assertEquals(VisibilityRepository.SaveError.SAVE, e.error)
        }
        assertEquals(VisibilityRepository.LastError.AUTH, repo.lastError)
        assertEquals(VisibilityState.Status.ERROR, repo.vis.status)
    }

    @Test fun putServerErrorIsSaveError() = runBlocking {
        server.enqueue(json(503, "{}"))
        val repo = repoWith()
        try { repo.setHidden("a", true); error("expected throw") }
        catch (e: VisibilityRepository.SaveException) {
            assertEquals(VisibilityRepository.SaveError.SAVE, e.error)
        }
        assertEquals(VisibilityRepository.LastError.SAVE, repo.lastError)
        // Local snapshot untouched — tile state unchanged.
        assertFalse(repo.vis.isHidden("a"))
    }

    @Test fun stalePutResponseTriggersConvergenceGet() = runBlocking {
        // PUT response rev=1 is BEHIND the local rev=5 (another device's
        // GET landed mid-flight) → not adopted; one convergence GET.
        server.enqueue(json(200, snapshot(listOf("stale"), rev = 1)))
        server.enqueue(json(200, snapshot(listOf("fresh"), rev = 6)))
        val repo = repoWith()
        repo.vis.applySnapshot("u1", 5, emptyList())
        repo.setHidden("a", true)
        assertFalse(repo.vis.isHidden("stale")) // stale PUT payload dropped
        withTimeout(5_000) { while (server.requestCount < 2) kotlinx.coroutines.delay(10) }
        assertTrue(repo.vis.isHidden("fresh"))
        assertEquals(6, repo.vis.revision)
    }

    @Test fun putRejectsBusyReadonlyInvalid() = runBlocking {
        // readOnly → OFFLINE without a request
        val ro = repoWith()
        ro.vis.setStatus(VisibilityState.Status.READONLY, readOnly = true)
        try { ro.setHidden("a", true); error("expected throw") }
        catch (e: VisibilityRepository.SaveException) {
            assertEquals(VisibilityRepository.SaveError.OFFLINE, e.error)
        }
        // invalid root → INVALID without a request
        val r = repoWith()
        try { r.setHidden("a/../b", true); error("expected throw") }
        catch (e: VisibilityRepository.SaveException) {
            assertEquals(VisibilityRepository.SaveError.INVALID, e.error)
        }
        assertEquals(0, server.requestCount)
    }

    @Test fun putBusyLockRejectsSecond() = runBlocking {
        // Hold the first PUT in-flight (no response enqueued yet → send
        // blocks waiting), second call must BUSY out.
        val repo = repoWith()
        val first = async { runCatching { repo.setHidden("a", true) } }
        withTimeout(5_000) { while (!repo.saving) kotlinx.coroutines.delay(5) }
        try { repo.setHidden("b", true); error("expected throw") }
        catch (e: VisibilityRepository.SaveException) {
            assertEquals(VisibilityRepository.SaveError.BUSY, e.error)
        }
        server.enqueue(json(200, snapshot(listOf("a"), rev = 2)))
        withTimeout(5_000) { first.await() }
        Unit
    }
}
