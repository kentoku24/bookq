package dev.bookq.core

import dev.bookq.core.reader.PageRef
import dev.bookq.core.reader.ReaderChapter
import dev.bookq.core.reader.ReaderPageMode
import dev.bookq.core.reader.ReaderWork
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * `api/vectors/chapter-padding.json` — cross-chapter spacer padding +
 * spread grouping shared with app.js `padChapterSpacers`/`rebuildSpreads`
 * and iOS `ReaderWork.assemble` (#441). The vector file is read straight
 * from the repo via the `bookq.apiDir` system property
 * (core/build.gradle.kts), same as SpecConformanceTest's spec access.
 */
class ChapterPaddingVectorsTest {

    private fun loadVector(name: String) = run {
        val dir = System.getProperty("bookq.apiDir")
            ?: error("bookq.apiDir system property not set")
        val file = File(dir, "vectors/$name")
        require(file.isFile) { "vector not found at ${file.absolutePath}" }
        Json.parseToJsonElement(file.readText()).jsonObject
    }

    private fun str(e: JsonElement) = e.jsonPrimitive.content
    private fun jbool(e: JsonElement?) = e?.jsonPrimitive?.content == "true"
    private fun ints(e: JsonElement) = e.jsonArray.map { it.jsonPrimitive.int }

    @Test fun chapterPadding() {
        val vec = loadVector("chapter-padding.json")
        val cases = vec.getValue("cases").jsonArray
        assertTrue("chapter-padding.json must carry cases", cases.isNotEmpty())
        for (raw in cases) {
            val c = raw.jsonObject
            val name = str(c.getValue("name"))
            val chapters = c.getValue("chapters").jsonArray.map { ch ->
                val cho = ch.jsonObject
                val folder = str(cho.getValue("folder"))
                ReaderChapter(
                    name = folder,
                    fullPath = folder,
                    images = cho.getValue("pages").jsonArray.map {
                        PageRef(str(it), folder)
                    },
                )
            }
            val mode = when (str(c.getValue("pageMode"))) {
                "spread" -> ReaderPageMode.SPREAD
                "single" -> ReaderPageMode.SINGLE
                else -> error("bad pageMode in vector case $name")
            }
            val offset = c.getValue("spreadOffset").jsonPrimitive.int
            val work = ReaderWork.assemble(
                chapters.first().fullPath, chapters, mode, offset)

            val gotImages = work.images.map {
                listOf(it.folder, it.name, if (it.isSpacer) "1" else "0")
            }
            val expectImages = c.getValue("expectImages").jsonArray.map { im ->
                val io = im.jsonObject
                listOf(
                    str(io.getValue("folder")),
                    str(io.getValue("name")),
                    if (jbool(io["isSpacer"])) "1" else "0",
                )
            }
            assertEquals("$name images", expectImages, gotImages)
            assertEquals("$name boundaries",
                ints(c.getValue("expectBoundaries")), work.boundaries)
            val expectSpreads = c.getValue("expectSpreads").jsonArray.map { row ->
                row.jsonArray.map { it.jsonPrimitive.int }
            }
            assertEquals("$name spreads", expectSpreads, work.spreads)
            assertEquals("$name idxToSpread",
                ints(c.getValue("expectIdxToSpread")), work.idxToSpread)
            val expectStarts = c.getValue("expectChapterStart").jsonArray.map { jbool(it) }
            val gotStarts = (0 until work.spreads.size)
                .map { it in work.chapterStartSpreads }
            assertEquals("$name chapterStart", expectStarts, gotStarts)
        }
    }
}
