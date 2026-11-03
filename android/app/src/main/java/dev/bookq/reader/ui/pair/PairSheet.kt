package dev.bookq.reader.ui.pair

import androidx.compose.foundation.Image
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import dev.bookq.core.sync.PairPhase
import dev.bookq.core.sync.PairRole
import dev.bookq.core.sync.PairStore
import kotlinx.coroutines.launch

/**
 * ペアリング sheet — iOS PairSheet / pairUI.js equivalent: create
 * (code+QR) / join (6桁) / auto-connect (MATCH) / session info / leave.
 * Auto-approve means the leader never sees an accept button.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PairSheet(
    pair: PairStore,
    wsBaseUrl: String?,      // origin for the QR payload (<host>/#/join/<code>)
    onDismiss: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val status by pair.status.collectAsState()
    val code by pair.code.collectAsState()
    val joinRequest by pair.joinRequest.collectAsState()
    val peer = pair.peer
    val phase by pair.lastState.collectAsState()
    val error by pair.errorText.collectAsState()
    val scope = rememberCoroutineScope()

    var joinCode by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var localError by remember { mutableStateOf<String?>(null) }

    fun run(op: suspend () -> Unit) {
        busy = true; localError = null
        scope.launch {
            try {
                op()
            } catch (e: dev.bookq.core.sync.PairException.Rejected) {
                localError = e.reason
            } catch (e: Exception) {
                localError = e.message
            }
            busy = false
        }
    }

    ModalBottomSheet(onDismissRequest = onDismiss, modifier = modifier) {
        Column(Modifier.padding(horizontal = 20.dp, vertical = 8.dp)) {
            Text("ペアリング", style = MaterialTheme.typography.titleLarge)
            Spacer(Modifier.height(12.dp))

            (localError ?: error)?.let {
                Text(it, color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.testTag("pairError"))
                Spacer(Modifier.height(8.dp))
            }

            when (val st = status) {
                PairStore.UiStatus.Idle -> Column {
                    Text("新しいセッション",
                        style = MaterialTheme.typography.titleSmall)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        FilledTonalButton(
                            onClick = { run { pair.create() } },
                            enabled = !busy,
                            modifier = Modifier.testTag("pairCreate")) {
                            Text("コードを発行する")
                        }
                        FilledTonalButton(
                            onClick = { run { pair.match() } },
                            enabled = !busy,
                            modifier = Modifier.testTag("pairMatch")) {
                            Text("自動で接続")
                        }
                    }
                    Spacer(Modifier.height(16.dp))
                    Text("コードで参加",
                        style = MaterialTheme.typography.titleSmall)
                    Row(verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        OutlinedTextField(
                            value = joinCode,
                            onValueChange = {
                                joinCode = it.filter(Char::isDigit).take(6)
                            },
                            placeholder = { Text("6桁のコード") },
                            singleLine = true,
                            keyboardOptions = KeyboardOptions(
                                keyboardType = KeyboardType.NumberPassword),
                            modifier = Modifier.weight(1f)
                                .testTag("pairJoinCode"))
                        Button(
                            onClick = { run { pair.join(joinCode) } },
                            enabled = joinCode.length == 6 && !busy,
                            modifier = Modifier.testTag("pairJoin")) {
                            Text("参加する")
                        }
                    }
                }
                PairStore.UiStatus.Connecting -> Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    CircularProgressIndicator(Modifier.size(20.dp))
                    Text("接続中…")
                }
                PairStore.UiStatus.Waiting -> Column(
                    horizontalAlignment = Alignment.CenterHorizontally) {
                    Text("このコードを相手に入力",
                        style = MaterialTheme.typography.titleSmall)
                    Text(code ?: "------",
                        style = MaterialTheme.typography.displaySmall,
                        fontFamily = FontFamily.Monospace,
                        modifier = Modifier.testTag("pairCode"))
                    val host = wsBaseUrl
                    val c = code
                    if (c != null && host != null) {
                        QrImage("$host/#/join/$c",
                            modifier = Modifier.size(168.dp)
                                .testTag("pairQr"))
                    }
                    Spacer(Modifier.height(8.dp))
                    joinRequest?.let { jr ->
                        Text("${jr.name} が参加を申請中…")
                    } ?: Text("参加者を待っています",
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Spacer(Modifier.height(8.dp))
                    TextButton(onClick = { pair.leave(); onDismiss() }) {
                        Text("キャンセル")
                    }
                }
                PairStore.UiStatus.Pending -> Column {
                    Text("リーダーの承認待ち…")
                    TextButton(onClick = { pair.leave(); onDismiss() }) {
                        Text("キャンセル")
                    }
                }
                PairStore.UiStatus.Paired -> Column {
                    HorizontalDivider()
                    Row(Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween) {
                        Text("あなた")
                        Text(if (pair.role == PairRole.leader)
                            "リーダー" else "フォロワー")
                    }
                    peer?.let { p ->
                        Row(Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.SpaceBetween) {
                            Text("相手")
                            Text(p.name.ifEmpty { p.role.name })
                        }
                        Row(Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.SpaceBetween) {
                            Text("状態")
                            Text(when (p.status) {
                                "active" -> if (p.ready) "準備完了" else "接続中"
                                "suspended" -> "離席中"
                                "offline" -> "オフライン"
                                else -> p.status
                            })
                        }
                    }
                    phase?.let { s ->
                        Row(Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.SpaceBetween) {
                            Text("フェーズ")
                            Text(when (s.phase) {
                                PairPhase.waiting -> "待機中"
                                PairPhase.selecting -> "作品選択中"
                                PairPhase.loading -> "読み込み中"
                                PairPhase.reading -> "同期読書中"
                                PairPhase.paused -> "一時停止"
                                PairPhase.ended -> "終了"
                            })
                        }
                    }
                    Spacer(Modifier.height(12.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        OutlinedButton(
                            onClick = { pair.swapSides() },
                            enabled = s_reading(phase),
                            modifier = Modifier.testTag("pairSwap")) {
                            Text("左右を入れ替える")
                        }
                        OutlinedButton(
                            onClick = { pair.leave(); onDismiss() },
                            modifier = Modifier.testTag("pairLeave")) {
                            Text("ペアを解除")
                        }
                    }
                }
                is PairStore.UiStatus.Ended -> Column {
                    Text(endReasonText(st.reason))
                    TextButton(onClick = {
                        localError = null; pair.disconnect()
                    }, modifier = Modifier.testTag("pairRetry")) {
                        Text("もう一度")
                    }
                }
            }
            Spacer(Modifier.height(24.dp))
        }
    }
}

private fun s_reading(s: dev.bookq.core.sync.PairSnapshot?): Boolean =
    s?.phase == PairPhase.reading

fun endReasonText(r: String): String = when (r) {
    "left" -> "ペアを解除しました"
    "rejected" -> "参加が拒否されました"
    "code_expired" -> "コードの有効期限が切れました"
    "resume_expired" -> "再接続の期限が切れました"
    "session_lost" -> "セッションが失われました"
    "resume_in_use" -> "別の端末が参加中です"
    "incompatible" -> "プロトコルバージョンが非対応です"
    else -> "終了: $r"
}
