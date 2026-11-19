package dev.bookq.reader.ext

import android.content.Context
import android.hardware.display.DisplayManager
import android.view.Display
import dev.bookq.core.reader.ExtDisplay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * `DISPLAY_CATEGORY_PRESENTATION` watch — the app-level half of the
 * external-display feature (issue #301). Keeps the attach set as a
 * StateFlow of pure [ExtDisplay.DisplayRef] snapshots and owns the
 * user tv-toggle; the projection decision itself lives in
 * [ExtDisplay.target] so it stays unit-testable without a Display.
 *
 * App-scoped (not ServerScope): display attach has nothing to do with
 * which server is mounted — matching iOS `ExternalDisplayStore.shared`.
 */
class ExternalDisplayController(context: Context) {

    private val dm = context.getSystemService(DisplayManager::class.java)

    /** Attached presentation-capable displays — drives the tv button. */
    private val _displays = MutableStateFlow<List<ExtDisplay.DisplayRef>>(emptyList())
    val displays = _displays.asStateFlow()

    /** tv toggle — ON projects when a display is attached, OFF returns
     *  the panel to mirroring. Defaults ON like iOS `userEnabled`. */
    private val _userEnabled = MutableStateFlow(true)
    val userEnabled = _userEnabled.asStateFlow()

    fun setUserEnabled(v: Boolean) { _userEnabled.value = v }

    private val listener = object : DisplayManager.DisplayListener {
        override fun onDisplayAdded(displayId: Int) = refresh()
        override fun onDisplayRemoved(displayId: Int) = refresh()
        override fun onDisplayChanged(displayId: Int) = refresh()
    }

    fun start() {
        refresh()
        dm?.registerDisplayListener(listener, null)
    }

    fun stop() {
        dm?.unregisterDisplayListener(listener)
    }

    private fun refresh() {
        val list = dm?.getDisplays(DisplayManager.DISPLAY_CATEGORY_PRESENTATION)
            ?: emptyArray()
        _displays.value = list.map { d ->
            ExtDisplay.DisplayRef(
                id = d.displayId,
                modes = d.supportedModes.map {
                    ExtDisplay.ModeRef(it.physicalWidth, it.physicalHeight,
                        it.refreshRate)
                },
            )
        }
    }

    /** Live `Display` for a ref — the Presentation needs the object. */
    fun displayFor(id: Int): Display? =
        dm?.getDisplays(DisplayManager.DISPLAY_CATEGORY_PRESENTATION)
            ?.firstOrNull { it.displayId == id }
}
