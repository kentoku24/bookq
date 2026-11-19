package dev.bookq.core

import dev.bookq.core.api.BookqJson
import dev.bookq.core.api.ImagePath
import dev.bookq.core.api.TreeNode
import dev.bookq.core.browse.BrowseRules
import dev.bookq.core.reader.PageRef
import dev.bookq.core.reader.ReaderChapter
import dev.bookq.core.reader.ReaderPageMode
import dev.bookq.core.reader.ReaderWork
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Conformance vectors (#280) — Kotlin side (#460).
 *
 * `api/vectors` JSON pins the domain rules shared by Python, JS, Swift
 * and Kotlin. Each file declares its `languages`; that the declared readers
 * actually reference it is pinned by tests/test_vectors_declaration.py
 * (pure Python). The vector trees decode straight into the production
 * `TreeNode` model — no test-only shapes.
 *
 * Vectors are read from the repo (`bookq.apiDir` system property set in
 * `core/build.gradle.kts` to `<repo>/api`, i.e. `android/../api`), so the
 * `:core:test` / `:core:testDebugUnitTest` task in CI (`android.yml` unit
 * job) executes these against the same files as iOS
 * `ConformanceVectorsTests.swift`. Same cases, same expectations — a drift
 * between Kotlin and the vectors fails here (vectors are never edited to
 * fit the implementation).
 */
class ConformanceVectorsTest {

    private val json = Json { ignoreUnknownKeys = true }

    private fun vectorsDir(): File {
        val dir = System.getProperty("bookq.apiDir")
            ?: error("bookq.apiDir system property not set")
        val v = File(dir, "vectors")
        require(v.isDirectory) { "vectors dir not found at ${v.absolutePath}" }
        return v
    }

    private fun load(name: String) =
        json.parseToJsonElement(File(vectorsDir(), name).readText()).jsonObject

    private fun rootsOf(case: kotlinx.serialization.json.JsonObject): List<TreeNode> =
        BookqJson.decodeFromJsonElement(
            kotlinx.serialization.builtins.ListSerializer(TreeNode.serializer()),
            case["tree"]!!,
        )

    // MARK: - work-chapters

    /** `ReaderWork.chapters` against work-chapters.json. */
    @Test fun workChapters() {
        val vec = load("work-chapters.json")
        assertTrue(vec["languages"]!!.jsonArray.map { it.jsonPrimitive.content }.contains("kotlin"))
        for (raw in vec["cases"]!!.jsonArray) {
            val c = raw.jsonObject
            val name = c["name"]!!.jsonPrimitive.content
            val roots = rootsOf(c)
            val folder = c["folder"]!!.jsonPrimitive.content
            val (list, cross) = ReaderWork.chapters(folder, roots)
            assertEquals(name, c["expectCross"]!!.jsonPrimitive.boolean, cross)
            assertEquals(
                name,
                c["expectChapterFolders"]!!.jsonArray.map { it.jsonPrimitive.content },
                list.map { it.fullPath },
            )
            val expect = c["expectChapters"]!!.jsonArray
            assertEquals(name, expect.size, list.size)
            for (i in expect.indices) {
                val e = expect[i].jsonObject
                assertEquals(name, e["name"]!!.jsonPrimitive.content, list[i].name)
                assertEquals(name, e["folder"]!!.jsonPrimitive.content, list[i].fullPath)
                assertEquals(
                    name,
                    e["pages"]!!.jsonArray.map { it.jsonPrimitive.content },
                    list[i].images.map { it.name },
                )
                assertTrue(name, list[i].images.all { it.folder == list[i].fullPath })
            }
        }
    }

    // MARK: - path-lookup

    /** `BrowseRules.nodeAt` + structural parent against path-lookup.json. */
    @Test fun pathLookup() {
        val vec = load("path-lookup.json")
        assertTrue(vec["languages"]!!.jsonArray.map { it.jsonPrimitive.content }.contains("kotlin"))
        for (raw in vec["cases"]!!.jsonArray) {
            val c = raw.jsonObject
            val name = c["name"]!!.jsonPrimitive.content
            val roots = rootsOf(c)
            val target = c["target"]!!.jsonPrimitive.content
            val node = if (target.isEmpty()) null else BrowseRules.nodeAt(target, roots)
            val expectNode = c["expectNode"]!!
            if (expectNode !is JsonNull) {
                val e = expectNode.jsonObject
                assertNotNull(name, node)
                assertEquals(name, e["name"]!!.jsonPrimitive.content, node!!.name)
                assertEquals(name, e["type"]!!.jsonPrimitive.content, node.type)
            } else {
                assertNull(name, node)
            }
            val expectParent = c["expectParent"]!!
            if (expectParent !is JsonNull) {
                val parent = expectParent.jsonPrimitive.content
                // The parent resolves and lists the target among its folders.
                assertNotNull(name, BrowseRules.nodeAt(parent, roots))
                assertTrue(
                    name,
                    BrowseRules.foldersAt(parent, roots).map { it.first }.contains(target),
                )
            } else if (node != null) {
                // Root-level match: the target is a root's own name.
                assertTrue(name, roots.any { it.name == node.name })
            }
        }
    }

    // MARK: - encode-path

    /** `ImagePath.encodePath` (+ `/image/` join) against encode-path.json. */
    @Test fun encodePath() {
        val vec = load("encode-path.json")
        assertTrue(vec["languages"]!!.jsonArray.map { it.jsonPrimitive.content }.contains("kotlin"))
        for (raw in vec["cases"]!!.jsonArray) {
            val c = raw.jsonObject
            val name = c["name"]!!.jsonPrimitive.content
            val path = c["path"]!!.jsonPrimitive.content
            assertEquals(name, c["expect"]!!.jsonPrimitive.content, ImagePath.encodePath(path))
            val folderEl = c["folder"]
            if (folderEl != null && folderEl !is JsonNull) {
                val folder = folderEl.jsonPrimitive.content
                val file = c["file"]!!.jsonPrimitive.content
                val inner = if (folder.isEmpty()) file else "$folder/$file"
                assertEquals(name, c["expect"]!!.jsonPrimitive.content, ImagePath.encodePath(inner))
                assertEquals(
                    name,
                    c["expectImageUrl"]!!.jsonPrimitive.content,
                    "/image/" + ImagePath.encodePath(inner),
                )
            }
        }
    }

    // MARK: - solo-spreads

    /** `ReaderWork.rebuildSpreads` against solo-spreads.json. */
    @Test fun soloSpreads() {
        val vec = load("solo-spreads.json")
        assertTrue(vec["languages"]!!.jsonArray.map { it.jsonPrimitive.content }.contains("kotlin"))
        for (raw in vec["cases"]!!.jsonArray) {
            val c = raw.jsonObject
            val name = c["name"]!!.jsonPrimitive.content
            val images = c["images"]!!.jsonArray.map {
                val o = it.jsonObject
                PageRef(
                    o["name"]!!.jsonPrimitive.content,
                    o["folder"]!!.jsonPrimitive.content,
                    o["isSpacer"]?.jsonPrimitive?.booleanOrNull ?: false,
                )
            }
            val boundaries = c["chapterBoundaries"]!!.jsonArray.map { it.jsonPrimitive.int }
            val mode = if (c["pageMode"]!!.jsonPrimitive.content == "spread") {
                ReaderPageMode.SPREAD
            } else {
                ReaderPageMode.SINGLE
            }
            val offset = c["spreadOffset"]!!.jsonPrimitive.int
            val work = ReaderWork(
                clickedPath = "",
                images = images,
                chapters = emptyList(),
                boundaries = boundaries,
                globalToReal = emptyList(),
                realCount = 0,
                spreads = emptyList(),
                idxToSpread = emptyList(),
                chapterStartSpreads = emptySet(),
                pageMode = mode,
            )
            val built = work.rebuildSpreads(offset)
            val expectSpreads = c["expectSpreads"]!!.jsonArray.map { row ->
                row.jsonArray.map { it.jsonPrimitive.int }
            }
            assertEquals(name, expectSpreads, built.spreads)
            assertEquals(
                name,
                c["expectIdxToSpread"]!!.jsonArray.map { it.jsonPrimitive.int },
                built.idxToSpread,
            )
            val expectStarts = c["expectChapterStart"]!!.jsonArray.map { it.jsonPrimitive.boolean }
            assertEquals(name, expectSpreads.size, expectStarts.size)
            for (s in expectSpreads.indices) {
                assertEquals(name, expectStarts[s], built.chapterStartSpread(s))
            }
        }
    }

    // MARK: - chapter-padding

    /** `ReaderWork.assemble` (spacer padding + rebuildSpreads) against
     *  chapter-padding.json — pins the post-shift pad rule shared with
     *  web `padChapterSpacers` and iOS `ReaderWork.assemble` (#441). */
    @Test fun chapterPadding() {
        val vec = load("chapter-padding.json")
        assertTrue(vec["languages"]!!.jsonArray.map { it.jsonPrimitive.content }.contains("kotlin"))
        for (raw in vec["cases"]!!.jsonArray) {
            val c = raw.jsonObject
            val name = c["name"]!!.jsonPrimitive.content
            val chapters = c["chapters"]!!.jsonArray.map { ch ->
                val cho = ch.jsonObject
                val folder = cho["folder"]!!.jsonPrimitive.content
                ReaderChapter(
                    name = folder,
                    fullPath = folder,
                    images = cho["pages"]!!.jsonArray.map {
                        PageRef(it.jsonPrimitive.content, folder)
                    },
                )
            }
            val mode = if (c["pageMode"]!!.jsonPrimitive.content == "spread") {
                ReaderPageMode.SPREAD
            } else {
                ReaderPageMode.SINGLE
            }
            val offset = c["spreadOffset"]!!.jsonPrimitive.int
            val work = ReaderWork.assemble(
                clickedPath = chapters.first().fullPath,
                chapters = chapters,
                pageMode = mode,
                spreadOffset = offset,
            )
            val expectImages = c["expectImages"]!!.jsonArray.map { im ->
                val io = im.jsonObject
                listOf(
                    io["folder"]!!.jsonPrimitive.content,
                    io["name"]!!.jsonPrimitive.content,
                    io["isSpacer"]?.jsonPrimitive?.booleanOrNull ?: false,
                )
            }
            assertEquals(name, expectImages, work.images.map {
                listOf(it.folder, it.name, it.isSpacer)
            })
            assertEquals(
                name,
                c["expectBoundaries"]!!.jsonArray.map { it.jsonPrimitive.int },
                work.boundaries,
            )
            val expectSpreads = c["expectSpreads"]!!.jsonArray.map { row ->
                row.jsonArray.map { it.jsonPrimitive.int }
            }
            assertEquals(name, expectSpreads, work.spreads)
            assertEquals(
                name,
                c["expectIdxToSpread"]!!.jsonArray.map { it.jsonPrimitive.int },
                work.idxToSpread,
            )
            val expectStarts = c["expectChapterStart"]!!.jsonArray.map { it.jsonPrimitive.boolean }
            assertEquals(name, expectSpreads.size, expectStarts.size)
            for (s in expectSpreads.indices) {
                assertEquals(name, expectStarts[s], work.chapterStartSpread(s))
            }
        }
    }
}
