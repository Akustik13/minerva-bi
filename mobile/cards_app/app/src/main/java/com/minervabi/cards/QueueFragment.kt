package com.minervabi.cards

import android.content.Intent
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
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
        b.filters.isSingleSelection = true          // до чипів: зміна режиму знімає позначку
        b.filters.isSelectionRequired = true
        listOf("all" to "Усі", "waiting" to "⏳ Чекають", "done" to "✅ У Minerva", "failed" to "⚠️ Помилки").forEach { (k, l) ->
            b.filters.addToggle(l, k == filter, k) { on -> if (on) { filter = k; render() } }.isCheckedIconVisible = false
        }
        b.filters.addAction("⬆ Надіслати зараз") {
            if (!Prefs.paired || Prefs.needsPairing) startActivity(Intent(requireContext(), PairActivity::class.java))
            else { Sync.uploadNow(requireContext()); requireContext().toast("⬆ Надсилаю, щойно буде інтернет") }
        }
        b.filters.addAction("🧹 Прибрати надіслані") {
            requireContext().confirm("Прибрати надіслані з телефона?",
                "Фото, які вже в Minerva, зникнуть лише з телефона — у Minerva вони лишаються (вкладка «🪪 У Minerva»).",
                "🧹 Прибрати") { requireContext().toast("Прибрано: ${CaptureStore.clearUploaded()}") }
        }
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
        val all = CaptureStore.all()
        val c = CaptureStore.counts()
        bb.summary.text = buildString {
            if (!Prefs.paired) append("🔗 Minerva не підключено — фото чекають на телефоні. «⚙ Налаштування → Підключити».\n")
            else if (Prefs.needsPairing) append("🔗 Телефон відключили в Minerva — підключіть знову QR-кодом.\n")
            append("⏳ чекають: ${c["waiting"]} · ✅ у Minerva: ${c["done"]}")
            if ((c["reading"] ?: 0) > 0) append(" · 🧠 AI читає: ${c["reading"]}")
            if ((c["failed"] ?: 0) > 0) append(" · ⚠️ помилки: ${c["failed"]}")
        }
        val items = all.filter {
            when (filter) {
                "waiting" -> it.waiting
                "done" -> it.status == Capture.UPLOADED
                "failed" -> it.status == Capture.FAILED
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
            Capture.PENDING -> if (c.error.isNotEmpty()) "⏳ Чекає інтернету · ${c.error}" else "⏳ Чекає інтернету"
            Capture.UPLOADING -> "⬆ Надсилається…"
            Capture.FAILED -> "⚠️ ${c.error.ifEmpty { "Minerva не прийняла" }} — торкніться, щоб виправити"
            else -> {
                val where = when {
                    card?.supplierName?.isNotEmpty() == true -> " · 🏭 ${card.supplierName}"
                    card?.customerName?.isNotEmpty() == true -> " · 👤 ${card.customerName}"
                    else -> ""
                }
                "✅ У Minerva · ${card?.aiText() ?: ""}$where"
            }
        }
    }

    private fun options(c: Capture) {
        val labels = mutableListOf<String>()
        val acts = mutableListOf<() -> Unit>()
        if (c.status == Capture.UPLOADED && c.serverId > 0) {
            labels += "🪪 Відкрити в Minerva"; acts += { CardDetailActivity.open(requireContext(), c.serverId) }
        }
        labels += "✏️ Примітка, позначки, категорії"; acts += { ReviewSheet.forEdit(c.uuid).show(childFragmentManager, "edit") }
        labels += "🖼 Фото"; acts += { PhotoActivity.openFile(requireContext(), c.front) }
        if (c.status == Capture.FAILED) {
            labels += "⬆ Надіслати ще раз"; acts += {
                CaptureStore.update(c.uuid) { it.status = Capture.PENDING; it.error = "" }
                Sync.uploadNow(requireContext())
            }
        }
        labels += "🗑 Прибрати з телефона"; acts += {
            val text = if (c.status == Capture.UPLOADED) "Фото лишиться в Minerva." else "Це фото ще НЕ в Minerva — воно зникне зовсім."
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
            ImageLoader.local(viewLifecycleOwner.lifecycleScope, h.v.thumb, c.front)
            val name = listOf(card?.company.orEmpty(), card?.person.orEmpty()).filter { it.isNotEmpty() }.joinToString(" · ")
            h.v.line1.text = name.ifEmpty { listOf(meta.kindLabel(c.kind), c.event).filter { it.isNotEmpty() }.joinToString(" · ") }
            h.v.line2.text = statusText(c)
            val bits = mutableListOf<String>()
            bits += shortDate(c.createdAt)
            if (c.back.isNotEmpty()) bits += "2 боки"
            if (c.qrFront.isNotEmpty() || c.qrBack.isNotEmpty()) bits += "🔳 QR"
            c.marks.mapTo(bits) { meta.markLabel(it).take(2) }
            c.categories.mapNotNullTo(bits) { meta.category(it)?.icon }
            if (c.note.isNotEmpty()) bits += "📝 ${c.note.take(60)}"
            h.v.line3.text = bits.joinToString(" ")
            h.v.root.setOnClickListener {
                if (c.status == Capture.UPLOADED && c.serverId > 0) CardDetailActivity.open(requireContext(), c.serverId)
                else ReviewSheet.forEdit(c.uuid).show(childFragmentManager, "edit")
            }
            h.v.root.setOnLongClickListener { options(c); true }
        }
    }
}
