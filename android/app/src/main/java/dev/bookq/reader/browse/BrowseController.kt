package dev.bookq.reader.browse

import dev.bookq.core.browse.BrowseRules
import dev.bookq.core.browse.ScrollDecider
import dev.bookq.core.visibility.VisibilityState
import dev.bookq.data.TreeRepository
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * Browse navigation glue — the pieces of `render.js`/app.js tap handling
 * that outlive a single destination:
 *
 *  - Shallow-window queue: taps while `treeLoaded == false` or while the
 *    visibility guard is PENDING cannot be evaluated yet (children are
 *    unknown / hidden state unresolved). JS queues these navigations and
 *    drains them when the full tree lands; [drain] does the same.
 *  - [decider]: the scroll-position decision table, shared across browse
 *    destinations so a viewer→browse return anchor survives the pop.
 */
class BrowseController {

    val decider = ScrollDecider()

    /** What a tap wants to do once the tree is usable. `open` null =
     *  undecided — the node was unknown at tap time, so [drain] decides
     *  open-vs-navigate on the full tree instead of freezing a guess. */
    data class PendingAction(val path: String, val open: Boolean?)

    /** Outcome of evaluating a tap. */
    sealed interface Action {
        data class Navigate(val path: String) : Action
        data class Open(val path: String, val page: Int) : Action
        /** Hidden-root deny — the UI shows a transient note. */
        data object Denied : Action
        /** Deferred — [drain] resolves it on full-tree/visibility arrival. */
        data object Queued : Action
    }

    private val _pending = MutableStateFlow<PendingAction?>(null)
    val pending = _pending.asStateFlow()

    /**
     * Evaluate a folder-tile tap. `drillsDown` null = the node was not
     * resolvable at tap time (search hit outside the shallow window or a
     * flattened-chain mid-path) — the open/navigate decision defers to
     * [drain] rather than guessing open on a folder that may drill down.
     */
    fun tap(
        path: String,
        drillsDown: Boolean?,
        tree: TreeRepository.State,
        vis: VisibilityState,
    ): Action {
        val guard = vis.guardPath(path)
        if (guard == VisibilityState.Guard.HIDDEN) return Action.Denied
        if (!tree.treeLoaded || guard == VisibilityState.Guard.PENDING ||
            drillsDown == null) {
            _pending.value = PendingAction(
                path, open = drillsDown?.let { !it })
            return Action.Queued
        }
        return if (!drillsDown) Action.Open(path, 0) else Action.Navigate(path)
    }

    /** Queue a reader-open directly (read-self/read-child/root tile). */
    fun tapSelf(path: String, tree: TreeRepository.State, vis: VisibilityState): Action {
        val guard = vis.guardPath(path)
        if (!tree.treeLoaded || guard == VisibilityState.Guard.PENDING) {
            _pending.value = PendingAction(path, open = true)
            return Action.Queued
        }
        if (guard == VisibilityState.Guard.HIDDEN) return Action.Denied
        return Action.Open(path, 0)
    }

    /**
     * Drain the queued action once `treeLoaded && hasSnapshot`. The target
     * may have changed under us — re-evaluate: missing node → drop,
     * only-images → open, else navigate. Returns the resolved action.
     */
    fun drain(
        roots: List<dev.bookq.core.api.TreeNode>,
        tree: TreeRepository.State,
        vis: VisibilityState,
    ): Action? {
        val p = _pending.value ?: return null
        if (!tree.treeLoaded || vis.guardPath(p.path) == VisibilityState.Guard.PENDING) return null
        _pending.value = null
        if (vis.guardPath(p.path) == VisibilityState.Guard.HIDDEN) return Action.Denied
        val node = if (p.path.isEmpty()) null else BrowseRules.nodeAt(p.path, roots)
        return when {
            p.open == true -> Action.Open(p.path, 0)
            node == null -> null // vanished between shallow and full
            BrowseRules.hasOnlyImages(node) -> Action.Open(p.path, 0)
            else -> Action.Navigate(p.path)
        }
    }

    fun clearPending() {
        _pending.value = null
    }
}
