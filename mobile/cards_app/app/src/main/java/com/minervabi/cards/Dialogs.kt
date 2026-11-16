package com.minervabi.cards

import android.content.Context
import android.widget.ArrayAdapter
import android.widget.AutoCompleteTextView
import android.widget.LinearLayout
import android.widget.ScrollView
import androidx.lifecycle.LifecycleCoroutineScope
import com.google.android.material.chip.ChipGroup
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.android.material.materialswitch.MaterialSwitch
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject

/** Діалоги: категорія (нова / правка), пресет (вибір, новий, правка, видалити, за замовчуванням). */
object Dialogs {
    val ICONS = listOf("🏭", "🟩", "🔧", "🔌", "📡", "🧩", "💎", "🔩", "📦", "🖨", "🔋", "🧪", "🏷", "🚚", "💡", "🧲",
        "🎛", "📻", "🛠", "⚙️")

    /** Нова категорія або правка: назва, значок, опис — одразу в Minerva (ті самі категорії, що в системі). */
    fun category(context: Context, scope: LifecycleCoroutineScope, cat: Category?, onDone: (Category) -> Unit) {
        val box = context.vbox()
        val (nameBox, name) = context.field("Назва (у множині, напр. «Виробники кабелів»)", cat?.name.orEmpty())
        box.addView(nameBox)
        box.addView(context.label("Значок"))
        val icons = ChipGroup(context).apply { isSingleSelection = true; isSelectionRequired = true }
        val chosen = cat?.icon?.ifEmpty { null } ?: "🏭"
        (listOf(chosen) + ICONS).distinct().forEach { icons.addToggle(it, it == chosen, it).isCheckedIconVisible = false }
        box.addView(icons)
        val (descBox, desc) = context.field("Опис — що роблять (допомагає AI вибирати категорію)", cat?.description.orEmpty(), true)
        box.addView(descBox)
        MaterialAlertDialogBuilder(context)
            .setTitle(if (cat == null) "🗂 Нова категорія" else "✏️ Категорія")
            .setView(ScrollView(context).apply { addView(box) })
            .setPositiveButton("💾 Зберегти") { _, _ ->
                val body = JSONObject().put("name", name.text.toString().trim())
                    .put("icon", icons.checkedTags().firstOrNull()?.toString() ?: "🏭")
                    .put("description", desc.text.toString().trim())
                scope.launch {
                    try {
                        val j = Api.saveCategory(cat?.id, body)
                        runCatching { Api.meta() }
                        val c = Category.parse(j.getJSONObject("category"))
                        context.toast(if (cat == null) "🗂 Категорію «${c.name}» додано в Minerva" else "💾 Збережено")
                        onDone(c)
                    } catch (e: ApiException) {
                        context.alert("Не збережено", if (e.offline) "Немає інтернету — категорії живуть у Minerva, спробуйте, коли буде зв'язок." else e.message.orEmpty())
                    }
                }
            }
            .setNegativeButton("Скасувати", null)
            .show()
    }

