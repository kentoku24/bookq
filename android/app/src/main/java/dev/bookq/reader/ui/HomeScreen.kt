package dev.bookq.reader.ui

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Settings
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import dev.bookq.data.ApiException
import dev.bookq.data.TreeResult
import dev.bookq.reader.AppGraph
import dev.bookq.reader.R
import dev.bookq.reader.trace.BookqTrace

private sealed interface HomeState {
    data object Loading : HomeState
    data class Loaded(val names: List<String>) : HomeState
    data class Error(val message: String) : HomeState
}

/**
 * M1 placeholder Home — fetches /api/v1/tree and lists top-level folder
 * names (M2 replaces this with the real browse grid; it exists so the
 * smoke UI test has something real to assert).
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun HomeScreen(graph: AppGraph, onSettings: () -> Unit) {
    val api = graph.model.api
    var reloadTick by remember { mutableStateOf(0) }
    val state by produceState<HomeState>(HomeState.Loading, api, reloadTick) {
        if (api == null) {
            value = HomeState.Error("no api")
        } else {
            BookqTrace.begin(BookqTrace.TREE_LOAD)
            value = try {
                val result = api.tree()
                BookqTrace.end()
                when (result) {
                    is TreeResult.Nodes ->
                        HomeState.Loaded(result.nodes.map { it.name })
                    TreeResult.NotModified ->
                        HomeState.Loaded(emptyList())
                }
            } catch (e: ApiException) {
                BookqTrace.end()
                HomeState.Error(e.message ?: "error")
            } catch (e: Exception) {
                BookqTrace.end()
                HomeState.Error(e.message ?: "error")
            }
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(stringResource(R.string.home_title)) },
                actions = {
                    IconButton(onClick = onSettings) {
                        Icon(
                            Icons.Default.Settings,
                            contentDescription = stringResource(R.string.settings_title),
                        )
                    }
                },
            )
        },
    ) { padding ->
        Box(Modifier.fillMaxSize().padding(padding)) {
            when (val s = state) {
                HomeState.Loading -> Box(
                    Modifier.fillMaxSize(),
                    contentAlignment = Alignment.Center,
                ) { CircularProgressIndicator() }
                is HomeState.Error -> Column(
                    Modifier.fillMaxSize().padding(24.dp),
                    horizontalAlignment = Alignment.CenterHorizontally,
                    verticalArrangement = androidx.compose.foundation.layout.Arrangement.Center,
                ) {
                    Text(
                        stringResource(R.string.home_error),
                        color = MaterialTheme.colorScheme.error,
                    )
                    Spacer(Modifier.height(8.dp))
                    TextButton(onClick = { reloadTick += 1 }) {
                        Text(stringResource(R.string.home_reload))
                    }
                }
                is HomeState.Loaded -> LazyColumn(Modifier.fillMaxSize()) {
                    items(s.names) { name ->
                        Text(
                            name,
                            style = MaterialTheme.typography.bodyLarge,
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(horizontal = 16.dp, vertical = 12.dp),
                        )
                        HorizontalDivider()
                    }
                }
            }
        }
    }
}
