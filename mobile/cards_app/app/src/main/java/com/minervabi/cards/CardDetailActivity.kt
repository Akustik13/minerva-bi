package com.minervabi.cards

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.view.ViewGroup
import android.widget.ImageView
import android.widget.LinearLayout
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.google.android.material.button.MaterialButton
import com.google.android.material.chip.ChipGroup
import com.minervabi.cards.databinding.ActivityCardDetailBinding
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject

/**
 * Візитка з Minerva: фото групи (боки й 📎 ще фото), фірма, людина, ще люди з інших візиток, контакти (📞 ✉️ 🌐 одним
 * дотиком), AI, категорії, позначки, примітка; «📷 Фото…» — перезняти, дознімати зворотний бік, ще фото.
 */
class CardDetailActivity : AppCompatActivity() {
    private lateinit var b: ActivityCardDetailBinding
    private var id = 0
    private var card: ServerCard? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivityCardDetailBinding.inflate(layoutInflater)
        setContentView(b.root)
        id = intent.getIntExtra("id", 0)
        b.toolbar.setNavigationIcon(androidx.appcompat.R.drawable.abc_ic_ab_back_material)
        b.toolbar.setNavigationOnClickListener { finish() }
        load()
    }

    private fun load(poll: Boolean = false) {
        lifecycleScope.launch {
            try {
                val c = Api.card(id)
                card = c
                render(c)
                if (c.reading && !poll) watch()
            } catch (e: ApiException) {
                b.body.removeAllViews()
                b.body.addView(label(if (e.offline) "📴 Немає інтернету — відкрийте, коли буде зв'язок." else "⚠️ ${e.message}"))
            }
        }
    }

    /** AI читає — перепитувати кожні 4 с, поки не прочитає (до 2 хв). */
    private fun watch() {
        lifecycleScope.launch {
            repeat(30) {
                delay(4000)
                val c = runCatching { Api.card(id) }.getOrNull() ?: return@launch
                card = c
                render(c)
                if (!c.reading) return@launch
            }
        }
    }

    private fun button(text: String, tonal: Boolean = true, onClick: () -> Unit) =
        MaterialButton(this, null, if (tonal) com.google.android.material.R.attr.materialButtonOutlinedStyle
        else com.google.android.material.R.attr.materialButtonStyle).apply {
            this.text = text
            setOnClickListener { onClick() }
        }

    private fun render(c: ServerCard) {
        val meta = Prefs.meta()
        b.toolbar.title = c.company.ifEmpty { c.title.ifEmpty { meta.kindLabel(c.kind) } }
        val body = b.body
        body.removeAllViews()
        // фото: боки, потім ще фото групи (по три в ряд)
        val sides = listOf("front" to "", "back" to "").filter { it.first == "front" || c.hasBack }.map { it.first } +
            c.photos.map { "p${it.id}" }
        sides.chunked(if (sides.size <= 2) 2 else 3).forEachIndexed { n, chunk ->
            val photos = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
            chunk.forEach { side ->
                val img = ImageView(this).apply {
                    layoutParams = LinearLayout.LayoutParams(0, dp(if (n == 0 && sides.size <= 2) 150 else 100), 1f)
                        .apply { marginEnd = dp(6); bottomMargin = dp(6) }
                    scaleType = ImageView.ScaleType.FIT_CENTER
                    contentDescription = when (side) {
                        "front" -> "Лицьовий бік"
                        "back" -> "Зворотний бік"
                        else -> c.photos.firstOrNull { "p${it.id}" == side }?.label ?: "Фото"
                    }
                    setOnClickListener { PhotoActivity.openServer(this@CardDetailActivity, id, side) }
                }
                ImageLoader.server(this, lifecycleScope, img, id, side)
                photos.addView(img)
            }
            repeat((if (sides.size <= 2) 2 else 3) - chunk.size) {
                photos.addView(android.view.View(this), LinearLayout.LayoutParams(0, 1, 1f))
            }
            body.addView(photos)
        }
        if (c.photos.isNotEmpty()) {
            body.addView(label("📎 Ще фото: " + c.photos.joinToString(", ") { it.label }, 13f, true))
        }
        if (c.editable) {
            body.addView(button("📷 Фото: знову, зворотний бік, ще фото (візитка, флаєр…)") {
                val cap = CaptureStore.adopt(c)
                MainActivity.review(this, cap.uuid)
                finish()
            }.apply {
                layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
                maxLines = 2
            })
        }
        // хто
        val who = listOf(c.person, c.position).filter { it.isNotEmpty() }.joinToString(" · ")
        if (who.isNotEmpty()) body.addView(label("👤 $who", 17f))
        if (c.country.isNotEmpty()) body.addView(label("${c.flag} ${c.country}"))
        body.addView(label(c.aiText() + if (c.error.isNotEmpty() && c.ai == "failed") " — ${c.error}" else "", 14f, true))
        if (c.supplierName.isNotEmpty()) body.addView(label("🏭 У постачальниках: ${c.supplierName}"))
        if (c.customerName.isNotEmpty()) body.addView(label("👤 Клієнт CRM: ${c.customerName}"))
        // контакти одним дотиком
        val contacts = ChipGroup(this)
        if (c.phone.isNotEmpty()) contacts.addAction("📞 ${c.phone}") { go(Intent(Intent.ACTION_DIAL, Uri.parse("tel:" + c.phone))) }
        if (c.mobile.isNotEmpty()) contacts.addAction("📱 ${c.mobile}") { go(Intent(Intent.ACTION_DIAL, Uri.parse("tel:" + c.mobile))) }
        if (c.email.isNotEmpty()) contacts.addAction("✉️ ${c.email}") { go(Intent(Intent.ACTION_SENDTO, Uri.parse("mailto:" + c.email))) }
        if (c.site.isNotEmpty()) contacts.addAction("🌐 ${c.site.removePrefix("https://").removePrefix("http://").take(32)}") { open(c.site) }
        if (contacts.childCount > 0) body.addView(contacts)
        if (c.address.isNotEmpty()) body.addView(label("📍 ${c.address}", 14f, true))
        if (c.desc.isNotEmpty()) body.addView(label("🏭 ${c.desc}", 14f))
        if (c.people.isNotEmpty()) {                     // люди з інших візиток групи
            body.addView(label("👥 Ще люди з цієї фірми", 14f))
            c.people.forEach { p ->
                val chips = ChipGroup(this)
                chips.addAction("👤 ${p.text}") {
                    when {
                        p.email.isNotEmpty() -> go(Intent(Intent.ACTION_SENDTO, Uri.parse("mailto:" + p.email)))
                        p.mobile.isNotEmpty() || p.phone.isNotEmpty() ->
                            go(Intent(Intent.ACTION_DIAL, Uri.parse("tel:" + p.mobile.ifEmpty { p.phone })))
                    }
                }
                body.addView(chips)
            }
        }
        if (c.newCategory.isNotEmpty()) body.addView(label("💡 AI пропонує нову категорію: ${c.newCategory} — додайте її на комп'ютері або тут ↓", 13f, true))
        if (c.notes.isNotEmpty()) body.addView(label("ℹ️ ${c.notes}", 13f, true))
        body.addView(label("🎪 ${c.event.ifEmpty { "—" }} · ${c.createdAt.take(10)}", 13f, true))
        if (c.whoText.isNotEmpty()) body.addView(label("👤 Зняв: ${c.whoText}", 13f, true))   // хто (роль) і звідки

        // що можна змінити
        val editable = c.editable
        body.addView(label("👥 Хто це для нас" + if (c.roleAi && c.role.isNotEmpty()) " (підказав AI)" else "", 14f))
        val roles = ChipGroup(this).apply { isSingleSelection = true }
        meta.roles.forEach { (k, l) -> roles.addToggle(l, k == c.role, k).apply { isCheckedIconVisible = false; isEnabled = editable } }
        body.addView(roles)
        if (c.roleAi && c.roleWhy.isNotEmpty()) body.addView(label("🧠 ${c.roleWhy}", 13f, true))
        body.addView(label("⭐ Позначки", 14f))
        val marks = ChipGroup(this)
        meta.marks.forEach { (k, l) -> marks.addToggle(l, k in c.marks, k).isEnabled = editable }
        body.addView(marks)
        body.addView(label("🗂 Категорії" + if (c.catsGuessed) " (підказав AI)" else "", 14f))
        val cats = ChipGroup(this)
        meta.categories.forEach { cat -> cats.addToggle(cat.label, cat.id in c.categories, cat.id).isEnabled = editable }
        if (editable && meta.perms.catAdd) cats.addAction("＋ Нова") {
            Dialogs.category(this, lifecycleScope, null) { created ->
                card?.let { save(it, extraCat = created.id) }
            }
        }
        body.addView(cats)
        val (noteBox, note) = field("📝 Примітка", c.note, multi = true)
        note.isEnabled = editable
        body.addView(noteBox)
        val (eventBox, event) = field("🎪 Де взяли", c.event)
        event.isEnabled = editable
        body.addView(eventBox)
        // 👁 хто бачить: змінює лише той, хто зняв
        body.addView(label("👁 Хто бачить у Minerva" + if (!c.canSetVisibility) " (змінює лише той, хто зняв)" else "", 14f))
        val vis = ChipGroup(this).apply { isSingleSelection = true; isSelectionRequired = true }
        meta.visibility.forEach { (k, l) ->
            vis.addToggle(l, k == c.visibility, k).apply { isCheckedIconVisible = false; isEnabled = c.canSetVisibility }
        }
        body.addView(vis)

        val actions = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(0, dp(12), 0, dp(24)) }
        fun add(btn: MaterialButton) {
            btn.layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(54)).apply { topMargin = dp(6) }
            actions.addView(btn)
        }
        if (editable || c.canSetVisibility) add(button("💾 Зберегти", tonal = false) {
            val body2 = JSONObject().put("note", note.text.toString()).put("event", event.text.toString())
                .put("marks", JSONArray(marks.checkedTags())).put("categories", JSONArray(cats.checkedTags()))
            val role = roles.checkedTags().firstOrNull()?.toString() ?: ""
            if (role != c.role) body2.put("role", role)
            val v = vis.checkedTags().firstOrNull()?.toString() ?: c.visibility
            if (c.canSetVisibility && v != c.visibility) body2.put("visibility", v)
            push(body2, "💾 Збережено")
        })
        if (meta.perms.upload) add(button(if (c.ai == "ai") "🧠 Прочитати AI ще раз" else "🧠 Прочитати AI") {
            lifecycleScope.launch {
                try {
                    render(Api.readCard(id, Prefs.model)); toast("🧠 AI читає — за хвилину тут будуть дані"); watch()
                } catch (e: ApiException) { alert("AI", e.message.orEmpty()) }
            }
        })
        val crm = editable && c.customerName.isEmpty() && meta.perms.crm
        val toCrm = {
            lifecycleScope.launch {
                try {
                    val r = Api.toCustomer(id)
                    val n = r.optJSONObject("customer")?.str("name").orEmpty()
                    val skipped = r.optJSONArray("skipped").strings()
                    when {
                        r.optBoolean("created") -> toast("👤 Новий клієнт CRM «$n»")
                        r.optBoolean("attached") -> toast("➕ Додано до клієнта «$n»")
                        skipped.isNotEmpty() -> alert("Не внесено", skipped.joinToString("\n"))
                    }
                    load()
                } catch (e: ApiException) { alert("Не внесено", e.message.orEmpty()) }
            }
            Unit
        }
        if (crm && c.role == "customer") add(button("👤 У CRM (клієнт)", tonal = false) { toCrm() })   // клієнт — першим
        if (editable && c.supplierName.isEmpty() && c.role != "customer") add(button("🏭 У постачальники") {
            lifecycleScope.launch {
                try {
                    val r = Api.toSupplier(id)
                    val s = r.optJSONObject("supplier")?.str("name").orEmpty()
                    val skipped = r.optJSONArray("skipped").strings()
                    when {
                        r.optBoolean("created") -> toast("🏭 Створено постачальника «$s»")
                        r.optBoolean("attached") -> toast("➕ Додано до «$s»")
                        skipped.isNotEmpty() -> alert("Не внесено", skipped.joinToString("\n"))
                    }
                    load()
                } catch (e: ApiException) { alert("Не внесено", e.message.orEmpty()) }
            }
        })
        if (crm && c.role != "customer") add(button("👤 У CRM") { toCrm() })
        add(button("🌐 Відкрити в Minerva") { open("${Prefs.server}/admin/inventory/supplier/cards/$id/") })
        if (editable) add(button("🗑 Видалити з Minerva") {
            confirm("Видалити візитку?", "Фото й примітки зникнуть з Minerva. Постачальник / клієнт, створені з неї, лишаються.", "🗑 Видалити") {
                lifecycleScope.launch {
                    try {
                        Api.deleteCard(id)
                        CaptureStore.all().filter { it.serverId == id }.forEach { CaptureStore.remove(it.uuid) }
                        ImageLoader.forgetServer(this@CardDetailActivity, id)
                        toast("🗑 Видалено"); finish()
                    } catch (e: ApiException) { alert("Не видалено", e.message.orEmpty()) }
                }
            }
        })
        body.addView(actions)
    }

    private fun save(c: ServerCard, extraCat: Int) {
        push(JSONObject().put("categories", JSONArray((c.categories + extraCat).distinct())), "🗂 Додано")
    }

    private fun push(json: JSONObject, ok: String) {
        lifecycleScope.launch {
            try {
                val c = Api.updateCard(id, json)
                card = c
                CaptureStore.all().filter { it.serverId == id }.forEach { cap ->
                    CaptureStore.update(cap.uuid) { CaptureStore.mergeServer(it, c.json) }
                }
                toast(ok)
                render(c)
            } catch (e: ApiException) {
                alert("Не збережено", if (e.offline) "Немає інтернету — спробуйте, коли буде зв'язок." else e.message.orEmpty())
            }
        }
    }

    private fun go(i: Intent) {
        runCatching { startActivity(i) }.onFailure { toast("Немає чим відкрити") }
    }

    companion object {
        fun open(context: Context, id: Int) {
            context.startActivity(Intent(context, CardDetailActivity::class.java).putExtra("id", id))
        }
    }
}
