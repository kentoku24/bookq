package dev.bookq.reader.ui

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.ui.platform.testTag
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import androidx.navigation.toRoute
import dev.bookq.reader.AppGraph
import dev.bookq.reader.AppPhase
import dev.bookq.reader.R
import dev.bookq.reader.nav.Routes
import dev.bookq.reader.ui.browse.BrowseScreen
import dev.bookq.reader.ui.reader.ReaderScreen

/** Phase router: onboarding → booting → login / library. */
@Composable
fun Root(graph: AppGraph) {
    val phase by graph.model.phase.collectAsStateWithLifecycle()
    when (val p = phase) {
        AppPhase.Onboarding -> OnboardingScreen(graph)
        AppPhase.Booting -> BootingScreen()
        is AppPhase.BootError -> BootErrorScreen(graph, p.message)
        AppPhase.Login -> LoginScreen(graph)
        AppPhase.Ready -> ReadyNavHost(graph)
    }
}

@Composable
private fun BootingScreen() {
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            CircularProgressIndicator()
            Spacer(Modifier.height(16.dp))
            Text(stringResource(R.string.boot_connecting))
        }
    }
}

@Composable
private fun BootErrorScreen(graph: AppGraph, message: String) {
    Box(Modifier.fillMaxSize().padding(24.dp), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Text(message, color = MaterialTheme.colorScheme.error)
            Spacer(Modifier.height(16.dp))
            Button(onClick = { graph.model.boot() }) {
                Text(stringResource(R.string.boot_retry))
            }
            Spacer(Modifier.height(8.dp))
            OutlinedButton(onClick = { graph.model.showOnboarding() }) {
                Text(stringResource(R.string.boot_change_server))
            }
        }
    }
}

@Composable
private fun ReadyNavHost(graph: AppGraph) {
    val nav = rememberNavController()
    val online by graph.online.online.collectAsStateWithLifecycle()
    Column(Modifier.fillMaxSize()) {
        if (!online) {
            // Amber offline banner — web status-dot parity: only shown
            // while the default network is unvalidated/absent.
            Surface(
                color = androidx.compose.ui.graphics.Color(0xFFC9A24F),
                modifier = Modifier.fillMaxWidth().testTag("offlineBanner"),
            ) {
                Text(
                    stringResource(R.string.offline_banner),
                    color = androidx.compose.ui.graphics.Color(0xFF1A1A1A),
                    style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.padding(horizontal = 16.dp, vertical = 6.dp),
                )
            }
        }
    NavHost(navController = nav, startDestination = Routes.Browse(),
        modifier = Modifier.weight(1f)) {
        composable<Routes.Browse> { entry ->
            val route = entry.toRoute<Routes.Browse>()
            BrowseScreen(
                graph = graph,
                entry = entry,
                path = route.path,
                onNavigate = { nav.navigate(Routes.Browse(it)) },
                onOpenReader = { folder, page ->
                    nav.navigate(Routes.Reader(folder, page))
                },
                onSettings = { nav.navigate(Routes.Settings) },
                onBack = { nav.popBackStack() },
                // Leaving show-all inside a hidden root bounces here —
                // the whole browse stack is hidden content, so reset it.
                onTop = {
                    nav.navigate(Routes.Browse("")) {
                        popUpTo(0) { inclusive = true }
                    }
                },
            )
        }
        composable<Routes.Reader> { entry ->
            val route = entry.toRoute<Routes.Reader>()
            ReaderScreen(
                graph = graph,
                nav = nav,
                folder = route.folder,
                page = route.page,
            )
        }
        composable<Routes.Settings> {
            SettingsScreen(graph, onBack = { nav.popBackStack() })
        }
    }
    }
}
