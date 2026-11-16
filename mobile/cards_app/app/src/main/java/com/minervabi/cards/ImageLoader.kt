package com.minervabi.cards

import android.content.Context
import android.graphics.Bitmap
import android.util.LruCache
import android.widget.ImageView
import androidx.lifecycle.LifecycleCoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

/** Мініатюри: свої знімки — з файлу, візитки Minerva — з сервера (з ключем телефону) + кеш у пам'яті й на диску. */
object ImageLoader {
    private val mem = object : LruCache<String, Bitmap>(24 * 1024 * 1024) {
        override fun sizeOf(key: String, value: Bitmap) = value.byteCount
    }

    fun local(scope: LifecycleCoroutineScope, view: ImageView, path: String) {
        load(scope, view, "f:$path") { ImageUtils.thumb(path) }
    }

    fun server(context: Context, scope: LifecycleCoroutineScope, view: ImageView, id: Int, side: String = "front",
               full: Boolean = false) {
        val key = "s:${Prefs.server}:$id:$side:${if (full) "full" else "thumb"}"
        val disk = File(context.cacheDir, "img-" + key.hashCode().toUInt().toString(16) + ".jpg")
        load(scope, view, key) {
            val bytes = if (disk.exists() && System.currentTimeMillis() - disk.lastModified() < 7L * 86400_000) disk.readBytes()
            else Api.image(id, side, full)?.also { runCatching { disk.writeBytes(it) } }
            bytes?.let { ImageUtils.thumb(it, if (full) 2000 else 480) }
        }
    }

    private fun load(scope: LifecycleCoroutineScope, view: ImageView, key: String, make: () -> Bitmap?) {
        view.tag = key
        mem.get(key)?.let { view.setImageBitmap(it); return }
        view.setImageDrawable(null)
        scope.launch {
            val bmp = withContext(Dispatchers.IO) { runCatching(make).getOrNull() }
            if (bmp != null) mem.put(key, bmp)
            if (view.tag == key) view.setImageBitmap(bmp)
        }
    }

    fun forget(id: Int) {
        mem.snapshot().keys.filter { it.contains(":$id:") }.forEach { mem.remove(it) }
    }
}
