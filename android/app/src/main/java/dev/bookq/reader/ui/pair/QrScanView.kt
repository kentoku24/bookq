package dev.bookq.reader.ui.pair

import androidx.annotation.OptIn
import androidx.camera.core.CameraSelector
import androidx.camera.core.ExperimentalGetImage
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.LocalLifecycleOwner
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.common.InputImage
import dev.bookq.core.sync.PairJoinCode

/* CameraX + ML Kit barcode preview — QR-only analyzer on the bundled
 * model (no GMS dependency, #300). Emits the first decoded join code
 * (`<origin>/#/join/<code>` or a bare 6-digit) once, then stops the
 * analysis stream until recomposition. */
@OptIn(ExperimentalGetImage::class)
@Composable
fun QrScanView(
    onCode: (String) -> Unit,
    modifier: Modifier = Modifier,
) {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    val previewView = remember { PreviewView(context) }

    DisposableEffect(lifecycleOwner) {
        val executor = ContextCompat.getMainExecutor(context)
        val scanner = BarcodeScanning.getClient(
            com.google.mlkit.vision.barcode.BarcodeScannerOptions.Builder()
                .setBarcodeFormats(Barcode.FORMAT_QR_CODE)
                .build())
        val analysis = ImageAnalysis.Builder()
            .setBackpressureStrategy(
                ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
            .build()
        var provider: ProcessCameraProvider? = null
        var emitted = false
        analysis.setAnalyzer(executor) { proxy ->
            val media = proxy.image
            if (media == null || emitted) {
                proxy.close(); return@setAnalyzer
            }
            scanner.process(InputImage.fromMediaImage(
                    media, proxy.imageInfo.rotationDegrees))
                .addOnSuccessListener { codes ->
                    if (!emitted) {
                        codes.firstNotNullOfOrNull { b ->
                            b.rawValue?.let(PairJoinCode::parse)
                        }?.let { emitted = true; onCode(it) }
                    }
                }
                .addOnCompleteListener { proxy.close() }
        }
        val future = ProcessCameraProvider.getInstance(context)
        future.addListener({
            val p = runCatching { future.get() }.getOrNull() ?: return@addListener
            provider = p
            val preview = Preview.Builder().build().also {
                it.surfaceProvider = previewView.surfaceProvider
            }
            runCatching {
                p.unbindAll()
                p.bindToLifecycle(lifecycleOwner,
                    CameraSelector.DEFAULT_BACK_CAMERA, preview, analysis)
            }
        }, executor)
        onDispose {
            provider?.unbindAll()
            runCatching { scanner.close() }
        }
    }
    AndroidView({ previewView }, modifier)
}
