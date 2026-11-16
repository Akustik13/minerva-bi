package com.minervabi.cards

import android.graphics.Bitmap
import androidx.annotation.OptIn
import androidx.camera.core.ExperimentalGetImage
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import com.google.android.gms.tasks.Tasks
import com.google.mlkit.vision.barcode.BarcodeScanner
import com.google.mlkit.vision.barcode.BarcodeScannerOptions
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.common.InputImage

/** QR-коди (ML Kit, модель у додатку): живий кадр видошукача і готовий знімок. */
object Qr {
    /** Кілька кодів на одному боці — розділювач, який Minerva розуміє (suppliers/services/qr.py SEPARATOR). */
    const val SEPARATOR = "\n\n----\n\n"

    fun scanner(): BarcodeScanner = BarcodeScanning.getClient(
        BarcodeScannerOptions.Builder().setBarcodeFormats(Barcode.FORMAT_QR_CODE).build())

    /** Аналізатор CameraX: кожен кадр → коди (onFound на головному потоці ML Kit). */
    class Analyzer(private val scanner: BarcodeScanner, private val onFound: (List<String>) -> Unit) :
        ImageAnalysis.Analyzer {
        @OptIn(ExperimentalGetImage::class)
        override fun analyze(proxy: ImageProxy) {
            val media = proxy.image
            if (media == null) {
                proxy.close(); return
            }
            val img = InputImage.fromMediaImage(media, proxy.imageInfo.rotationDegrees)
            scanner.process(img)
                .addOnSuccessListener { codes -> onFound(codes.mapNotNull { it.rawValue?.trim() }.filter { it.isNotEmpty() }) }
                .addOnCompleteListener { proxy.close() }
        }
    }

    /** Усі QR на готовому знімку (у фоновому потоці). */
    fun read(bmp: Bitmap?): List<String> {
        if (bmp == null) return emptyList()
        val s = scanner()
        return try {
            Tasks.await(s.process(InputImage.fromBitmap(bmp, 0)))
                .mapNotNull { it.rawValue?.trim() }.filter { it.isNotEmpty() }.distinct()
        } catch (e: Exception) {
            emptyList()
        } finally {
            s.close()
        }
    }

    fun join(codes: Collection<String>): String = codes.filter { it.isNotBlank() }.distinct().joinToString(SEPARATOR)

    /** Коротко для підказки: «👤 Wendy Wu» з vCard, сайт або перші символи. */
    fun short(text: String): String {
        val fn = Regex("(?im)^FN[^:]*:(.+)$").find(text)?.groupValues?.get(1)?.trim()
        if (!fn.isNullOrEmpty()) return "👤 $fn"
        val mecard = Regex("(?i)MECARD:N:([^;]+)").find(text)?.groupValues?.get(1)?.trim()
        if (!mecard.isNullOrEmpty()) return "👤 $mecard"
        return text.lineSequence().firstOrNull()?.take(40) ?: ""
    }
}
