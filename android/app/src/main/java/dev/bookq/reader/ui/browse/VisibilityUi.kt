package dev.bookq.reader.ui.browse

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Visibility
import androidx.compose.material.icons.filled.VisibilityOff
import androidx.compose.material3.FilledIconButton
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.testTag
import androidx.compose.ui.unit.dp
import dev.bookq.core.visibility.VisibilityState
import dev.bookq.reader.R

/**
 * Show-all mode tile overlay — `visibility.js` `presentation` parity:
 * badge text (非表示 / 上位が非表示 / 配下に非表示) + eye toggle on
 * editable top-level roots. Only rendered while showAll is on — web
 * hides every marker in normal mode.
 */
@Composable
fun VisibilityOverlay(
    pres: VisibilityState.Presentation,
    path: String,
    busy: Boolean,
    onToggle: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(horizontalAlignment = Alignment.End, modifier = modifier) {
        pres.badge?.let { kind ->
            Surface(
                color = when (kind) {
                    VisibilityState.BadgeKind.SELF ->
                        MaterialTheme.colorScheme.errorContainer
                    else -> MaterialTheme.colorScheme.secondaryContainer
                },
                shape = RoundedCornerShape(4.dp),
                // Own merge boundary — the parent card merges descendants,
                // which would fold a plain Text's testTag into the tile
                // node and lose it from the merged tree entirely.
                modifier = Modifier.semantics(mergeDescendants = true) {
                    testTag = "visBadge:$path"
                },
            ) {
                Text(
                    when (kind) {
                        VisibilityState.BadgeKind.SELF ->
                            stringResource(R.string.vis_badge_hidden)
                        VisibilityState.BadgeKind.DESCENDANT ->
                            stringResource(R.string.vis_badge_under)
                        VisibilityState.BadgeKind.ANCESTOR ->
                            stringResource(R.string.vis_badge_above)
                    },
                    style = MaterialTheme.typography.labelSmall,
                    modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp),
                )
            }
        }
        if (pres.toggle) {
            FilledIconButton(
                onClick = onToggle,
                enabled = !busy,
                modifier = Modifier
                    .padding(top = 4.dp)
                    .size(32.dp)
                    .testTag("visToggle:$path"),
            ) {
                Icon(
                    if (pres.hidden) Icons.Default.Visibility
                    else Icons.Default.VisibilityOff,
                    contentDescription = stringResource(
                        if (pres.hidden) R.string.vis_show else R.string.vis_hide),
                    modifier = Modifier.size(16.dp),
                )
            }
        }
    }
}
