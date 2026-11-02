package dev.bookq.core.visibility

import java.text.Normalizer
import java.util.Locale

/**
 * Port of `app/static/browse/visibility.js` (read path) — hidden-root
 * predicates and the snapshot state machine. Write/mutation paths
 * (PUT + owner header + coalesce) arrive in M6; this holds everything the
 * browse render and navigation guards need.
 *
 * Semantics (JS source of truth):
 *  - Hidden keys are the `_fullPath` of top-level nodes — a flattened
 *    scanner root's name is multi-segment, so key matching is a full
 *    prefix match, never a first-segment compare.
 *  - Comparison key = NFC + lowercase; the original names are kept for
 *    display/PUT (`_normKey` / `_hidden` split).
 *  - `isVisible` fails CLOSED: with no snapshot applied the hidden set is
 *    empty AND every path is treated as not-visible — the grid never
 *    paints an unfiltered list in loading/error.
 *  - ancestor matches (a path that is an ancestor of a hidden key) are
 *    hidden in normal mode too — a provisional tile whose entire subtree
 *    is hidden shows nothing.
 *
 * Thread safety: the repository applies snapshots on IO while Compose
 * reads predicates on Main — scalars are @Volatile, collection state is
 * serialized on [lock] (in-memory work only; contention negligible).
 */
class VisibilityState {

    enum class Status { IDLE, LOADING, READY, READONLY, ERROR }
    enum class Guard { ALLOW, PENDING, HIDDEN }
    enum class MatchKind { SELF, DESCENDANT, ANCESTOR }

    data class Snapshot(val owner: String, val revision: Int, val hiddenRoots: List<String>)
    data class Match(val key: String, val kind: MatchKind)

    @Volatile var status: Status = Status.IDLE
        private set
    @Volatile var owner: String = ""
        private set
    @Volatile var revision: Int = 0
        private set
    @Volatile var readOnly: Boolean = false
        private set
    @Volatile var showAll: Boolean = false
        private set
    @Volatile var hasSnapshot: Boolean = false
        private set

    private val lock = Any()

    /** Hidden roots in original (display) form, insertion order preserved. */
    private val hidden = linkedSetOf<String>()

    /** normKey → original key, for match-key lookups (JS `_hiddenNorm`). */
    private val hiddenNorm = linkedMapOf<String, String>()

    private var topLevel: Set<String> = emptySet()

    // ── snapshot adoption ──

    /**
     * Adopt a snapshot; returns whether the visible state actually changed.
     * @param force commit regardless of owner (server says another account).
     * @param allowDownrev server responses only — a stale server revision
     *   still applies (cache writes never pass this).
     */
    fun applySnapshot(
        owner: String,
        revision: Int,
        roots: List<String>,
        force: Boolean = false,
        allowDownrev: Boolean = false,
    ): Boolean = synchronized(lock) {
        if (!force && owner.isNotEmpty() && this.owner.isNotEmpty() && owner != this.owner) {
            return@synchronized false
        }
        if (!allowDownrev && hasSnapshot && revision < this.revision) return@synchronized false
        val sameOwner = this.owner == owner
        val changed = !sameOwner || this.revision != revision ||
            hidden.toList() != roots.distinct()
        this.owner = owner
        this.revision = revision
        hidden.clear()
        hiddenNorm.clear()
        for (r in roots) {
            if (hidden.add(r)) hiddenNorm[normKey(r)] = r
        }
        hasSnapshot = true
        return@synchronized changed
    }

    fun setStatus(s: Status, readOnly: Boolean = this.readOnly) {
        status = s
        this.readOnly = readOnly
    }

    fun setShowAll(v: Boolean) {
        showAll = v
    }

    fun snapshot(): Snapshot = synchronized(lock) {
        Snapshot(owner, revision, hidden.toList())
    }

    // ── path normalization ──

    /**
     * Strip "", "." and ".." segments; ".." past the root yields null —
     * a path escaping the library root is never a valid root key.
     */
    fun normalizePath(path: String?): String? {
        if (path.isNullOrEmpty()) return null
        val out = ArrayDeque<String>()
        for (s in path.split('/')) {
            if (s.isEmpty() || s == ".") continue
            if (s == "..") {
                if (out.isEmpty()) return null
                out.removeLast()
                continue
            }
            out.addLast(s)
        }
        return out.joinToString("/")
    }

    /** Comparison key: NFC + lowercase — matches JS `_normKey` so case-
     *  insensitive filesystems can't evade the hidden set. */
    fun normKey(p: String): String {
        val n = try {
            Normalizer.normalize(p, Normalizer.Form.NFC)
        } catch (e: Exception) {
            p
        }
        return n.lowercase(Locale.ROOT)
    }

