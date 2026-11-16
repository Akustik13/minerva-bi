package com.minervabi.cards

import android.os.Bundle
import android.view.KeyEvent
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.view.inputmethod.EditorInfo
import android.widget.LinearLayout
import androidx.core.widget.NestedScrollView
import androidx.fragment.app.Fragment
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.bottomsheet.BottomSheetBehavior
import com.google.android.material.bottomsheet.BottomSheetDialog
import com.google.android.material.chip.ChipGroup
import com.minervabi.cards.databinding.FragmentListBinding
import com.minervabi.cards.databinding.ItemCardBinding
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject

/**
 * 🪪 У Minerva: усі візитки й флаєри з бази (як «🪪 Візитки й флаєри» на комп'ютері) — пошук і «⚙ Фільтри»: вікно,
 * де всі категорії й «де взяли» видно одразу (чипи переносяться на новий рядок); вибрані фільтри — чипи з ✕ під
 * пошуком.
 */
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

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, s: Bundle?): View {
        _b = FragmentListBinding.inflate(inflater, container, false)
        return b.root
    }

    override fun onViewCreated(view: View, s: Bundle?) {
        b.title.text = "🪪 Візитки в Minerva"
        b.searchRow.show(true)
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
        b.searchBox.setEndIconOnClickListener { b.search.setText(""); load(1) }
        b.btnFilters.setOnClickListener { filterSheet() }
        renderFilters()
        fromCache()
        load(1)
    }

    private val aiLabels = listOf("" to "Усі", "none" to "🧠 Ще не прочитані", "failed" to "⚠️ AI не прочитав",
        "nocat" to "🗂 Без категорії")

    /** Вибрані фільтри — чипи з ✕ (дотик — змінити), кількість — на кнопці «⚙ Фільтри». */
    private fun renderFilters() {
        val bb = _b ?: return
        val meta = Prefs.meta()
        bb.filters.removeAllViews()
        fun active(text: String, clear: () -> Unit) {
            bb.filters.addAction(text) { filterSheet() }.apply {
                isCloseIconVisible = true
                setOnCloseIconClickListener { clear(); renderFilters(); load(1) }
            }
        }
        if (mine) active("👤 Мої") { mine = false }
        if (show == "new") active("🆕 Не внесені") { show = "" }
        if (ai.isNotEmpty()) active(aiLabels.first { it.first == ai }.second) { ai = "" }
        if (cat > 0) active(meta.category(cat)?.label ?: "🗂 категорія") { cat = 0 }
        if (event.isNotEmpty()) active(if (event == "-") "🎪 без події" else "🎪 ${event.take(28)}") { event = "" }
        val n = bb.filters.childCount
        bb.filters.show(n > 0)
        bb.btnFilters.text = if (n > 0) "⚙ Фільтри · $n" else "⚙ Фільтри"
    }

    /** Усі фільтри в одному вікні знизу: чипи переносяться на новий рядок — видно все, що є. */
    private fun filterSheet() {
        val ctx = requireContext()
        val meta = Prefs.meta()
        val box = ctx.vbox(16)
        fun section(title: String) = box.addView(ctx.label(title, 15f).apply { setPadding(0, ctx.dp(10), 0, 0) })

        section("Чиї й що")
        val who = ChipGroup(ctx)
        who.addToggle("👤 Лише мої", mine) { mine = it }
        who.addToggle("🆕 Ще не внесені в постачальники / CRM", show == "new") { show = if (it) "new" else "" }
        box.addView(who)

        section("🧠 AI")
        val aiGroup = ChipGroup(ctx).apply { isSingleSelection = true; isSelectionRequired = true }
        aiLabels.forEach { (k, l) -> aiGroup.addToggle(l, ai == k, k) { on -> if (on) ai = k }.isCheckedIconVisible = false }
        box.addView(aiGroup)

        if (meta.categories.isNotEmpty()) {
            section("🗂 Категорія")
            val cats = ChipGroup(ctx).apply { isSingleSelection = true; isSelectionRequired = true }
            cats.addToggle("Усі", cat == 0, 0) { on -> if (on) cat = 0 }.isCheckedIconVisible = false
            meta.categories.forEach { c -> cats.addToggle(c.label, cat == c.id, c.id) { on -> if (on) cat = c.id }.isCheckedIconVisible = false }
            box.addView(cats)
        }

        section("🎪 Де взяли")
        val events = ChipGroup(ctx).apply { isSingleSelection = true; isSelectionRequired = true }
        events.addToggle("Усі", event.isEmpty(), "") { on -> if (on) event = "" }.isCheckedIconVisible = false
        events.addToggle("— без події", event == "-", "-") { on -> if (on) event = "-" }.isCheckedIconVisible = false
        meta.events.forEach { e -> events.addToggle(e, event == e, e) { on -> if (on) event = e }.isCheckedIconVisible = false }
        box.addView(events)

        val dialog = BottomSheetDialog(ctx)
        val row = LinearLayout(ctx).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, ctx.dp(16), 0, ctx.dp(20))
        }
        row.addRowButton(ctx.button("outlined", "↺ Скинути все") {
            mine = false; show = ""; ai = ""; cat = 0; event = ""
            dialog.dismiss()
        }, false)
        row.addRowButton(ctx.button("filled", "✓ Показати") { dialog.dismiss() }, true)
        box.addView(row)
        dialog.setContentView(NestedScrollView(ctx).apply { addView(box) })
        dialog.behavior.state = BottomSheetBehavior.STATE_EXPANDED
        dialog.behavior.skipCollapsed = true
        dialog.setOnDismissListener { renderFilters(); load(1) }
        dialog.show()
    }

    override fun onHiddenChanged(hidden: Boolean) {
        super.onHiddenChanged(hidden)
        if (!hidden && items.isEmpty()) load(1)
    }

    override fun onDestroyView() {
        _b = null
        super.onDestroyView()
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
