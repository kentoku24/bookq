package dev.bookq.reader.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.produceState
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.navigation.NavBackStackEntry
import androidx.navigation.NavController
import dev.bookq.core.browse.BrowseRules
import dev.bookq.reader.AppGraph
import dev.bookq.reader.R
import dev.bookq.reader.ui.browse.RETURN_ANCHOR_KEY

/**
 * M2 reader placeholder — verifies the browse→reader→browse loop
 * (navigation, return anchor, scroll restore) before M3 lands the real
 * viewer. Records progress-equivalent state by writing the work path as
 * the return anchor for the browse level underneath.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ReaderStubScreen(
    graph: AppGraph,
    nav: NavController,
    folder: String,
    page: Int,
) {
    val scope by graph.serverScope.collectAsStateWithLifecycle()
    val count by produceState<Int?>(null, scope) {
        val api = scope?.api ?: return@produceState
        value = try {
            api.images(folder).size
        } catch (e: Exception) {
            -1
        }
    }

    fun goBack() {
        // Anchor = the work's own path — scrolls its tile into view on the
        // browse level being returned to (scroll.js VIEWER via).
        nav.previousBackStackEntry
            ?.savedStateHandle
            ?.set(RETURN_ANCHOR_KEY, folder)
        nav.popBackStack()
    }
    BackHandler { goBack() }

    Scaffold(
        topBar = {
            TopAppBar(
                navigationIcon = {
                    IconButton(onClick = { goBack() }) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, null)
                    }
                },
                title = {
                    Text(
                        if (folder.isEmpty()) {
                            stringResource(R.string.reader_root_title)
                        } else {
                            BrowseRules.splitDisplayName(
                                folder.substringAfterLast('/')).first
                        },
                        maxLines = 1,
                    )
                },
            )
        },
    ) { padding ->
        Box(
            Modifier.fillMaxSize().padding(padding),
            contentAlignment = Alignment.Center,
        ) {
            Column(
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.Center,
            ) {
                when (val c = count) {
                    null -> CircularProgressIndicator()
                    -1 -> Text(stringResource(R.string.home_error),
                        color = MaterialTheme.colorScheme.error)
                    else -> Text(
                        stringResource(R.string.reader_stub_pages, c, page + 1),
                        style = MaterialTheme.typography.titleMedium,
                        modifier = Modifier.testTag("readerStub"),
                    )
                }
            }
        }
    }
}
