package dev.bookq.data

import dev.bookq.data.offline.OfflineStore
import dev.bookq.data.offline.WorkRecord
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class OfflineStoreTest {

    @get:Rule val tmp = TemporaryFolder()

    private lateinit var filesDir: File
    private lateinit var store: OfflineStore

    @Before fun setup() {
        filesDir = tmp.newFolder("files")
        store = OfflineStore(filesDir, "host|user@x")
    }

    private fun page(folder: String, name: String, bytes: Int = 10): File {
        val f = store.pageFile(folder, name)
        f.parentFile?.mkdirs()
        f.writeBytes(ByteArray(bytes))
        return f
    }

    @Test fun `put get update remove round-trips`() {
        store.put(WorkRecord("a/b", pageCount = 3, cachedCount = 1,
            names = listOf("1.jpg"), addedAt = 5, lastReadAt = 7))
        assertEquals(3, store.get("a/b")?.pageCount)
        store.update("a/b") { it.copy(cachedCount = 2) }
        assertEquals(2, store.get("a/b")?.cachedCount)
        store.remove("a/b")
        assertNull(store.get("a/b"))
    }

    @Test fun `records survive a store reload`() {
        store.put(WorkRecord("w", pageCount = 2, names = listOf("a", "b"), addedAt = 1))
        val store2 = OfflineStore(filesDir, "host|user@x")
        assertEquals(2, store2.get("w")?.pageCount)
        assertEquals(listOf("a", "b"), store2.get("w")?.names)
    }

    @Test fun `scopes never share records`() {
        store.put(WorkRecord("w", pageCount = 1))
        val other = OfflineStore(filesDir, "other-host|")
        assertNull(other.get("w"))
        assertFalse(store.pageFile("w", "1").path.contains(otherScope(other)))
    }

    private fun otherScope(o: OfflineStore): String =
        o.pageFile("w", "1").parentFile?.parentFile?.name ?: ""

    @Test fun `cachedNames intersects record names with existing files`() {
        page("w", "1.jpg"); page("w", "2.jpg")
        store.put(WorkRecord("w", pageCount = 3, names = listOf("1.jpg", "2.jpg", "3.jpg")))
        assertEquals(listOf("1.jpg", "2.jpg"), store.cachedNames("w"))
    }

    @Test fun `reconcile recounts from disk`() {
        page("w", "1.jpg", bytes = 100)
        store.put(WorkRecord("w", pageCount = 3, cachedCount = 3, bytes = 9999,
            names = listOf("1.jpg", "2.jpg", "3.jpg")))
        store.reconcile("w")
        assertEquals(1, store.get("w")?.cachedCount)
        assertEquals(100L, store.get("w")?.bytes)
    }

    @Test fun `deleteWork removes files and record`() {
        val f = page("w", "1.jpg")
        store.put(WorkRecord("w", pageCount = 1, cachedCount = 1, names = listOf("1.jpg")))
        store.deleteWork("w")
        assertFalse(f.exists())
        assertNull(store.get("w"))
    }

    @Test fun `stats dedupes parent records with leaf records`() {
        store.put(WorkRecord("P", pageCount = 1, bytes = 10))
        store.put(WorkRecord("P/v1", pageCount = 4, bytes = 500))
        store.put(WorkRecord("Q", pageCount = 2, bytes = 50))
        val (works, bytes) = store.stats()
        assertEquals(2, works)      // P deduped under P/v1
        assertEquals(550L, bytes)   // P's own bytes skipped (web dedupedBytes)
    }

    @Test fun `oldest follows lastReadAt then addedAt`() {
        store.put(WorkRecord("old", addedAt = 5, lastReadAt = 10))
        store.put(WorkRecord("new", addedAt = 1, lastReadAt = 100))
        store.put(WorkRecord("mid", addedAt = 50, lastReadAt = 0))
        assertEquals("old", store.oldest()?.folder)   // lastReadAt 10
        store.update("old") { it.copy(lastReadAt = 200) }
        assertEquals("mid", store.oldest()?.folder)   // falls back addedAt 50
    }

    @Test fun `clear wipes records and files`() {
        page("w", "1.jpg")
        store.put(WorkRecord("w", pageCount = 1))
        store.clear()
        assertNull(store.get("w"))
        assertTrue(store.records.value.isEmpty())
    }

    @Test fun `records flow emits on put`() {
        val first = store.records.value
        assertTrue(first.isEmpty())
        store.put(WorkRecord("w"))
        assertTrue(store.records.value.containsKey("w"))
    }

    @Test fun `nested archive names land in subdirs`() {
        val f = page("epub", "OEBPS/img/001.jpg")
        assertTrue(f.isFile)
        assertTrue(f.path.contains("OEBPS"))
    }
}
