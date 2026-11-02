package dev.bookq.core

import dev.bookq.core.visibility.VisibilityState
import dev.bookq.core.visibility.VisibilityState.BlockedGrid
import dev.bookq.core.visibility.VisibilityState.Guard
import dev.bookq.core.visibility.VisibilityState.MatchKind
import dev.bookq.core.visibility.VisibilityState.Status
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Port of `browse/visibility.js` read-path semantics. */
class VisibilityRulesTest {

    private fun state() = VisibilityState()

    private fun ready(hidden: List<String>, owner: String = "u@x", rev: Int = 1) =
        state().apply {
            applySnapshot(owner, rev, hidden)
            setStatus(Status.READY)
        }

    // ── normalizePath ──

    @Test fun normalizePath() {
        val s = state()
        assertEquals("a/b", s.normalizePath("a/b"))
        assertEquals("a/b", s.normalizePath("a//b"))
        assertEquals("a/b", s.normalizePath("a/./b"))
        assertEquals("b", s.normalizePath("a/../b"))
        assertNull(s.normalizePath("../escape"))
        assertNull(s.normalizePath(""))
        assertNull(s.normalizePath(null))
    }

    // ── hiddenMatch kinds & longest-wins ──

    @Test fun matchSelfAndDescendant() {
        val s = ready(listOf("secret"))
        assertEquals(MatchKind.SELF, s.hiddenMatch("secret")?.kind)
        assertEquals(MatchKind.DESCENDANT, s.hiddenMatch("secret/ch1")?.kind)
        assertNull(s.hiddenMatch("other"))
        // prefix-only similarity is NOT a match
        assertNull(s.hiddenMatch("secret2"))
    }

    @Test fun matchFlattenedKeyAndAncestor() {
        // Flattened roots hide multi-segment keys — full prefix match.
        val s = ready(listOf("wrap/secret"))
        assertEquals(MatchKind.SELF, s.hiddenMatch("wrap/secret")?.kind)
        assertEquals(MatchKind.DESCENDANT, s.hiddenMatch("wrap/secret/x")?.kind)
        // "wrap" is an ANCESTOR of the hidden key, not hidden itself
        assertEquals(MatchKind.ANCESTOR, s.hiddenMatch("wrap")?.kind)
        // ancestor does not deny entry at the guard
        assertEquals(Guard.ALLOW, s.guardPath("wrap"))
        // …but is invisible in the normal-mode grid (its subtree is all hidden)
        assertFalse(s.isVisible("wrap"))
    }

    @Test fun longestCoveringKeyWins() {
        val s = ready(listOf("a", "a/b"))
        assertEquals("a/b", s.hiddenRootFor("a/b/c"))
        assertEquals("a", s.hiddenRootFor("a/c"))
    }

    @Test fun caseAndUnicodeInsensitiveMatch() {
        val s = ready(listOf("HIDE_ME"))
        assertTrue(s.isHidden("hide_me"))
        assertTrue(s.isHidden("Hide_Me/x"))
    }

    // ── fail-closed & guards ──

    @Test fun noSnapshotFailsClosed() {
        val s = state()
        s.setStatus(Status.LOADING)
        assertFalse(s.isVisible("anything"))
        assertEquals(Guard.PENDING, s.guardPath("anything"))
        assertEquals(BlockedGrid.Loading, s.blockedGrid())
    }

    @Test fun errorWithoutSnapshotBlocksWithRetry() {
        val s = state()
        s.setStatus(Status.ERROR)
        assertEquals(BlockedGrid.Error, s.blockedGrid())
        assertFalse(s.isVisible("x"))
    }

    @Test fun snapshotRendersNormally() {
        val s = ready(listOf("hid"))
        assertNull(s.blockedGrid())
        assertTrue(s.isVisible("ok"))
        assertFalse(s.isVisible("hid"))
        assertEquals(Guard.HIDDEN, s.guardPath("hid/sub"))
        assertEquals(Guard.ALLOW, s.guardPath("ok"))
    }

    @Test fun showAllBypassesEverything() {
        val s = ready(listOf("hid"))
        s.setShowAll(true)
        assertTrue(s.isVisible("hid"))
        assertEquals(Guard.ALLOW, s.guardPath("hid"))
        val noSnap = state()
        noSnap.setShowAll(true)
        assertTrue(noSnap.isVisible("anything"))
        assertEquals(Guard.ALLOW, noSnap.guardPath("anything"))
    }

    // ── applySnapshot revision/owner rules ──

    @Test fun otherOwnerRejectedUnlessForced() {
        val s = ready(listOf("a"), owner = "u1")
        assertFalse(s.applySnapshot("u2", 5, listOf("b")))
        assertEquals("u1", s.owner)
        assertTrue(s.applySnapshot("u2", 5, listOf("b"), force = true))
        assertEquals("u2", s.owner)
        assertEquals(listOf("b"), s.snapshot().hiddenRoots)
    }

    @Test fun downrevRejectedForCacheAllowedForServer() {
        val s = ready(listOf("a"), rev = 5)
        assertFalse(s.applySnapshot("u@x", 3, listOf("b")))
        assertTrue(s.applySnapshot("u@x", 3, listOf("b"), allowDownrev = true))
        assertEquals(3, s.revision)
    }

    @Test fun topLevelSet() {
        val s = ready(emptyList())
        s.setTopLevelPaths(listOf("root1", "root/2"))
        assertTrue(s.isTopLevel("root1"))
        assertTrue(s.isTopLevel("root/2"))
        assertFalse(s.isTopLevel("root1/child"))
    }
}