    /**
     * Three match kinds: SELF (path === hidden key), DESCENDANT (path under
     * key + "/"), ANCESTOR (path is an ancestor of a hidden key — a
     * provisional tile from a shallow/unflattened name). Covering matches
     * take the LONGEST key; ancestor takes the shortest.
     */
    fun hiddenMatch(path: String): Match? {
        val norm = normalizePath(path)
        if (norm.isNullOrEmpty()) return null
        val nkey = normKey(norm)
        synchronized(lock) {
            var bestCover: String? = null
            var bestAnc: String? = null
            for ((kn, orig) in hiddenNorm) {
                if (nkey == kn || nkey.startsWith("$kn/")) {
                    if (bestCover == null || kn.length > bestCover!!.length) bestCover = kn
                } else if (kn.startsWith("$nkey/")) {
                    if (bestAnc == null || kn.length < bestAnc.length) bestAnc = kn
                }
            }
            if (bestCover != null) {
                return Match(
                    key = hiddenNorm[bestCover] ?: bestCover,
                    kind = if (bestCover == nkey) MatchKind.SELF else MatchKind.DESCENDANT,
                )
            }
            if (bestAnc != null) {
                return Match(key = hiddenNorm[bestAnc] ?: bestAnc, kind = MatchKind.ANCESTOR)
            }
            return null
        }
    }

    /** The hidden key covering path (self/descendant only — ancestor does
     *  not deny entry; drilling in is allowed, contents filter instead). */
    fun hiddenRootFor(path: String): String? {
        val m = hiddenMatch(path)
        return if (m != null && m.kind != MatchKind.ANCESTOR) m.key else null
    }

    fun isHidden(path: String): Boolean = hiddenRootFor(path) != null

    /**
     * Positive-hidden only: no snapshot → hidden set is empty, so
     * error/unknown never masquerades as hidden… BUT visibility fails
     * closed — without a snapshot nothing is visible.
     */
    fun isVisible(path: String): Boolean {
        if (showAll) return true
        if (!hasSnapshot) return false
        return hiddenMatch(path) == null
    }

    // ── top-level node set (editable roots; used by badges in M6) ──

    fun setTopLevelPaths(paths: Collection<String>) {
        synchronized(lock) {
            topLevel = paths.filterTo(LinkedHashSet()) { it.isNotEmpty() }
        }
    }

    fun isTopLevel(path: String): Boolean = synchronized(lock) { path in topLevel }

    /** Shared deny/queue gate for every navigation entry. */
    fun guardPath(path: String?): Guard {
        if (showAll) return Guard.ALLOW
        if (status == Status.LOADING || !hasSnapshot) return Guard.PENDING
        return if (!path.isNullOrEmpty() && hiddenRootFor(path) != null) Guard.HIDDEN else Guard.ALLOW
    }

    // ── showAll presentation (web `presentation`/`uiPresentation`) ──

    enum class BadgeKind { SELF, DESCENDANT, ANCESTOR }

    /**
     * `uiPresentation` — badge + eye-toggle decision for one tile.
     * Only meaningful in showAll mode (callers gate on it): normal mode
     * filters hidden roots outright and marks nothing.
     *
     * @param editable null → auto via [isTopLevel]; ancestor matches are
     *   NEVER editable (they aren't real tree nodes — PUTting them 404s).
     */
    data class Presentation(
        val badge: BadgeKind?,
        /** Covered by the hidden set — the eye offers 再表示. */
        val hidden: Boolean,
        /** Eye toggle allowed (editable root & not an ancestor match). */
        val toggle: Boolean,
    )

    fun presentation(path: String, editable: Boolean? = null): Presentation {
        val m = hiddenMatch(path)
        val badge = when (m?.kind) {
            MatchKind.SELF -> BadgeKind.SELF
            MatchKind.DESCENDANT -> BadgeKind.DESCENDANT
            MatchKind.ANCESTOR -> BadgeKind.ANCESTOR
            null -> null
        }
        val hidden = m?.kind == MatchKind.SELF || m?.kind == MatchKind.DESCENDANT
        var edit = editable ?: isTopLevel(path)
        if (m?.kind == MatchKind.ANCESTOR) edit = false
        return Presentation(badge = badge, hidden = hidden,
            toggle = edit && path.isNotEmpty())
    }

    /**
     * Client-side copy of the backend root rule (`_validRootPath`):
     * non-empty, ≤2048, no NUL/backslash, no empty/"." /".." /
     * dot-leading segments. Multi-segment roots (flattened names) are
     * valid — membership in the top-level set is the server's check.
     */
    fun validRootPath(root: String?): String? {
        if (root.isNullOrEmpty() || root.length > 2048) return null
        if (root.contains('\\') || root.contains('\u0000')) return null
        for (s in root.split('/')) {
            if (s.isEmpty() || s == "." || s == ".." || s.startsWith(".")) return null
        }
        return root
    }

    /** Grid-wide gate while NO snapshot exists — renderers must never
     *  paint an unfiltered list in loading/error. Any snapshot (cached or
     *  live) renders normally. */
    sealed interface BlockedGrid {
        data object Loading : BlockedGrid
        data object Error : BlockedGrid
    }

    fun blockedGrid(): BlockedGrid? {
        if (hasSnapshot) return null
        return when (status) {
            Status.IDLE, Status.LOADING -> BlockedGrid.Loading
            Status.ERROR -> BlockedGrid.Error
            else -> null
        }
    }
}
