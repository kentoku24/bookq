package dev.bookq.reader.ui

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.SingleChoiceSegmentedButtonRow
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import dev.bookq.reader.AppGraph
import dev.bookq.reader.BuildConfig
import dev.bookq.reader.BrowseLayout
import dev.bookq.reader.R
import dev.bookq.reader.ServerUrls
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(graph: AppGraph, onBack: () -> Unit) {
    val settings = graph.settings
    val scope = rememberCoroutineScope()

    val serverUrl by settings.serverUrl.collectAsStateWithLifecycle()
    val originalImages by settings.originalImages.collectAsStateWithLifecycle()
    val browseLayout by settings.browseLayout.collectAsStateWithLifecycle()
    val volumeKeys by settings.volumeKeys.collectAsStateWithLifecycle()
    val dynamicColor by settings.dynamicColor.collectAsStateWithLifecycle()
    val perfHud by settings.perfHud.collectAsStateWithLifecycle()
    val user by graph.auth.user.collectAsStateWithLifecycle()
    val serverVersion by graph.model.serverVersion.collectAsStateWithLifecycle()

    var serverDialog by remember { mutableStateOf(false) }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(stringResource(R.string.settings_title)) },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(
                            Icons.AutoMirrored.Filled.ArrowBack,
                            contentDescription = null,
                        )
                    }
                },
            )
        },
    ) { padding ->
        Column(
            Modifier
                .fillMaxSize()
                .padding(padding)
                .verticalScroll(rememberScrollState()),
        ) {
            // サーバー
            SectionHeader(stringResource(R.string.settings_server))
            SettingRow(
                title = stringResource(R.string.settings_server_url),
                subtitle = serverUrl.ifEmpty { "—" },
                onClick = { serverDialog = true },
            )
            SwitchRow(
                title = stringResource(R.string.settings_original_images),
                checked = originalImages,
            ) { scope.launch { settings.setOriginalImages(it) } }
            HorizontalDivider()

            // オフライン保存 (M5)
            val scopeState by graph.serverScope.collectAsStateWithLifecycle()
            val offline = scopeState?.offline
            if (offline != null) {
                val records by offline.records.collectAsStateWithLifecycle()
                val (works, bytes) = records.let { offline.stats() }
                var clearConfirm by remember { mutableStateOf(false) }
                SectionHeader(stringResource(R.string.settings_offline))
                SettingRow(
                    title = stringResource(R.string.settings_offline),
                    subtitle = if (works > 0) {
                        stringResource(R.string.settings_offline_summary,
                            works, dev.bookq.core.offline.OfflineRules.fmt(bytes))
                    } else {
                        stringResource(R.string.settings_offline_empty)
                    },
                    onClick = {},
                )
                if (works > 0) {
                    TextButton(
                        onClick = { clearConfirm = true },
                        modifier = Modifier.padding(horizontal = 16.dp),
                    ) {
                        Text(stringResource(R.string.settings_offline_clear),
                            color = MaterialTheme.colorScheme.error)
                    }
                }
                if (clearConfirm) {
                    AlertDialog(
                        onDismissRequest = { clearConfirm = false },
                        text = { Text(stringResource(R.string.settings_offline_clear_confirm)) },
                        confirmButton = {
                            TextButton(onClick = {
                                clearConfirm = false
                                scope.launch { offline.clearAll() }
                            }) { Text(stringResource(R.string.settings_offline_clear)) }
                        },
                        dismissButton = {
                            TextButton(onClick = { clearConfirm = false }) {
                                Text(stringResource(R.string.settings_cancel))
                            }
                        },
                    )
                }
                HorizontalDivider()
            }

            // 表示
            SectionHeader(stringResource(R.string.settings_browse_layout))
            SingleChoiceSegmentedButtonRow(
                Modifier.fillMaxWidth().padding(horizontal = 16.dp),
            ) {
                val labels = listOf(
                    BrowseLayout.TILE to stringResource(R.string.settings_layout_tile),
                    BrowseLayout.LIST to stringResource(R.string.settings_layout_list),
                    BrowseLayout.CARD to stringResource(R.string.settings_layout_card),
                )
                labels.forEachIndexed { i, (mode, label) ->
                    SegmentedButton(
                        selected = browseLayout == mode,
                        onClick = { scope.launch { settings.setBrowseLayout(mode) } },
                        shape = SegmentedButtonDefaults.itemShape(
                            index = i, count = labels.size),
                    ) { Text(label) }
                }
            }
            SwitchRow(
                title = stringResource(R.string.settings_volume_keys),
                checked = volumeKeys,
            ) { scope.launch { settings.setVolumeKeys(it) } }
            SwitchRow(
                title = stringResource(R.string.settings_dynamic_color),
                checked = dynamicColor,
            ) { scope.launch { settings.setDynamicColor(it) } }
            if (BuildConfig.DEBUG) {
                SwitchRow(
                    title = stringResource(R.string.settings_perf_hud),
                    checked = perfHud,
                ) { scope.launch { settings.setPerfHud(it) } }
            }
            HorizontalDivider()

            // アカウント
            SectionHeader(stringResource(R.string.settings_user))
            user?.let {
                LabeledRow(stringResource(R.string.settings_user), it.name)
                LabeledRow(stringResource(R.string.settings_email), it.email)
            }
            SettingRow(
                title = stringResource(R.string.settings_logout),
                titleColor = MaterialTheme.colorScheme.error,
                onClick = {
                    graph.model.logout()
                    onBack()
                },
            )
            HorizontalDivider()

            // 情報
            SectionHeader(stringResource(R.string.settings_app_version))
            LabeledRow(
                stringResource(R.string.settings_app_version),
                BuildConfig.VERSION_NAME,
            )
            LabeledRow(
                stringResource(R.string.settings_server_version),
                serverVersion?.let { v ->
                    listOfNotNull(v.version, v.sha)
                        .joinToString(" ").ifEmpty { null }
                } ?: stringResource(R.string.settings_unknown),
            )
        }
    }

    if (serverDialog) {
        var draft by remember { mutableStateOf(serverUrl) }
        val invalid = stringResource(R.string.onboarding_error_invalid)
        var error by remember { mutableStateOf<String?>(null) }
        AlertDialog(
            onDismissRequest = { serverDialog = false },
            title = { Text(stringResource(R.string.settings_server_url)) },
            text = {
                Column {
                    OutlinedTextField(
                        value = draft,
                        onValueChange = { draft = it; error = null },
                        singleLine = true,
                        modifier = Modifier.fillMaxWidth(),
                    )
                    error?.let {
                        Text(
                            it,
                            color = MaterialTheme.colorScheme.error,
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                }
            },
            confirmButton = {
                TextButton(onClick = {
                    val normalized = ServerUrls.normalize(draft)
                    if (normalized == null) {
                        error = invalid
                    } else {
                        serverDialog = false
                        scope.launch { graph.model.setServerAndBoot(normalized) }
                    }
                }) { Text(stringResource(R.string.settings_change)) }
            },
            dismissButton = {
                TextButton(onClick = { serverDialog = false }) {
                    Text(stringResource(R.string.settings_cancel))
                }
            },
        )
    }
}

@Composable
private fun SectionHeader(title: String) {
    Text(
        title,
        style = MaterialTheme.typography.titleSmall,
        color = MaterialTheme.colorScheme.primary,
        modifier = Modifier.padding(horizontal = 16.dp, vertical = 8.dp),
    )
}

@Composable
private fun SettingRow(
    title: String,
    subtitle: String? = null,
    titleColor: androidx.compose.ui.graphics.Color = MaterialTheme.colorScheme.onSurface,
    onClick: () -> Unit,
) {
    TextButton(
        onClick = onClick,
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(Modifier.fillMaxWidth()) {
            Text(title, color = titleColor, style = MaterialTheme.typography.bodyLarge)
            subtitle?.let {
                Text(
                    it,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    style = MaterialTheme.typography.bodySmall,
                )
            }
        }
    }
}

@Composable
private fun SwitchRow(title: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(
        Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(title, Modifier.weight(1f), style = MaterialTheme.typography.bodyLarge)
        Switch(checked = checked, onCheckedChange = onChange)
    }
}

@Composable
private fun LabeledRow(label: String, value: String) {
    Row(
        Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            label,
            Modifier.weight(1f),
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Text(value, style = MaterialTheme.typography.bodyMedium)
    }
}
