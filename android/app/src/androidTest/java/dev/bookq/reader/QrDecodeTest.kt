package dev.bookq.reader

import android.graphics.Bitmap
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.google.mlkit.vision.barcode.BarcodeScannerOptions
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.common.InputImage
import com.google.zxing.BarcodeFormat
import com.google.zxing.qrcode.QRCodeWriter
import dev.bookq.core.sync.PairJoinCode
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/**
 * On-device decode of the real ML Kit bundled barcode model against
 * ZXing-generated QR bitmaps — the same scanner options QrScanView
 * wires into its CameraX analyzer (#300). Physical camera aiming
 * can't be automated; this covers the decode + parse pipeline on
 * real hardware.
 */
@RunWith(AndroidJUnit4::class)
class QrDecodeTest {

    private fun qrBitmap(content: String, size: Int = 512): Bitmap {
        val m = QRCodeWriter().encode(content, BarcodeFormat.QR_CODE, size, size)
        return Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888).also { b ->
            for (y in 0 until size) for (x in 0 until size) {
                b.setPixel(x, y, if (m[x, y]) 0xFF000000.toInt() else 0xFFFFFFFF.toInt())
            }
        }
    }

    private fun decode(content: String): String? {
        val scanner = BarcodeScanning.getClient(
            BarcodeScannerOptions.Builder()
                .setBarcodeFormats(Barcode.FORMAT_QR_CODE)
                .build())
        val latch = CountDownLatch(1)
        var raw: String? = null
        scanner.process(InputImage.fromBitmap(qrBitmap(content), 0))
            .addOnSuccessListener { codes -> raw = codes.firstOrNull()?.rawValue }
            .addOnCompleteListener { latch.countDown() }
        assertTrue("ML Kit decode timed out", latch.await(30, TimeUnit.SECONDS))
        scanner.close()
        return raw
    }

    @Test
    fun decodesJoinUrlToCode() {
        val url = "https://dev-01.javanese-goblin.ts.net/#/join/468211"
        val raw = decode(url)
        assertEquals(url, raw)
        assertNotNull(raw)
        assertEquals("468211", PairJoinCode.parse(raw!!))
    }

    @Test
    fun decodesBareSixDigits() {
        val raw = decode("123456")
        assertEquals("123456", raw)
        assertNotNull(raw)
        assertEquals("123456", PairJoinCode.parse(raw!!))
    }
}
