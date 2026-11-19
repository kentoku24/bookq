package dev.bookq.reader.ui.pair

import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Slider
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import coil3.compose.SubcomposeAsyncImage
import coil3.request.ImageRequest
import coil3.size.Scale
import dev.bookq.core.sync.PairPhase
import dev.bookq.core.sync.PairReaderGate
import dev.bookq.core.sync.PairRole
import dev.bookq.core.sync.PairStore

/**
 * Paired-reader surface — the dual-reader equivalent of ReaderScreen
 * (ios PairedReaderView). Each device renders ONLY its own side of the
 * committed spread (pairedView.apply): page turns are wire intents,
 * not local moves.
 */
@Composable
fun PairedReaderScreen(
    pair: PairStore,
    pageUrlFor: (folder: String, name: String) -> String?,
    imageLoader: coil3.ImageLoader,
    onClose: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val phase by pair.lastState.collectAsState()
    val projection by pair.projection.collectAsState()
    val manifest by pair.manifest.collectAsState()
    var showChrome by remember { mutableStateOf(true) }

    // iOS onDisappear parity: a leader closing while `reading` means
    // "back to browsing" — SET_ACTIVITY selecting. Phase-driven
    // dismissals (transient loading, paused, ended) are not intents.
    DisposableEffect(Unit) {
        onDispose {
            if (pair.role == PairRole.leader && pair.bound &&
                PairReaderGate.closeSignalsSelecting(pair.phase)) {
                pair.setActivity("selecting")
            }
        }
    }

    val ph = phase?.phase
    Box(
        modifier
            .fillMaxSize()
            .background(Color.Black)
            .pointerInput(ph) {
                // Edge taps → TURN cross (web parity: taps cross chapters).
                detectTapGestures { pos ->
                    val frac = pos.x / size.width.toFloat()
                    when {
                        frac < 0.30f -> turn(pair, 1, cross = true)
                        frac > 0.70f -> turn(pair, -1, cross = true)
                        else -> showChrome = !showChrome
                    }
                }
            }
            .pointerInput(ph) {
                // Swipes clamp inside the chapter (keys do too — web).
                var acc = 0f
                detectDragGestures(
                    onDragEnd = {
                        if (acc > 60) turn(pair, -1, cross = false)
                        else if (acc < -60) turn(pair, 1, cross = false)
                        acc = 0f
                    },
                ) { _, d -> acc += d.x }
            },
    ) {
        when (ph) {
            PairPhase.reading, PairPhase.paused, PairPhase.loading -> {
                val p = projection
                if (p != null) {
                    val ref = p.ref
                    if (ref != null) {
                        SubcomposeAsyncImage(
                            model = ImageRequest.Builder(
                                androidx.compose.ui.platform.LocalContext.current)
                                .data(pageUrlFor(ref.folder, ref.name))
                                .scale(Scale.FIT)
                                .build(),
                            imageLoader = imageLoader,
                            contentDescription = ref.name,
                            contentScale = ContentScale.Fit,
                            modifier = Modifier.fillMaxSize()
                                .testTag("pairPage"),
                        )
                    } else {
                        // Blank partner side — never back-fills (web).
                        Box(Modifier.fillMaxSize()
                            .background(Color(0xFF1F1F1F)),
                            contentAlignment = Alignment.Center) {
                            Text("空白", color = Color.Gray)
                        }
                    }
                } else {
                    CircularProgressIndicator(
                        Modifier.align(Alignment.Center),
                        color = Color.White)
                }
                if (ph == PairPhase.paused) {
                    Surface(
                        color = Color.Black.copy(alpha = 0.7f),
                        shape = MaterialTheme.shapes.extraLarge,
                        modifier = Modifier
                            .align(Alignment.BottomCenter)
                            .padding(bottom = 80.dp),
                    ) {
                        Text("相手が離席中・再接続待ち",
                            color = Color.White,
                            modifier = Modifier.padding(
                                horizontal = 14.dp, vertical = 8.dp))
                    }
                }
            }
            PairPhase.selecting -> Column(
                Modifier.align(Alignment.Center).padding(24.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.spacedBy(14.dp)) {
                Text(if (pair.role == PairRole.leader)
                    "ブラウズから共有する作品を選んでください"
                    else "リーダーが作品を選ぶのを待っています",
                    color = Color.White,
                    modifier = Modifier.testTag("pairSelecting"))
                Button(onClick = onClose) { Text("閉じる") }
            }
            PairPhase.waiting -> Text("相手の参加を待っています…",
                color = Color.White, modifier = Modifier.align(Alignment.Center))
            else -> CircularProgressIndicator(
                Modifier.align(Alignment.Center), color = Color.White)
        }

        // Chrome — counter + role + swap/offset + spread slider.
        if (showChrome) {
            Column(Modifier.align(Alignment.TopCenter).fillMaxWidth()) {
                Row(
                    Modifier.fillMaxWidth().padding(10.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.SpaceBetween) {
                    TextButton(onClick = onClose,
                        modifier = Modifier.testTag("pairClose")) {
                        Text("✕", color = Color.White)
                    }
                    projection?.let { p ->
                        Surface(color = Color.Black.copy(alpha = 0.5f),
                            shape = MaterialTheme.shapes.extraLarge) {
                            Column(horizontalAlignment =
                                    Alignment.CenterHorizontally,
                                modifier = Modifier.padding(
                                    horizontal = 10.dp, vertical = 4.dp)) {
                                Text("${p.spreadIndex + 1} / ${p.spreadTotal}",
                                    color = Color.White,
                                    modifier = Modifier.testTag("pairCounter"))
                                Text(if (pair.role == PairRole.leader)
                                    "リーダー" else "フォロワー",
                                    color = Color.White.copy(alpha = 0.8f),
                                    style = MaterialTheme.typography.labelSmall)
                            }
                        }
                    }
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        TextButton(
                            onClick = { pair.swapSides() },
                            enabled = ph == PairPhase.reading,
                            modifier = Modifier.testTag("pairSwapSides")) {
                            Text("⇄", color = Color.White)
                        }
                        TextButton(
                            onClick = {
                                val cur = phase?.offset ?: 0
                                pair.setOffset(if (cur == 0) 1 else 0)
                            },
                            enabled = ph == PairPhase.reading,
                            modifier = Modifier.testTag("pairOffset")) {
                            Text("📖", color = Color.White)
                        }
                    }
                }
                val m = manifest; val p = projection
                if (m != null && p != null && m.spreadTable.size > 1) {
                    // Slider → GOTO the target spread's anchor (right) ref.
                    Slider(
                        value = p.spreadIndex.toFloat(),
                        onValueChange = { v ->
                            val s = v.toInt()
                            if (s in m.spreadTable.indices) {
                                pair.gotoPage(m.spreadTable[s].right)
                            }
                        },
                        valueRange = 0f..(m.spreadTable.size - 1).toFloat(),
                        modifier = Modifier
                            .padding(horizontal = 20.dp)
                            .testTag("pairSlider"),
                    )
                }
            }
        }
    }
}

private fun turn(pair: PairStore, delta: Int, cross: Boolean) {
    pair.turn(delta = delta, crossChapter = cross,
        fromChapter = pair.projection.value?.chapterFolder)
}
