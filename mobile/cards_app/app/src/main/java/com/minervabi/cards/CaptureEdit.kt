package com.minervabi.cards

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import java.io.File

/**
 * Зміни фото знімка: 🔄 перезняти лицьовий / зворотний бік, дознімати зворотний пізніше, 🗑 прибрати його, 📎 ще фото
 * групи (друга візитка, флаєр, каталог…), ↻ повернути. Чернетка / ще не надісланий — просто інші файли; надісланий
 * пам'ятає зміни (Capture.frontNew …) — UploadWorker дошле їх у Minerva й попросить AI прочитати групу заново.
 */
object CaptureEdit {
    /** Після змін фото надісланої візитки — AI заново, якщо його вмикали або він уже читав. */
    private fun markChanged(c: Capture) {
        if (c.status != Capture.UPLOADED) return
        c.pushError = ""
        if (c.readAi || c.card?.ai == "ai") c.reread = true
    }

    private fun drop(path: String) {
        if (path.isNotEmpty()) File(path).delete()
    }

    /** 🔄 Нове фото боку (перезняли / дознімали зворотний). alt — 📐 версія «по рамці» (лише чернетка). */
    fun setSide(uuid: String, side: String, path: String, qr: String = "", alt: String = "") {
        CaptureStore.update(uuid) { c ->
            val keepAlt = if (c.isDraft) alt else ""
            if (!c.isDraft) drop(alt)
            if (side == "back") {
                drop(c.back); drop(c.backAlt)
                c.back = path; c.backAlt = keepAlt; c.qrBack = qr
                if (c.status == Capture.UPLOADED) { c.backNew = true; c.backGone = false }
            } else {
                drop(c.front); drop(c.frontAlt)
                c.front = path; c.frontAlt = keepAlt; c.qrFront = qr
                if (c.status == Capture.UPLOADED) c.frontNew = true
            }
            markChanged(c)
        }
    }

    /** 🗑 Прибрати зворотний бік (надісланий — і в Minerva). */
    fun dropBack(uuid: String) {
        CaptureStore.update(uuid) { c ->
            drop(c.back); drop(c.backAlt)
            c.back = ""; c.backAlt = ""; c.qrBack = ""
            if (c.status == Capture.UPLOADED) {
                c.backNew = false
                c.backGone = c.card?.hasBack == true || c.backGone
            }
            markChanged(c)
        }
    }

    /** 📎 Ще фото до знімка. */
    fun addExtra(uuid: String, path: String, kind: String, qr: String = "") {
        CaptureStore.update(uuid) { c ->
            c.extras = c.extras + Extra(path = path, kind = kind, qr = qr)
            markChanged(c)
        }
    }

    /** 🔄 Перезняли ще фото: нове замість старого (у Minerva старе прибирається). */
    fun replaceExtra(uuid: String, extraUuid: String, path: String, qr: String = "") {
        CaptureStore.update(uuid) { c ->
            val old = c.extras.firstOrNull { it.uuid == extraUuid }
            val fresh = Extra(path = path, kind = old?.kind ?: "card", note = old?.note.orEmpty(), qr = qr)
            if (old != null) {
                drop(old.path)
                if (old.serverId > 0) c.dropPhotos = (c.dropPhotos + old.serverId).distinct()
            }
            c.extras = if (old == null) c.extras + fresh else c.extras.map { if (it.uuid == extraUuid) fresh else it }
            markChanged(c)
        }
    }

    /** 🗑 Прибрати ще фото. */
    fun dropExtra(uuid: String, extraUuid: String) {
        CaptureStore.update(uuid) { c ->
            val old = c.extras.firstOrNull { it.uuid == extraUuid } ?: return@update
            drop(old.path)
            if (old.serverId > 0) c.dropPhotos = (c.dropPhotos + old.serverId).distinct()
            c.extras = c.extras.filter { it.uuid != extraUuid }
            markChanged(c)
        }
    }

    /** 📐 Вибрана версія (по краях / по рамці) лишається, інша — геть з телефона. */
    fun resolveAlts(uuid: String, edges: Boolean) {
        CaptureStore.update(uuid) { c ->
            if (!edges && c.frontAlt.isNotEmpty()) { drop(c.front); c.front = c.frontAlt } else drop(c.frontAlt)
            if (!edges && c.backAlt.isNotEmpty() && c.back.isNotEmpty()) { drop(c.back); c.back = c.backAlt } else drop(c.backAlt)
            c.frontAlt = ""; c.backAlt = ""
        }
    }

    /**
     * ↻ Повернути фото на 90° за годинниковою стрілкою (у новий файл — мініатюри не плутаються). slot — front / back /
     * extra uuid. Викликати не з головного потоку. → чи вийшло.
     */
    fun rotate(uuid: String, slot: String): Boolean {
        val c = CaptureStore.get(uuid) ?: return false
        val src = when (slot) {
            "front" -> c.front
            "back" -> c.back
            else -> c.extras.firstOrNull { it.uuid == slot }?.path.orEmpty()
        }
        if (src.isEmpty() || !File(src).exists()) return false
        val bmp = BitmapFactory.decodeFile(src) ?: return false
        val turned = Bitmap.createBitmap(bmp, 0, 0, bmp.width, bmp.height, Matrix().apply { postRotate(90f) }, true)
        if (turned !== bmp) bmp.recycle()
        val out = CaptureStore.newFile(if (slot == "front" || slot == "back") slot else "extra")
        ImageUtils.save(turned, out)
        turned.recycle()
        when (slot) {
            "front", "back" -> {
                val qr = if (slot == "front") c.qrFront else c.qrBack
                // 📐 друга версія («по рамці») до повороту вже не пасує — лишаємо повернуту
                setSide(uuid, slot, out.absolutePath, qr)
            }
            else -> replaceExtra(uuid, slot, out.absolutePath, c.extras.firstOrNull { it.uuid == slot }?.qr.orEmpty())
        }
        return true
    }
}
