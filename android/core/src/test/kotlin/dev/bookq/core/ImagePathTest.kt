package dev.bookq.core

import dev.bookq.core.api.ImagePath
import org.junit.Assert.assertEquals
import org.junit.Test

/** JS encodeURIComponent parity — the same cases the web client produces. */
class ImagePathTest {
    @Test fun segmentBasic() {
        assertEquals("abc", ImagePath.encodeSegment("abc"))
        assertEquals("a%20b", ImagePath.encodeSegment("a b"))
        assertEquals("a%2Bb", ImagePath.encodeSegment("a+b"))
        assertEquals("a%2Fb", ImagePath.encodeSegment("a/b"))
        assertEquals("a%23b", ImagePath.encodeSegment("a#b"))
        assertEquals("a%3Fb", ImagePath.encodeSegment("a?b"))
        assertEquals("a%25b", ImagePath.encodeSegment("a%b"))
    }

    @Test fun unreservedPunctuationPassThrough() {
        assertEquals("-_.!~*'()", ImagePath.encodeSegment("-_.!~*'()"))
    }

    @Test fun japanese() {
        assertEquals("%E7%AC%AC1%E8%A9%B1", ImagePath.encodeSegment("第1話"))
    }

    @Test fun emoji() {
        // U+1F600 → UTF-8 F0 9F 98 80
        assertEquals("%F0%9F%98%80", ImagePath.encodeSegment("\uD83D\uDE00"))
        assertEquals("a%F0%9F%98%80b", ImagePath.encodeSegment("a\uD83D\uDE00b"))
    }

    @Test fun pathKeepsSeparators() {
        assertEquals("a%20b/c", ImagePath.encodePath("a b/c"))
        assertEquals(
            "%E4%BD%9C%E5%93%81%E5%90%8D/%E7%AC%AC1%E5%B7%BB",
            ImagePath.encodePath("作品名/第1巻"),
        )
        // flattened names ("parent/child") become two segments — same as JS
        assertEquals("%E8%A6%AA/%E5%AD%90", ImagePath.encodePath("親/子"))
        assertEquals("parent/child", ImagePath.encodePath("parent/child"))
        // empty segments preserved (leading/trailing slash)
        assertEquals("/a/", ImagePath.encodePath("/a/"))
        assertEquals("", ImagePath.encodePath(""))
    }
}
