package dev.bookq.reader.reader

import dev.bookq.core.api.TreeNode
import dev.bookq.core.reader.PageRef
import dev.bookq.core.reader.ReaderChapter
import dev.bookq.core.reader.ReaderWork
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * Reader session state — the Android counterpart of app.js `loadImages`
 * state plus the viewer's session-only layout settings (decision Q17:
 * レイアウト設定は本をまたいで引き継がない — they reset on every open,
 * never persist).
 */
class ReaderController {

    sealed interface LoadState {
        data object Loading : LoadState
        data class Ready(val work: ReaderWork, val startIdx: Int) : LoadState
        data object Empty : LoadState
        data class Error(val message: String) : LoadState
    }

    private val _load = MutableStateFlow<LoadState>(LoadState.Loading)
    val load = _load.asStateFlow()

    // ── session-only layout settings (reset per open — web Q17) ──
    private val _landscapeSpread = MutableStateFlow(true)
    val landscapeSpread = _landscapeSpread.asStateFlow()
    private val _spreadOffset = MutableStateFlow(0)
    val spreadOffset = _spreadOffset.asStateFlow()
    private val _tapForwardLeft = MutableStateFlow(true)
    val tapForwardLeft = _tapForwardLeft.asStateFlow()

    fun toggleLandscapeSpread() { _landscapeSpread.value = !_landscapeSpread.value }
    fun toggleSpreadOffset() { _spreadOffset.value = 1 - _spreadOffset.value }
    fun toggleTapForward() { _tapForwardLeft.value = !_tapForwardLeft.value }

    // ── aspect-ratio map (web `pageAR`/`lastAR`) ──
    private val _pageAR = MutableStateFlow<Map<Int, Float>>(emptyMap())
    val pageAR = _pageAR.asStateFlow()

    @Volatile var lastAR = 0.7f
        private set

    /** `recordAR` — remember this page's real aspect ratio; later pages
     *  fall back to the most recent measurement. */
    fun recordAR(globalIdx: Int, ar: Float) {
        if (ar <= 0f) return
        lastAR = ar
        val cur = _pageAR.value
        if (cur[globalIdx] == ar) return
        _pageAR.value = cur + (globalIdx to ar)
    }

    fun arOf(globalIdx: Int): Float = _pageAR.value[globalIdx] ?: lastAR

    /**
     * Assemble the work — `loadImages` port. Tree nodes provide leaf/mixed
     * chapter images; the plain fallback calls [imageLister]
     * (`/api/v1/images`) once per chapter whose listing the tree doesn't
     * carry (archives without scanned children, ...). [savedPage] is the
     * chapter-local resume index the route carried.
     */
    suspend fun open(
        path: String,
        savedPage: Int,
        roots: List<TreeNode>,
        imageLister: suspend (String) -> List<String>,
        viewW: Float,
        viewH: Float,
    ) {
        _load.value = LoadState.Loading
        val (chapterList, cross) = ReaderWork.chapters(path, roots)
        val filled = chapterList.map { ch ->
            if (ch.images.isNotEmpty()) ch else try {
                ReaderChapter(ch.name, ch.fullPath,
                    imageLister(ch.fullPath).map { PageRef(it, ch.fullPath) })
            } catch (e: Exception) {
                _load.value = LoadState.Error(
                    e.message ?: "画像を読み込めませんでした")
                return
            }
        }
        if (filled.all { it.images.isEmpty() }) {
            _load.value = LoadState.Empty
            return
        }
        val mode = ReaderWork.pageMode(viewW, viewH, _landscapeSpread.value)
        val work = ReaderWork.assemble(path, filled, mode, _spreadOffset.value)
        // Resume: clicked chapter's local index (web clamps -1 → 0).
        val cc = work.chapters.indexOfFirst { it.fullPath == path }
            .coerceAtLeast(0)
        val start = work.resumeGlobalIndex(cc, savedPage)
        _load.value = LoadState.Ready(work, start)
    }

    /** Rotation/toggle — rebuild spreads, keep the same global anchor. */
    fun remode(work: ReaderWork, viewW: Float, viewH: Float): ReaderWork {
        val mode = ReaderWork.pageMode(viewW, viewH, _landscapeSpread.value)
        return if (mode == work.pageMode && _spreadOffset.value == work.spreadOffset) work
        else work.withMode(mode, _spreadOffset.value)
    }
}
