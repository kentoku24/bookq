package dev.bookq.reader.debug

import android.view.Choreographer
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import java.util.Locale

private data class FpsSample(val fps: Int, val worstMs: Float)

/**
 * Debug-only FPS overlay: Choreographer frame callbacks aggregated over
 * ~500ms windows → fps + worst frame ms. Toggled by the debug-only
 * "FPS を表示" setting.
 */
@Composable
fun BoxScope.FpsHud() {
    var sample by remember { mutableStateOf(FpsSample(0, 0f)) }

    DisposableEffect(Unit) {
        var frames = 0
        var worstNs = 0L
        var windowStart = 0L
        var last = 0L
        val callback = object : Choreographer.FrameCallback {
            override fun doFrame(frameTimeNanos: Long) {
                if (last != 0L) {
                    frames += 1
                    worstNs = maxOf(worstNs, frameTimeNanos - last)
                }
                if (windowStart == 0L) windowStart = frameTimeNanos
                last = frameTimeNanos
                val windowNs = frameTimeNanos - windowStart
                if (windowNs >= 500_000_000L) {
                    sample = FpsSample(
                        fps = (frames * 1_000_000_000L / windowNs).toInt(),
                        worstMs = worstNs / 1_000_000f,
                    )
                    frames = 0
                    worstNs = 0
                    windowStart = frameTimeNanos
                }
                Choreographer.getInstance().postFrameCallback(this)
            }
        }
        Choreographer.getInstance().postFrameCallback(callback)
        onDispose { Choreographer.getInstance().removeFrameCallback(callback) }
    }

    Surface(
        color = MaterialTheme.colorScheme.surface.copy(alpha = 0.85f),
        shape = MaterialTheme.shapes.small,
        modifier = Modifier
            .align(Alignment.TopEnd)
            .padding(8.dp),
    ) {
        Text(
            text = String.format(
                Locale.US, "%d fps · worst %.1f ms", sample.fps, sample.worstMs),
            style = MaterialTheme.typography.labelSmall,
            modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp),
        )
    }
}
