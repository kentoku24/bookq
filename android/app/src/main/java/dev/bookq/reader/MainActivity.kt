package dev.bookq.reader

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Surface
import androidx.compose.runtime.getValue
import androidx.compose.ui.ExperimentalComposeUiApi
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.testTagsAsResourceId
import androidx.core.splashscreen.SplashScreen.Companion.installSplashScreen
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.lifecycleScope
import dev.bookq.reader.debug.FpsHud
import dev.bookq.reader.ui.Root
import dev.bookq.reader.ui.theme.BookqTheme
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {

    override fun onNewIntent(intent: android.content.Intent) {
        super.onNewIntent(intent)
        applyDebugServerUrl(intent)
    }

    private fun applyDebugServerUrl(intent: android.content.Intent?) {
        if (!BuildConfig.DEBUG) return
        val graph = (application as BookqApp).graph
        intent?.getStringExtra("serverURL")?.let { url ->
            lifecycleScope.launch {
                graph.settings.awaitLoaded()
                graph.model.setServerAndBoot(url)
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        installSplashScreen()
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        val graph = (application as BookqApp).graph

        // Debug-only preset: `am start --es serverURL <url>` boots straight
        // into the given server (iOS -serverURL parity, dev.sh flow).
        // Cold start only — re-applying on recreation (rotation etc.)
        // would log the user out again via serverChanged.
        if (savedInstanceState == null) {
            applyDebugServerUrl(intent)
        }

        setContent {
            val dynamicColor by graph.settings.dynamicColor
                .collectAsStateWithLifecycle()
            val perfHud by graph.settings.perfHud.collectAsStateWithLifecycle()
            BookqTheme(dynamicColor = dynamicColor) {
                // Expose testTags to UiAutomator as resource-ids
                // (connectedDebugAndroidTest locators use By.res).
                @OptIn(ExperimentalComposeUiApi::class)
                Surface(modifier = Modifier.fillMaxSize().semantics {
                    testTagsAsResourceId = true
                }) {
                    Box {
                        Root(graph)
                        if (BuildConfig.DEBUG && perfHud) {
                            FpsHud()
                        }
                    }
                }
            }
        }
    }
}
