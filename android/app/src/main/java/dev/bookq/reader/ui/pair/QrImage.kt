package dev.bookq.reader.ui.pair

import android.graphics.Bitmap
import androidx.compose.foundation.Image
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import com.google.zxing.BarcodeFormat
import com.google.zxing.qrcode.QRCodeWriter

/** QR bitmap for the join link — ZXing core, no external dep beyond it. */
@Composable
fun QrImage(content: String, modifier: Modifier = Modifier) {
    val bitmap = remember(content) {
        runCatching {
            val size = 512
            val m = QRCodeWriter().encode(content, BarcodeFormat.QR_CODE,
                size, size)
            Bitmap.createBitmap(size, size, Bitmap.Config.RGB_565).apply {
                for (y in 0 until size) for (x in 0 until size) {
                    setPixel(x, y,
                        if (m.get(x, y)) 0xFF000000.toInt()
                        else 0xFFFFFFFF.toInt())
                }
            }
        }.getOrNull()
    }
    bitmap?.let {
        Image(bitmap = it.asImageBitmap(), contentDescription = "QRコード",
            modifier = modifier, contentScale = ContentScale.Fit)
    }
}
