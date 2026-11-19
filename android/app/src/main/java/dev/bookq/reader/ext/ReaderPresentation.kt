package dev.bookq.reader.ext

import android.app.Activity
import android.app.Presentation
import android.graphics.drawable.ColorDrawable
import android.os.Bundle
import android.view.Display
import android.view.WindowManager
import androidx.compose.runtime.Composable
import androidx.compose.ui.platform.ComposeView
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.ViewModelStoreOwner
import androidx.lifecycle.setViewTreeLifecycleOwner
import androidx.lifecycle.setViewTreeViewModelStoreOwner
import androidx.savedstate.SavedStateRegistryOwner
import androidx.savedstate.setViewTreeSavedStateRegistryOwner
import dev.bookq.core.reader.ExtDisplay

/**
 * The external-surface window (issue #301) — a `Presentation` carrying
 * a ComposeView that renders the reader snapshot with the device
 * reader's own rules, minus chrome. Fullscreen, black background, and
 * the highest-resolution mode the panel offers.
 *
 * A Presentation window is not inside the activity's view hierarchy,
 * so the activity's view-tree owners are grafted onto the decor view
 * — without them ComposeView crashes looking for a LifecycleOwner.
 */
class ReaderPresentation(
    private val activity: Activity,
    display: Display,
    private val content: @Composable () -> Unit,
) : Presentation(activity, display) {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window?.apply {
            setFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN,
                WindowManager.LayoutParams.FLAG_FULLSCREEN)
            setBackgroundDrawable(ColorDrawable(android.graphics.Color.BLACK))
            // 最大解像度モードを選ぶ — the panel's own best mode, not
            // whatever the system happened to pick (iOS `bestMode`).
            val modes = display.supportedModes
            ExtDisplay.bestModeIndex(modes.map {
                ExtDisplay.ModeRef(it.physicalWidth, it.physicalHeight,
                    it.refreshRate)
            })?.let { i ->
                val lp = attributes
                lp.preferredDisplayModeId = modes[i].modeId
                attributes = lp
            }
            (activity as? LifecycleOwner)?.let {
                decorView.setViewTreeLifecycleOwner(it)
            }
            (activity as? ViewModelStoreOwner)?.let {
                decorView.setViewTreeViewModelStoreOwner(it)
            }
            (activity as? SavedStateRegistryOwner)?.let {
                decorView.setViewTreeSavedStateRegistryOwner(it)
            }
        }
        setContentView(ComposeView(context).apply {
            setContent { content() }
        })
    }
}
