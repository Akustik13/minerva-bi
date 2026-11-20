package com.minervabi.cards

import android.content.Intent
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.LinearLayout
import androidx.fragment.app.Fragment
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.minervabi.cards.databinding.FragmentListBinding
import com.minervabi.cards.databinding.ItemCardBinding
import kotlinx.coroutines.launch

/** 📤 Мої знімки: що чекає інтернету, що вже в Minerva (і що з ним зробив AI), помилки. */
class QueueFragment : Fragment() {
    private var _b: FragmentListBinding? = null
    private val b get() = _b!!
    private var filter = "all"
    private val segIds = mutableMapOf<String, Int>()
    private val adapter = Adapter()
    private val listener: () -> Unit = { render() }

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, s: Bundle?): View {
        _b = FragmentListBinding.inflate(inflater, container, false)
        return b.root
    }

    override fun onViewCreated(view: View, s: Bundle?) {
        b.title.text = "📤 Мої знімки"
        b.list.layoutManager = LinearLayoutManager(requireContext())
        b.list.adapter = adapter
        b.refresh.setOnRefreshListener { refresh() }
        val ctx = requireContext()
        // стан — чотири однакові кнопки в один рядок з лічильниками (нічого не гортається вбік)
        b.seg.show(true)
        listOf("all", "waiting", "done", "failed").forEach { k ->
            val btn = ctx.button("outlined", "") {}
            btn.id = View.generateViewId()
            btn.maxLines = 1
            btn.setPadding(ctx.dp(4), 0, ctx.dp(4), 0)
            btn.insetTop = 0; btn.insetBottom = 0
            btn.layoutParams = LinearLayout.LayoutParams(0, ctx.dp(44), 1f)
            segIds[k] = btn.id
            b.seg.addView(btn)
        }
        b.seg.check(segIds.getValue(filter))
        b.seg.addOnButtonCheckedListener { _, id, checked ->
            if (checked) { filter = segIds.entries.first { it.value == id }.key; render() }
        }
        b.actions.show(true)
        b.actions.addRowButton(ctx.button("tonal", "⬆ Надіслати зараз") {
            if (!Prefs.paired || Prefs.needsPairing) startActivity(Intent(ctx, PairActivity::class.java))
            else {
                Sync.uploadNow(ctx)
                val local = CaptureStore.local()
                if (local.isNotEmpty()) LocalSend.ask(ctx, local)    // зняті без підключення — спершу спитати
                else ctx.toast("⬆ Надсилаю, щойно буде інтернет")
            }
        }, false)
        b.actions.addRowButton(ctx.button("outlined", "🧹 Прибрати надіслані") {
            ctx.confirm("Прибрати надіслані з телефона?",
                "Фото, які вже в Minerva, зникнуть лише з телефона — у Minerva вони лишаються (вкладка «🪪 У Minerva»).",
                "🧹 Прибрати") { ctx.toast("Прибрано: ${CaptureStore.clearUploaded()}") }
        }, true)
        CaptureStore.listen(listener)
        render()
    }

    override fun onHiddenChanged(hidden: Boolean) {
        super.onHiddenChanged(hidden)
        if (!hidden) { render(); refresh(quiet = true) }
    }

    override fun onDestroyView() {
        CaptureStore.unlisten(listener)
        _b = null
        super.onDestroyView()
    }

    private fun refresh(quiet: Boolean = false) {
        if (!Prefs.paired) { _b?.refresh?.isRefreshing = false; return }
        Sync.uploadNow(requireContext())
        viewLifecycleOwner.lifecycleScope.launch {
            try {
                Sync.refreshStatuses()
            } catch (e: ApiException) {
                if (!quiet) context?.toast(if (e.offline) "Немає інтернету — покажемо, щойно буде" else e.message.orEmpty())
            }
            _b?.refresh?.isRefreshing = false
        }
    }

    private fun render() {
        val bb = _b ?: return
        val all = CaptureStore.mine()
        val c = CaptureStore.counts()
        bb.summary.text = when {
            !Prefs.paired -> "🔗 Minerva не підключено — знімки лише на телефоні. Підключіть («⚙ Налаштування → 📷 Сканувати QR») — тоді виберете, які надіслати."
            (c["local"] ?: 0) > 0 -> "📱 Лише на телефоні: ${c["local"]} — «⬆ Надіслати зараз», щоб вибрати, які надіслати в Minerva"
            Prefs.needsPairing -> "🔗 Телефон відключили в Minerva — підключіть знову QR-кодом."
            (c["reading"] ?: 0) > 0 -> "🧠 AI читає: ${c["reading"]} · потягніть донизу — оновити"
            else -> "Потягніть донизу — оновити стан з Minerva"
        }
        val labels = mapOf("all" to "Усі ${all.size}", "waiting" to "⏳ ${(c["waiting"] ?: 0) + (c["local"] ?: 0)}", "done" to "✅ ${c["done"]}",
            "failed" to "⚠️ ${c["failed"]}")
        segIds.forEach { (k, id) -> bb.seg.findViewById<android.widget.Button>(id)?.text = labels[k] }
        val items = all.filter {
            when (filter) {
                "waiting" -> it.waiting || it.local
                "done" -> it.status == Capture.UPLOADED
                "failed" -> it.failed
                else -> true
            }
        }
        adapter.items = items
        adapter.notifyDataSetChanged()
        bb.empty.show(items.isEmpty())
        bb.empty.text = if (all.isEmpty()) "Ще немає знімків — вкладка «📷 Знімати»." else "Тут нічого немає."
    }

    private fun statusText(c: Capture): String {
        val card = c.card
        return when (c.status) {
            Capture.DRAFT -> "✏️ Чернетка — торкніться, щоб дописати й зберегти"
            Capture.PENDING -> when {
                c.held -> "📱 Лише на телефоні — ще не надіслано в Minerva"
                c.error.isNotEmpty() -> "⏳ Чекає інтернету · ${c.error}"
                else -> "⏳ Чекає інтернету"
            }
            Capture.UPLOADING -> "⬆ Надсилається…"
            Capture.FAILED -> "⚠️ ${c.error.ifEmpty { "Minerva не прийняла" }} — торкніться, щоб виправити"
            else -> {
                val where = when {
                    card?.supplierName?.isNotEmpty() == true -> " · 🏭 ${card.supplierName}"
                    card?.customerName?.isNotEmpty() == true -> " · 👤 ${card.customerName}"
                    else -> ""
                }
                when {
                    c.pushError.isNotEmpty() -> "⚠️ Зміни фото не дійшли: ${c.pushError} — торкніться й утримуйте"
                    c.dirty -> "⏳ Зміни фото чекають інтернету · ✅ У Minerva$where"
                    else -> "✅ У Minerva · ${card?.aiText() ?: ""}$where"
                }
            }
        }
    }

    private fun options(c: Capture) {
        val labels = mutableListOf<String>()
        val acts = mutableListOf<() -> Unit>()
        if (c.status == Capture.UPLOADED && c.serverId > 0) {
            labels += "🪪 Відкрити в Minerva"; acts += { CardDetailActivity.open(requireContext(), c.serverId) }
        }
        labels += "✏️ Фото (знову, зворотний бік, ще фото), примітка, позначки"
        acts += { ReviewSheet.forEdit(c.uuid).show(childFragmentManager, "edit") }
        if (c.front.isNotEmpty()) { labels += "🖼 Фото на весь екран"; acts += { PhotoActivity.openFile(requireContext(), c.front) } }
        if (c.local) {
            labels += "⬆ Надіслати в Minerva"; acts += {
                if (!Prefs.paired) startActivity(Intent(requireContext(), PairActivity::class.java))
                else LocalSend.send(requireContext(), listOf(c.uuid))
            }
        }
        if (c.failed) {
            labels += "⬆ Надіслати ще раз"; acts += {
                CaptureStore.update(c.uuid) {
                    if (it.status == Capture.FAILED) it.status = Capture.PENDING
                    it.error = ""; it.pushError = ""
                }
                Sync.uploadNow(requireContext())
            }
        }
        labels += "🗑 Прибрати з телефона"; acts += {
            val text = when {
                c.dirty -> "Знімок у Minerva, але зміни фото ще не дійшли — вони зникнуть."
                c.status == Capture.UPLOADED -> "Фото лишиться в Minerva."
                else -> "Це фото ще НЕ в Minerva — воно зникне зовсім."
            }
            requireContext().confirm("Прибрати знімок?", text, "🗑 Прибрати") { CaptureStore.remove(c.uuid) }
        }
        MaterialAlertDialogBuilder(requireContext())
            .setItems(labels.toTypedArray()) { _, i -> acts[i]() }
            .show()
    }

    inner class Holder(val v: ItemCardBinding) : RecyclerView.ViewHolder(v.root)

    inner class Adapter : RecyclerView.Adapter<Holder>() {
        var items: List<Capture> = emptyList()
        override fun getItemCount() = items.size
        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int) =
            Holder(ItemCardBinding.inflate(LayoutInflater.from(parent.context), parent, false))

        override fun onBindViewHolder(h: Holder, pos: Int) {
            val c = items[pos]
            val card = c.card
            val meta = Prefs.meta()
            if (c.front.isNotEmpty() || c.serverId == 0) ImageLoader.local(viewLifecycleOwner.lifecycleScope, h.v.thumb, c.front)
            else ImageLoader.server(requireContext(), viewLifecycleOwner.lifecycleScope, h.v.thumb, c.serverId)
            val name = listOf(card?.company.orEmpty(), card?.person.orEmpty()).filter { it.isNotEmpty() }.joinToString(" · ")
            h.v.line1.text = name.ifEmpty { listOf(meta.kindLabel(c.kind), c.event).filter { it.isNotEmpty() }.joinToString(" · ") }
            h.v.line2.text = statusText(c)
            val bits = mutableListOf<String>()
            bits += shortDate(c.createdAt)
            meta.roleIcon(card?.role ?: c.role).takeIf { it.isNotEmpty() }?.let { bits += it }
            if (c.visibility == "private") bits += "🔒"
            if (c.hasBack) bits += "2 боки"
            if (c.extras.isNotEmpty()) bits += "📎 +${c.extras.size}"
            if (c.qrFront.isNotEmpty() || c.qrBack.isNotEmpty()) bits += "🔳 QR"
            c.marks.mapTo(bits) { meta.markLabel(it).take(2) }
            c.categories.mapNotNullTo(bits) { meta.category(it)?.icon }
            if (c.note.isNotEmpty()) bits += "📝 ${c.note.take(60)}"
            h.v.line3.text = bits.joinToString(" ")
            h.v.root.setOnClickListener {
                if (c.status == Capture.UPLOADED && c.serverId > 0 && c.pushError.isEmpty()) CardDetailActivity.open(requireContext(), c.serverId)
                else ReviewSheet.forEdit(c.uuid).show(childFragmentManager, "edit")
            }
            h.v.root.setOnLongClickListener { options(c); true }
        }
    }
}
