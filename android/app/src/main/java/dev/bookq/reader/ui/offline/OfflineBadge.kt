package dev.bookq.reader.ui.offline

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.unit.dp
import dev.bookq.core.offline.OfflineRules
import dev.bookq.reader.R

/**
 * The 40dp circular download badge — Android's `.offline-btn`. Idle ↓ /
 * listing spinner / downloading ring + ■ / partial ring + ↓ / complete ✓ /
 * confirm-delete 🗑 (armed 3s) / deleting spinner. Taps are dispatched by
 * `OfflineManager.onBadgeTap`; PARTIAL opens the continue/delete menu.
 */
@Composable
fun OfflineBadge(
    state: OfflineRules.BtnState,
    pct: Float,
    partial: OfflineRules.Progress,
    tag: String,
    onIdle: () -> Unit,
    onStop: () -> Unit,
    onConfirmDelete: () -> Unit,
    onContinue: () -> Unit,
    onPartialDelete: () -> Unit,
    modifier: Modifier = Modifier,
) {
    var menu by remember { mutableStateOf(false) }
    val armed = state == OfflineRules.BtnState.CONFIRM_DELETE
    val enabled = state == OfflineRules.BtnState.IDLE ||
        state == OfflineRules.BtnState.DOWNLOADING ||
        state == OfflineRules.BtnState.PARTIAL ||
        state == OfflineRules.BtnState.COMPLETE || armed

    Box(modifier) {
        Surface(
            onClick = {
                when (state) {
                    OfflineRules.BtnState.IDLE -> onIdle()
                    OfflineRules.BtnState.DOWNLOADING -> onStop()
                    OfflineRules.BtnState.PARTIAL -> menu = true
                    OfflineRules.BtnState.COMPLETE,
                    OfflineRules.BtnState.CONFIRM_DELETE -> onConfirmDelete()
                    else -> Unit
                }
            },
            enabled = enabled,
            shape = CircleShape,
            color = MaterialTheme.colorScheme.surface.copy(alpha = 0.92f),
            tonalElevation = 3.dp,
            shadowElevation = 2.dp,
            modifier = Modifier
                .size(40.dp)
                .testTag(tag)
                .semantics { stateDescription = "offline:$state" },
        ) {
            Box(contentAlignment = Alignment.Center, modifier = Modifier.size(40.dp)) {
                when (state) {
                    OfflineRules.BtnState.LISTING ->
                        CircularProgressIndicator(
                            modifier = Modifier.size(22.dp), strokeWidth = 2.dp)
                    OfflineRules.BtnState.DOWNLOADING -> {
                        CircularProgressIndicator(
                            progress = { pct },
                            modifier = Modifier.size(28.dp),
                            strokeWidth = 3.dp,
                        )
                        Box(Modifier.size(9.dp).clip(MaterialTheme.shapes.small)
                            .background(MaterialTheme.colorScheme.primary))
                    }
                    OfflineRules.BtnState.STOPPING -> {
                        CircularProgressIndicator(
                            progress = { pct },
                            modifier = Modifier.size(28.dp),
                            strokeWidth = 3.dp,
                        )
                        Box(Modifier.size(9.dp).clip(MaterialTheme.shapes.small)
                            .background(MaterialTheme.colorScheme.outline))
                    }
                    OfflineRules.BtnState.PARTIAL -> {
                        CircularProgressIndicator(
                            progress = { pct },
                            modifier = Modifier.size(28.dp),
                            strokeWidth = 3.dp,
                            trackColor = MaterialTheme.colorScheme.surfaceVariant,
                        )
                        Icon(Icons.Default.KeyboardArrowDown, contentDescription = null,
                            modifier = Modifier.size(16.dp))
                    }
                    OfflineRules.BtnState.COMPLETE ->
                        Icon(Icons.Default.Check, contentDescription = null,
                            tint = MaterialTheme.colorScheme.primary)
                    OfflineRules.BtnState.CONFIRM_DELETE ->
                        Icon(Icons.Default.Delete, contentDescription = null,
                            tint = MaterialTheme.colorScheme.error)
                    OfflineRules.BtnState.DELETING ->
                        CircularProgressIndicator(
                            modifier = Modifier.size(22.dp), strokeWidth = 2.dp)
                    OfflineRules.BtnState.IDLE ->
                        Icon(Icons.Default.KeyboardArrowDown, contentDescription = null)
                }
            }
        }

        DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
            DropdownMenuItem(
                text = { Text(stringResource(R.string.offline_continue, (partial.pageCount - partial.cachedCount).coerceAtLeast(0))) },
                onClick = { menu = false; onContinue() },
            )
            DropdownMenuItem(
                text = { Text(stringResource(R.string.offline_delete_pages, partial.cachedCount)) },
                onClick = { menu = false; onPartialDelete() },
            )
        }
    }
}
