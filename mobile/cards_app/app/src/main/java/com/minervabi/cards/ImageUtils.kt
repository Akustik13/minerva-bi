package com.minervabi.cards

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.BitmapRegionDecoder
import android.graphics.Matrix
import android.graphics.Rect
import android.graphics.RectF
import android.net.Uri
import androidx.exifinterface.media.ExifInterface
import java.io.File
import java.io.FileOutputStream
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt

/** Фото: обрізати по рамці видошукача, повернути за EXIF, зменшити до maxPx, JPEG; мініатюри для списків. */
object ImageUtils {

    private fun rotationOf(exif: ExifInterface): Int = when (exif.getAttributeInt(
        ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL)) {
        ExifInterface.ORIENTATION_ROTATE_90 -> 90
        ExifInterface.ORIENTATION_ROTATE_180 -> 180
        ExifInterface.ORIENTATION_ROTATE_270 -> 270
        else -> 0
    }

    /**
     * Знімок камери → лише те, що в рамці. frame — рамка у px PreviewView (null — увесь видимий кадр), view — розмір
     * PreviewView (FILL_CENTER: видно середину знімка). Читає з файлу лише потрібну ділянку (BitmapRegionDecoder) —
     * 50-мегапіксельний знімок не забиває пам'ять.
     */
    fun cropToFrame(src: File, out: File, frame: RectF?, viewW: Int, viewH: Int, maxPx: Int): Bitmap? {
        val rot = runCatching { rotationOf(ExifInterface(src.absolutePath)) }.getOrDefault(0)
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeFile(src.absolutePath, bounds)
        val w = bounds.outWidth
        val h = bounds.outHeight
        if (w <= 0 || h <= 0) return null
        val rw = if (rot == 90 || rot == 270) h else w     // розмір знімка, як його видно (після повороту)
        val rh = if (rot == 90 || rot == 270) w else h
        // що з знімка видно в PreviewView (FILL_CENTER) і де там рамка
        var crop = RectF(0f, 0f, rw.toFloat(), rh.toFloat())
        if (viewW > 0 && viewH > 0) {
            val scale = max(viewW / rw.toFloat(), viewH / rh.toFloat())
            val visW = viewW / scale
            val visH = viewH / scale
            val left = (rw - visW) / 2f
            val top = (rh - visH) / 2f
            crop = if (frame != null) RectF(left + frame.left / scale, top + frame.top / scale,
                left + frame.right / scale, top + frame.bottom / scale)
            else RectF(left, top, left + visW, top + visH)
        }
        crop.intersect(0f, 0f, rw.toFloat(), rh.toFloat())
        // та сама ділянка у знімку до повороту
        val raw = when (rot) {
            90 -> RectF(crop.top, h - crop.right, crop.bottom, h - crop.left)
            180 -> RectF(w - crop.right, h - crop.bottom, w - crop.left, h - crop.top)
            270 -> RectF(w - crop.bottom, crop.left, w - crop.top, crop.right)
            else -> crop
        }
        val r = Rect(raw.left.roundToInt().coerceIn(0, w - 1), raw.top.roundToInt().coerceIn(0, h - 1),
            raw.right.roundToInt().coerceIn(1, w), raw.bottom.roundToInt().coerceIn(1, h))
        if (r.width() < 20 || r.height() < 20) return null
        var sample = 1
        while (max(r.width(), r.height()) / (sample * 2) >= maxPx) sample *= 2
        @Suppress("DEPRECATION")
        val decoder = BitmapRegionDecoder.newInstance(src.absolutePath, false) ?: return null
        val part = try {
            decoder.decodeRegion(r, BitmapFactory.Options().apply { inSampleSize = sample })
        } finally {
            decoder.recycle()
        } ?: return null
        return finish(part, rot, maxPx, out)
    }

    /** Повернути, зменшити, зберегти JPEG 90. */
    private fun finish(bmp: Bitmap, rot: Int, maxPx: Int, out: File): Bitmap {
        var b = bmp
        val longest = max(b.width, b.height)
        val k = if (longest > maxPx) maxPx / longest.toFloat() else 1f
        if (rot != 0 || k < 1f) {
            val m = Matrix()
            if (k < 1f) m.postScale(k, k)
            if (rot != 0) m.postRotate(rot.toFloat())
            val nb = Bitmap.createBitmap(b, 0, 0, b.width, b.height, m, true)
            if (nb !== b) b.recycle()
            b = nb
        }
        FileOutputStream(out).use { b.compress(Bitmap.CompressFormat.JPEG, 90, it) }
        return b
    }

    /** Фото з галереї / сканера документів → файл знімка (поворот за EXIF, ≤ maxPx). */
    fun fromUri(context: Context, uri: Uri, out: File, maxPx: Int): Bitmap? {
        val cr = context.contentResolver
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        cr.openInputStream(uri)?.use { BitmapFactory.decodeStream(it, null, bounds) } ?: return null
        if (bounds.outWidth <= 0) return null
        var sample = 1
        while (max(bounds.outWidth, bounds.outHeight) / (sample * 2) >= maxPx) sample *= 2
        val bmp = cr.openInputStream(uri)?.use {
            BitmapFactory.decodeStream(it, null, BitmapFactory.Options().apply { inSampleSize = sample })
        } ?: return null
        val rot = runCatching { cr.openInputStream(uri)?.use { rotationOf(ExifInterface(it)) } ?: 0 }.getOrDefault(0)
        return finish(bmp, rot, maxPx, out)
    }

    /** Мініатюра з файлу (для списку) — довша сторона ≈ side px. */
    fun thumb(file: String, side: Int = 360): Bitmap? {
        if (file.isEmpty() || !File(file).exists()) return null
        val o = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeFile(file, o)
        var sample = 1
        while (max(o.outWidth, o.outHeight) / (sample * 2) >= side) sample *= 2
        return BitmapFactory.decodeFile(file, BitmapFactory.Options().apply { inSampleSize = sample })
    }

    fun thumb(bytes: ByteArray, side: Int = 480): Bitmap? {
        val o = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size, o)
        var sample = 1
        while (min(o.outWidth, o.outHeight) / (sample * 2) >= side) sample *= 2
        return BitmapFactory.decodeByteArray(bytes, 0, bytes.size, BitmapFactory.Options().apply { inSampleSize = sample })
    }
}
