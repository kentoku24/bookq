package dev.bookq.reader.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import dev.bookq.reader.AppGraph
import dev.bookq.reader.R
import dev.bookq.reader.ServerUrls
import kotlinx.coroutines.launch

/** First-run / reconfigure: enter the Tailscale HTTPS URL, probe
 *  /api/v1/health, then move on to login or library. */
@Composable
fun OnboardingScreen(graph: AppGraph) {
    val saved by graph.settings.serverUrl.collectAsStateWithLifecycle()
    var text by remember(saved) { mutableStateOf(saved) }
    var checking by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()
    val errHttp = stringResource(R.string.onboarding_error_http)
    val errInvalid = stringResource(R.string.onboarding_error_invalid)
    val errUnreachable = stringResource(R.string.onboarding_error_unreachable)

    Column(
        modifier = Modifier.fillMaxSize().padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Text(
            stringResource(R.string.onboarding_title),
            style = MaterialTheme.typography.displaySmall,
        )
        Spacer(Modifier.height(12.dp))
        Text(
            stringResource(R.string.onboarding_hint),
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
        )
        Spacer(Modifier.height(24.dp))
        OutlinedTextField(
            value = text,
            onValueChange = { text = it; error = null },
            placeholder = { Text(stringResource(R.string.onboarding_url_placeholder)) },
            singleLine = true,
            enabled = !checking,
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
            modifier = Modifier.widthIn(max = 420.dp).fillMaxWidth(),
        )
        Spacer(Modifier.height(16.dp))
        Button(
            enabled = !checking && text.isNotBlank(),
            onClick = {
                val normalized = ServerUrls.normalize(text)
                if (normalized == null) {
                    error = if (text.trim().startsWith("http://")) {
                        errHttp
                    } else {
                        errInvalid
                    }
                    return@Button
                }
                checking = true
                scope.launch {
                    val ok = graph.healthCheck(normalized)
                    checking = false
                    if (ok) {
                        graph.model.setServerAndBoot(normalized)
                    } else {
                        error = errUnreachable
                    }
                }
            },
            modifier = Modifier.widthIn(max = 280.dp).fillMaxWidth(),
        ) {
            if (checking) {
                CircularProgressIndicator(
                    Modifier.size(18.dp),
                    color = MaterialTheme.colorScheme.onPrimary,
                    strokeWidth = 2.dp,
                )
                Spacer(Modifier.height(0.dp))
            }
            Text(
                stringResource(
                    if (checking) R.string.onboarding_connecting
                    else R.string.onboarding_connect),
            )
        }
        error?.let {
            Spacer(Modifier.height(12.dp))
            Text(
                it,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.error,
                textAlign = TextAlign.Center,
            )
        }
    }
}
