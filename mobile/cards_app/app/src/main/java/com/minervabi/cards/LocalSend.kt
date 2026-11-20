package com.minervabi.cards

import android.content.Context
import com.google.android.material.dialog.MaterialAlertDialogBuilder

/**
 * 📱 Знімки, зроблені без підключення до Minerva (Capture.held): після підключення — «⬆ Надіслати всі»,
 * «☑ Вибрати…» або «Пізніше» (тоді — «📤 Мої знімки → ⬆ Надіслати»). Самі вони в Minerva не йдуть.
 */
object LocalSend {
    fun ask(ctx: Context, local: List<Capture>, done: () -> Unit = {}) {
        if (local.isEmpty()) { done(); return }
        MaterialAlertDialogBuilder(ctx).setTitle("📱 Знімки без Minerva: ${local.size}")
            .setMessage("Їх зроблено, поки телефон не був підключений до Minerva. Надіслати їх у Minerva" +
                (if (Prefs.who.isNotEmpty()) " (${Prefs.who})" else "") + "?")
            .setPositiveButton("⬆ Надіслати всі") { _, _ -> send(ctx, local.map { it.uuid }); done() }
            .setNeutralButton("☑ Вибрати…") { _, _ -> choose(ctx, local, done) }
            .setNegativeButton("Пізніше") { _, _ -> done() }
            .setCancelable(false).show()
    }

    private fun choose(ctx: Context, local: List<Capture>, done: () -> Unit) {
        val meta = Prefs.meta()
        val labels = local.map { c ->
            listOf(shortDate(c.createdAt), meta.kindLabel(c.kind), c.event, c.note.take(40).let { if (it.isEmpty()) "" else "📝 $it" },
                if (c.visibility == "private") "🔒" else "").filter { it.isNotEmpty() }.joinToString(" · ")
        }
        val checked = BooleanArray(local.size) { true }
        MaterialAlertDialogBuilder(ctx).setTitle("☑ Що надіслати в Minerva")
            .setMultiChoiceItems(labels.toTypedArray(), checked) { _, i, on -> checked[i] = on }
            .setPositiveButton("⬆ Надіслати") { _, _ ->
                send(ctx, local.filterIndexed { i, _ -> checked[i] }.map { it.uuid }); done()
            }
            .setNegativeButton("Пізніше") { _, _ -> done() }
            .setCancelable(false).show()
    }

    fun send(ctx: Context, uuids: List<String>) {
        if (uuids.isEmpty()) return
        CaptureStore.release(uuids)
        Sync.uploadNow(ctx)
        ctx.toast("⬆ Надсилаю в Minerva: ${uuids.size}")
    }
}
