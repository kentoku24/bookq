package dev.bookq.reader.ui

import android.net.Uri
import androidx.activity.ComponentActivity
import androidx.activity.compose.LocalActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.browser.auth.AuthTabIntent
import androidx.browser.customtabs.CustomTabsIntent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import dev.bookq.reader.AppGraph
import dev.bookq.reader.R
import dev.bookq.reader.ServerUrls
import dev.bookq.reader.trace.BookqTrace
import kotlinx.coroutines.launch

/**
 * RFC 8252 login: preferred path is Auth Tab (`AuthTabIntent`, callback
 * scheme `bookq` delivered in-process); where Auth Tab is unsupported the
 * browser falls back to a plain Custom Tab and the `bookq://auth`
 * intent-filter on AuthCallbackActivity completes the flow.
 */
@Composable
fun LoginScreen(graph: AppGraph) {
    val scope = rememberCoroutineScope()
    val activity = LocalActivity.current as? ComponentActivity
    val snackbar = remember { SnackbarHostState() }
    var busy by remember { mutableStateOf(false) }

    val loginError by graph.loginError.collectAsStateWithLifecycle()
    val cancelMsg = stringResource(R.string.login_cancelled)

    // Auth Tab result → exchange the returned callback URI.
    val launcher = rememberLauncherForActivityResult(
        AuthTabIntent.AuthenticateUserResultContract(),
    ) { result ->
        busy = false
        when {
            result.resultCode == AuthTabIntent.RESULT_OK && result.resultUri != null ->
                scope.launch { graph.completeLogin(result.resultUri.toString()) }
            result.resultCode == AuthTabIntent.RESULT_CANCELED ->
                scope.launch {
                    // Auth-Tab-unsupported browsers degrade to a plain tab:
                    // the deep-link may still complete the login via
                    // AuthCallbackActivity — don't cry cancel then.
                    kotlinx.coroutines.delay(300)
                    if (graph.auth.user.value == null) {
                        snackbar.showSnackbar(cancelMsg)
                    }
                }
            else ->
                scope.launch { snackbar.showSnackbar(
                    result.resultUri?.toString() ?: cancelMsg) }
        }
    }

    LaunchedEffect(loginError) {
        loginError?.let {
            snackbar.showSnackbar(it)
            graph.consumeLoginError()
        }
    }

    // Backing out of a Custom-Tab fallback delivers no result at all —
    // clear the spinner when the activity resumes without one.
    LifecycleResumeEffect(Unit) {
        busy = false
        onPauseOrDispose { }
    }

    fun launchLogin() {
        val base = ServerUrls.parse(graph.settings.serverUrl.value) ?: return
        busy = true
        scope.launch {
            val pending = try {
                graph.auth.beginLogin(base)
            } catch (e: Exception) {
                busy = false
                return@launch
            }
            val uri = Uri.parse(pending.authorizeUrl)
            try {
                AuthTabIntent.Builder().build()
                    .launch(launcher, uri, "bookq")
            } catch (e: Exception) {
                // No auth-tab-capable handler — plain Custom Tab; the
                // bookq://auth intent-filter catches the redirect.
                CustomTabsIntent.Builder().build().launchUrl(
                    activity ?: run { busy = false; return@launch }, uri)
            }
        }
    }

    Scaffold(snackbarHost = { SnackbarHost(snackbar) }) { padding ->
        Column(
            modifier = Modifier.fillMaxSize().padding(padding).padding(24.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center,
        ) {
            Text(
                stringResource(R.string.login_title),
                style = MaterialTheme.typography.displaySmall,
            )
            Spacer(Modifier.height(32.dp))
            Button(
                enabled = !busy,
                onClick = { BookqTrace.section(BookqTrace.BOOT) { launchLogin() } },
                modifier = Modifier.widthIn(max = 280.dp).fillMaxWidth(),
            ) {
                if (busy) {
                    CircularProgressIndicator(
                        Modifier.size(18.dp),
                        color = MaterialTheme.colorScheme.onPrimary,
                        strokeWidth = 2.dp,
                    )
                    Spacer(Modifier.height(0.dp))
                }
                Text(stringResource(R.string.login_google))
            }
        }
    }
}
