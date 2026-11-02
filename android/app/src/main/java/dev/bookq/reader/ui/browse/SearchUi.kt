package dev.bookq.reader.ui.browse

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import coil3.compose.AsyncImage
import dev.bookq.core.api.ComicMetadata
import dev.bookq.core.api.Facets
import dev.bookq.core.api.TreeNode
import dev.bookq.core.browse.BrowseRules
import dev.bookq.core.browse.LevelModel
import dev.bookq.data.SearchRepository
import dev.bookq.reader.R
import dev.bookq.reader.ServerScope

/**
 * M6 search/facet UI — `search/facets.js` port: global result cards,
 * armed-filter chips, and the filter bottom sheet (artist/tag sections,
 * reset/apply — the single `relevance` sort has no UI since it's fixed).
 */

/** One global search hit — title/artist/folder + resolved cover. */
@Composable
fun SearchResultCard(
    meta: ComicMetadata,
    node: TreeNode?,
    scope: ServerScope,
    coverW: Int,
    onTap: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Card(onClick = onTap, modifier = modifier.testTag("searchResult:${meta.folderPath}")) {
        Column {
            val cover = node?.let { LevelModel.coverOf(it) }
            val model = when (cover) {
                is LevelModel.Cover.Single ->
                    scope.api.thumbUrl(meta.folderPath, cover.name, coverW).toString()
                is LevelModel.Cover.Sprite ->
                    scope.api.thumbSpriteUrl(meta.folderPath, cover.names,
                        cover.spec.rows, coverW).toString()
                else -> null
            }
            if (model != null) {
                AsyncImage(
                    model = model,
                    imageLoader = scope.imageLoader,
                    contentDescription = null,
                    contentScale = ContentScale.Crop,
                    modifier = Modifier
                        .fillMaxWidth()
                        .height(140.dp),
                )
            }
            Column(Modifier.padding(10.dp)) {
                Text(meta.displayTitle, maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                    style = MaterialTheme.typography.bodyMedium)
                if (meta.artist.isNotEmpty()) {
                    Text(meta.artist.joinToString(", "), maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.outline)
                }
                Text(meta.folderPath, maxLines = 1, overflow = TextOverflow.Ellipsis,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.outline)
            }
        }
    }
}

/** Results grid — same column spec as the browse tile grid. */
@Composable
fun SearchResultGrid(
    results: List<ComicMetadata>,
    nodes: List<TreeNode>,
    scope: ServerScope,
    coverW: Int,
    onTap: (ComicMetadata) -> Unit,
    modifier: Modifier = Modifier,
) {
    LazyVerticalGrid(
        columns = GridCells.Adaptive(minSize = 160.dp),
        horizontalArrangement = Arrangement.spacedBy(12.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
        modifier = modifier.padding(horizontal = 16.dp),
    ) {
        items(results, key = { it.folderPath }) { meta ->
            SearchResultCard(
                meta = meta,
                node = BrowseRules.nodeAt(meta.folderPath, nodes),
                scope = scope,
                coverW = coverW,
                onTap = { onTap(meta) },
            )
        }
    }
}

/** Armed filter chips — the `#filter-chips` row; tap removes + refires. */
@Composable
fun FilterChipRow(
    artists: Set<String>,
    tags: Set<String>,
    onRemove: (SearchRepository.Kind, String) -> Unit,
) {
    if (artists.isEmpty() && tags.isEmpty()) return
    LazyRow(
        horizontalArrangement = Arrangement.spacedBy(8.dp),
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 16.dp, vertical = 4.dp),
    ) {
        items(artists.toList()) { a ->
            AssistChip(
                onClick = { onRemove(SearchRepository.Kind.ARTIST, a) },
                label = { Text(a) },
                trailingIcon = {
                    Icon(Icons.Default.Close, null,
                        Modifier.padding(start = 2.dp).width(16.dp))
                },
                modifier = Modifier.testTag("filterChip:artist:$a"),
            )
        }
        items(tags.toList()) { t ->
            AssistChip(
                onClick = { onRemove(SearchRepository.Kind.TAG, t) },
                label = { Text(t) },
                trailingIcon = {
                    Icon(Icons.Default.Close, null,
                        Modifier.padding(start = 2.dp).width(16.dp))
                },
                modifier = Modifier.testTag("filterChip:tag:$t"),
            )
        }
    }
}

/** Filter bottom sheet — `#bottom-sheet` parity: artist section, tag
 *  section, reset/apply footer. Selection mutates the shared filter state
 *  live (web chips apply immediately; the footer buttons mirror the
 *  web's reset/apply affordances). */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun FacetSheet(
    facets: Facets?,
    artists: Set<String>,
    tags: Set<String>,
    onToggleArtist: (String) -> Unit,
    onToggleTag: (String) -> Unit,
    onReset: () -> Unit,
    onDismiss: () -> Unit,
) {
    ModalBottomSheet(onDismissRequest = onDismiss) {
        Column(
            Modifier
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 16.dp)
                .padding(bottom = 16.dp)
                .heightIn(max = 480.dp),
        ) {
            Text(stringResource(R.string.facet_artists),
                style = MaterialTheme.typography.titleSmall)
            FacetChips(facets?.artists.orEmpty(), artists, onToggleArtist, "artist")
            Spacer(Modifier.height(12.dp))
            Text(stringResource(R.string.facet_tags),
                style = MaterialTheme.typography.titleSmall)
            FacetChips(facets?.tags.orEmpty(), tags, onToggleTag, "tag")
            Spacer(Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                TextButton(onClick = onReset,
                    modifier = Modifier.testTag("facetReset")) {
                    Text(stringResource(R.string.facet_reset))
                }
                TextButton(onClick = onDismiss,
                    modifier = Modifier.testTag("facetApply")) {
                    Text(stringResource(R.string.facet_apply))
                }
            }
        }
    }
}

@Composable
private fun FacetChips(
    options: List<String>,
    selected: Set<String>,
    onToggle: (String) -> Unit,
    kind: String,
) {
    // Chips wrap — FlowRow unavailable, so chunk into fixed rows.
    options.chunked(4).forEach { row ->
        Row(
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            modifier = Modifier.padding(vertical = 2.dp),
        ) {
            row.forEach { v ->
                FilterChip(
                    selected = v in selected,
                    onClick = { onToggle(v) },
                    label = { Text(v, maxLines = 1, overflow = TextOverflow.Ellipsis) },
                    modifier = Modifier.testTag("facetChip:$kind:$v"),
                )
            }
        }
    }
}
