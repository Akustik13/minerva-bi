package com.minervabi.cards

import android.os.Bundle
import android.view.KeyEvent
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.view.inputmethod.EditorInfo
import androidx.fragment.app.Fragment
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.chip.Chip
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.minervabi.cards.databinding.FragmentListBinding
import com.minervabi.cards.databinding.ItemCardBinding
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject

/** 🪪 У Minerva: усі візитки й флаєри з бази (як «🪪 Візитки й флаєри» на комп'ютері) — пошук і фільтри. */
class BrowseFragment : Fragment() {
    private var _b: FragmentListBinding? = null
    private val b get() = _b!!
    private val items = mutableListOf<ServerCard>()
    private val adapter = Adapter()
    private var page = 1
    private var more = false
    private var loading: Job? = null
    private var mine = false
    private var ai = ""
    private var show = ""
    private var cat = 0
    private var event = ""
    private lateinit var catChip: Chip
    private lateinit var eventChip: Chip

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, s: Bundle?): View {
        _b = FragmentListBinding.inflate(inflater, container, false)
        return b.root
    }

    override fun onViewCreated(view: View, s: Bundle?) {
        b.title.text = "🪪 Візитки в Minerva"
        b.searchBox.show(true)
        val lm = LinearLayoutManager(requireContext())
        b.list.layoutManager = lm
        b.list.adapter = adapter
        b.list.addOnScrollListener(object : RecyclerView.OnScrollListener() {
            override fun onScrolled(rv: RecyclerView, dx: Int, dy: Int) {
                if (more && loading?.isActive != true && lm.findLastVisibleItemPosition() >= items.size - 5) load(page + 1)
            }
        })
        b.refresh.setOnRefreshListener { load(1) }
        b.search.setOnEditorActionListener { _, id, e ->
            if (id == EditorInfo.IME_ACTION_SEARCH || e?.keyCode == KeyEvent.KEYCODE_ENTER) { load(1); true } else false
        }
        b.filters.addToggle("👤 Мої", false) { mine = it; load(1) }
        aiChip("🧠 Не прочитані", "none")
        aiChip("🗂 Без категорії", "nocat")
        aiChip("⚠️ AI не прочитав", "failed")
        b.filters.addToggle("🆕 Не внесені", false) { show = if (it) "new" else ""; load(1) }
        catChip = b.filters.addAction("🗂 Категорія ▾") { chooseCategory() }
        eventChip = b.filters.addAction("🎪 Де взяли ▾") { chooseEvent() }
        fromCache()
        load(1)
    }

    /** Стан AI — один з трьох (вибір іншого знімає попередній). */
    private fun aiChip(label: String, key: String) {
        b.filters.addToggle(label, false, key) { on ->
            if (on) { ai = key; uncheckOthers(key) } else if (ai == key) ai = ""
            load(1)
        }
    }

    private fun uncheckOthers(keep: String) {
        for (i in 0 until b.filters.childCount) {
            val c = b.filters.getChildAt(i) as? Chip ?: continue
            if (c.tag is String && c.tag != keep && c.isChecked && c.tag in setOf("none", "nocat", "failed")) c.isChecked = false
        }
    }

    override fun onHiddenChanged(hidden: Boolean) {
        super.onHiddenChanged(hidden)
        if (!hidden && items.isEmpty()) load(1)
    }

    override fun onDestroyView() {
        _b = null
        super.onDestroyView()
    }

    private fun chooseCategory() {
        val cats = Prefs.meta().categories
        val labels = listOf("Усі категорії") + cats.map { it.label }
        MaterialAlertDialogBuilder(requireContext()).setTitle("🗂 Категорія")
            .setItems(labels.toTypedArray()) { _, i ->
                cat = if (i == 0) 0 else cats[i - 1].id
                catChip.text = if (i == 0) "🗂 Категорія ▾" else "${cats[i - 1].label} ✕"
                load(1)
            }.show()
    }

    private fun chooseEvent() {
        val events = Prefs.meta().events
        val labels = listOf("Усі", "— без події") + events
        MaterialAlertDialogBuilder(requireContext()).setTitle("🎪 Де взяли")
            .setItems(labels.toTypedArray()) { _, i ->
                event = when (i) { 0 -> ""; 1 -> "-"; else -> events[i - 2] }
                eventChip.text = when (i) { 0 -> "🎪 Де взяли ▾"; 1 -> "🎪 без події ✕"; else -> "🎪 ${events[i - 2].take(24)} ✕" }
                load(1)
            }.show()
    }

    private fun params(p: Int) = mapOf(
        "q" to b.search.text?.toString()?.trim().orEmpty(), "mine" to (if (mine) "1" else ""), "ai" to ai,
        "show" to show, "cat" to (if (cat > 0) cat.toString() else ""), "event" to event, "page" to p.toString(),
    )

    private val firstPage get() = params(1).filterKeys { it != "page" }.values.all { it.isEmpty() }

    private fun fromCache() {
        val arr = runCatching { JSONArray(Prefs.browseJson) }.getOrNull() ?: return
        items.clear()
        for (i in 0 until arr.length()) items.add(ServerCard(arr.getJSONObject(i)))
        adapter.notifyDataSetChanged()
    }

    private fun load(p: Int) {
        if (!Prefs.paired) {
            _b?.refresh?.isRefreshing = false
            b.summary.text = "🔗 Підключіть Minerva («⚙ Налаштування»), щоб бачити візитки з бази."
            return
        }
        loading?.cancel()
        _b?.refresh?.isRefreshing = p == 1
        loading = viewLifecycleOwner.lifecycleScope.launch {
            try {
                val (list, hasMore) = Api.cards(params(p))
                if (p == 1) items.clear()
                items.addAll(list)
                page = p; more = hasMore
                if (p == 1 && firstPage) Prefs.browseJson = JSONArray(list.map { JSONObject(it.json.toString()) }).toString()
                _b?.summary?.text = if (items.isEmpty()) "" else "Показано ${items.size}${if (more) "+" else ""} · потягніть донизу — оновити"
            } catch (e: ApiException) {
                _b?.summary?.text = if (e.offline) "📴 Немає інтернету — показано збережене раніше" else "⚠️ ${e.message}"
            }
            adapter.notifyDataSetChanged()
            _b?.empty?.show(items.isEmpty())
            _b?.empty?.text = "Нічого не знайдено."
            _b?.refresh?.isRefreshing = false
        }
    }

    inner class Holder(val v: ItemCardBinding) : RecyclerView.ViewHolder(v.root)

    inner class Adapter : RecyclerView.Adapter<Holder>() {
        override fun getItemCount() = items.size
        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int) =
            Holder(ItemCardBinding.inflate(LayoutInflater.from(parent.context), parent, false))

        override fun onBindViewHolder(h: Holder, pos: Int) {
            val c = items[pos]
            val meta = Prefs.meta()
            ImageLoader.server(requireContext(), viewLifecycleOwner.lifecycleScope, h.v.thumb, c.id)
            h.v.line1.text = listOf(c.company, c.person).filter { it.isNotEmpty() }.joinToString(" · ")
                .ifEmpty { c.title.ifEmpty { meta.kindLabel(c.kind) } }
            val where = when {
                c.supplierName.isNotEmpty() -> "🏭 ${c.supplierName}"
                c.customerName.isNotEmpty() -> "👤 ${c.customerName}"
                else -> ""
            }
            h.v.line2.text = listOf("${c.flag} ${c.country}".trim(), c.aiText(), where).filter { it.isNotEmpty() }.joinToString(" · ")
            val cats = c.categories.mapNotNull { meta.category(it)?.label }
            h.v.line3.text = listOf(cats.joinToString(", "), c.event, c.desc.take(80)).filter { it.isNotEmpty() }.joinToString(" · ")
            h.v.root.setOnClickListener { CardDetailActivity.open(requireContext(), c.id) }
        }
    }
}