    /** Пресет: назва, де взяли, що це, позначки, категорії, примітка, AI, для всіх, за замовчуванням. */
    fun preset(context: Context, scope: LifecycleCoroutineScope, p: Preset?, onDone: (Int?) -> Unit) {
        val meta = Prefs.meta()
        val box = context.vbox()
        val (nameBox, name) = context.field("Назва, напр. «electronica 2026, хол B3»", p?.name.orEmpty())
        box.addView(nameBox)
        val (eventBox, event) = context.field("🎪 Де взяли — підставиться в кожне фото", p?.event.orEmpty())
        (event as? AutoCompleteTextView)?.setAdapter(ArrayAdapter(context, android.R.layout.simple_dropdown_item_1line, meta.events))
        box.addView(eventBox)
        box.addView(context.label("Що це"))
        val kinds = ChipGroup(context).apply { isSingleSelection = true; isSelectionRequired = true }
        meta.kinds.forEach { (k, l) -> kinds.addToggle(l, k == (p?.kind ?: "card"), k).isCheckedIconVisible = false }
        box.addView(kinds)
        box.addView(context.label("👥 Хто це для нас (напр. «наш стенд» — відвідувачі: 👤 клієнти)"))
        val roles = ChipGroup(context).apply { isSingleSelection = true }
        meta.roles.forEach { (k, l) -> roles.addToggle(l, k == (p?.role ?: ""), k).isCheckedIconVisible = false }
        box.addView(roles)
        box.addView(context.label("⭐ Позначки"))
        val marks = ChipGroup(context)
        meta.marks.forEach { (k, l) -> marks.addToggle(l, k in (p?.marks ?: listOf("fair")), k) }
        box.addView(marks)
        box.addView(context.label("🗂 Категорії"))
        val cats = ChipGroup(context)
        meta.categories.forEach { c -> cats.addToggle(c.label, c.id in (p?.categories ?: emptyList()), c.id) }
        box.addView(cats)
        val (noteBox, note) = context.field("📝 Примітка до кожного фото", p?.note.orEmpty(), true)
        box.addView(noteBox)
        val ai = MaterialSwitch(context).apply { text = "🧠 Читати AI"; isChecked = p?.readAi ?: true }
        val shared = MaterialSwitch(context).apply { text = "👥 Для всіх (колеги на тій самій виставці)"; isChecked = p?.shared ?: false }
        val def = MaterialSwitch(context).apply { text = "★ За замовчуванням"; isChecked = p == null || meta.defaultPreset == p.id }
        listOf(ai, shared, def).forEach { box.addView(it) }
        MaterialAlertDialogBuilder(context)
            .setTitle(if (p == null) "📌 Новий пресет" else "✏️ Пресет")
            .setView(ScrollView(context).apply { addView(box) })
            .setPositiveButton("💾 Зберегти") { _, _ ->
                val body = JSONObject().put("action", "save").put("name", name.text.toString().trim())
                    .put("event", event.text.toString().trim())
                    .put("kind", kinds.checkedTags().firstOrNull()?.toString() ?: "card")
                    .put("role", roles.checkedTags().firstOrNull()?.toString() ?: "")
                    .put("marks", JSONArray(marks.checkedTags())).put("categories", JSONArray(cats.checkedTags()))
                    .put("note", note.text.toString().trim()).put("read_ai", ai.isChecked).put("shared", shared.isChecked)
                    .put("make_default", def.isChecked).put("model", p?.model.orEmpty())
                if (p != null) body.put("id", p.id)
                save(context, scope, body, onDone)
            }
            .apply { if (p != null && p.mine) setNeutralButton("🗑 Видалити") { _, _ ->
                context.confirm("Видалити пресет?", "«${p.name}» зникне й у Minerva. Фото, зроблені з ним, лишаються.", "🗑 Видалити") {
                    save(context, scope, JSONObject().put("action", "delete").put("id", p.id)) { onDone(null) }
                }
            } }
            .setNegativeButton("Скасувати", null)
            .show()
    }

    private fun save(context: Context, scope: LifecycleCoroutineScope, body: JSONObject, onDone: (Int?) -> Unit) {
        scope.launch {
            try {
                val j = Api.savePreset(body)
                runCatching { Api.meta() }
                val id = if (j.isNull("id")) null else j.optInt("id")
                if (id != null) Prefs.presetId = id
                context.toast("📌 Збережено в Minerva")
                onDone(id)
            } catch (e: ApiException) {
                context.alert("Не збережено", if (e.offline) "Немає інтернету — пресети живуть у Minerva (їх бачать і на комп'ютері). Спробуйте, коли буде зв'язок." else e.message.orEmpty())
            }
        }
    }

    /** Вибір пресету для знімків: «без пресету», мої й спільні, ＋ новий, ✏️ правка вибраного. */
    fun choosePreset(context: Context, scope: LifecycleCoroutineScope, onDone: () -> Unit) {
        val meta = Prefs.meta()
        val current = Prefs.presetId ?: meta.defaultPreset ?: -1
        val items = mutableListOf("— Без пресету")
        val ids = mutableListOf(-1)
        meta.presets.forEach { p ->
            val star = if (meta.defaultPreset == p.id) "★ " else ""
            val who = when { p.shared && !p.mine -> " · 👥 ${p.owner}"; p.shared -> " · 👥"; else -> "" }
            items.add("📌 $star${p.name}${if (p.event.isNotEmpty() && p.event != p.name) " — ${p.event}" else ""}$who")
            ids.add(p.id)
        }
        val checked = ids.indexOf(current).coerceAtLeast(0)
        MaterialAlertDialogBuilder(context)
            .setTitle("📌 Пресет для нових фото")
            .setSingleChoiceItems(items.toTypedArray(), checked) { d, which ->
                Prefs.presetId = ids[which]
                meta.preset(ids[which])?.let { Prefs.kind = it.kind }
                d.dismiss(); onDone()
            }
            .setPositiveButton("＋ Новий пресет") { _, _ -> preset(context, scope, null) { onDone() } }
            .apply {
                val p = meta.preset(current)
                if (p != null && p.mine) setNeutralButton("✏️ Змінити «${p.name.take(18)}»") { _, _ -> preset(context, scope, p) { onDone() } }
            }
            .show()
    }

    fun row(context: Context): LinearLayout = LinearLayout(context).apply { orientation = LinearLayout.HORIZONTAL }
}
