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

/** Візитка з Minerva: фото, фірма, людина, контакти (📞 ✉️ 🌐 одним дотиком), AI, категорії, позначки, примітка. */
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
        // фото
        val photos = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        listOf("front" to true, "back" to c.hasBack).filter { it.second }.forEach { (side, _) ->
            val img = ImageView(this).apply {
                layoutParams = LinearLayout.LayoutParams(0, dp(150), 1f).apply { marginEnd = dp(6) }
                scaleType = ImageView.ScaleType.FIT_CENTER
                setOnClickListener { PhotoActivity.openServer(this@CardDetailActivity, id, side) }
            }
            ImageLoader.server(this, lifecycleScope, img, id, side)
            photos.addView(img)
        }
        body.addView(photos)
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
        if (c.newCategory.isNotEmpty()) body.addView(label("💡 AI пропонує нову категорію: ${c.newCategory} — додайте її на комп'ютері або тут ↓", 13f, true))
        if (c.notes.isNotEmpty()) body.addView(label("ℹ️ ${c.notes}", 13f, true))
        body.addView(label("🎪 ${c.event.ifEmpty { "—" }} · ${c.createdAt.take(10)}${if (c.by.isNotEmpty()) " · ${c.by}" else ""}", 13f, true))

        // що можна змінити
        val editable = c.editable
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

        val actions = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(0, dp(12), 0, dp(24)) }
        fun add(btn: MaterialButton) {
            btn.layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(54)).apply { topMargin = dp(6) }
            actions.addView(btn)
        }
        if (editable) add(button("💾 Зберегти", tonal = false) {
            val body2 = JSONObject().put("note", note.text.toString()).put("event", event.text.toString())
                .put("marks", JSONArray(marks.checkedTags())).put("categories", JSONArray(cats.checkedTags()))
            push(body2, "💾 Збережено")
        })
        if (meta.perms.upload) add(button(if (c.ai == "ai") "🧠 Прочитати AI ще раз" else "🧠 Прочитати AI") {
            lifecycleScope.launch {
                try {
                    render(Api.readCard(id, Prefs.model)); toast("🧠 AI читає — за хвилину тут будуть дані"); watch()
                } catch (e: ApiException) { alert("AI", e.message.orEmpty()) }
            }
        })
        if (editable && c.supplierName.isEmpty()) add(button("🏭 У постачальники") {
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
        add(button("🌐 Відкрити в Minerva") { open("${Prefs.server}/admin/inventory/supplier/cards/$id/") })
        if (editable) add(button("🗑 Видалити з Minerva") {
            confirm("Видалити візитку?", "Фото й примітки зникнуть з Minerva. Постачальник / клієнт, створені з неї, лишаються.", "🗑 Видалити") {
                lifecycleScope.launch {
                    try {
                        Api.deleteCard(id)
                        CaptureStore.all().filter { it.serverId == id }.forEach { CaptureStore.remove(it.uuid) }
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
                CaptureStore.all().filter { it.serverId == id }.forEach { cap -> CaptureStore.update(cap.uuid) { it.server = c.json } }
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
