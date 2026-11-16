package com.minervabi.cards

import android.content.Intent
import android.os.Bundle
import android.view.Gravity
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.LinearLayout
import androidx.core.content.ContextCompat
import androidx.core.widget.NestedScrollView
import androidx.fragment.app.Fragment
import androidx.lifecycle.lifecycleScope
import com.google.android.material.button.MaterialButton
import com.google.android.material.card.MaterialCardView
import com.google.android.material.chip.ChipGroup
import com.google.android.material.materialswitch.MaterialSwitch
import com.google.android.material.radiobutton.MaterialRadioButton
import kotlinx.coroutines.launch

/** ⚙ Підключення до Minerva, пресети, категорії, як знімати й надсилати, версія додатка. */
class SettingsFragment : Fragment() {
    private lateinit var root: LinearLayout

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, s: Bundle?): View {
        root = LinearLayout(requireContext()).apply { orientation = LinearLayout.VERTICAL; setPadding(0, 0, 0, requireContext().dp(24)) }
        return NestedScrollView(requireContext()).apply { addView(root) }
    }

    override fun onViewCreated(view: View, s: Bundle?) = build()

    override fun onHiddenChanged(hidden: Boolean) {
        super.onHiddenChanged(hidden)
        if (!hidden) {
            build()
            if (Prefs.paired) viewLifecycleOwner.lifecycleScope.launch { runCatching { Api.meta() }.onSuccess { build() } }
        }
    }

    override fun onResume() {
        super.onResume()
        if (!isHidden) build()
    }

    private fun card(title: String): LinearLayout {
        val ctx = requireContext()
        val box = LinearLayout(ctx).apply { orientation = LinearLayout.VERTICAL; setPadding(ctx.dp(16), ctx.dp(12), ctx.dp(16), ctx.dp(14)) }
        box.addView(ctx.label(title, 18f))
        root.addView(MaterialCardView(ctx).apply {
            layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
                .apply { setMargins(ctx.dp(12), ctx.dp(10), ctx.dp(12), 0) }
            addView(box)
        })
        return box
    }

    /** tonal — звичайна дія, outlined — другорядна, text — тиха / небезпечна. */
    private fun button(box: LinearLayout, text: String, kind: String = "tonal", onClick: () -> Unit): MaterialButton {
        val btn = requireContext().button(kind, text, onClick)
        btn.layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
            .apply { topMargin = requireContext().dp(6) }
        box.addView(btn)
        return btn
    }

    private fun switch(box: LinearLayout, text: String, value: Boolean, onChange: (Boolean) -> Unit) {
        box.addView(MaterialSwitch(requireContext()).apply {
            this.text = text
            isChecked = value
            setOnCheckedChangeListener { _, on -> onChange(on) }
        })
    }

    private fun build() {
        if (!::root.isInitialized || context == null) return
        val ctx = requireContext()
        val meta = Prefs.meta()
        val scope = viewLifecycleOwner.lifecycleScope
        root.removeAllViews()

        val acc = card("🔗 Minerva")
        if (Prefs.paired) {
            acc.addView(ctx.label("👤 ${Prefs.userName}${if (Prefs.userEmail.isNotEmpty()) " · ${Prefs.userEmail}" else ""}"))
            acc.addView(ctx.label("🌐 ${Prefs.server.removePrefix("https://")}", secondary = true))
            acc.addView(ctx.label("📱 ${Api.deviceName()}", secondary = true))
            if (Prefs.needsPairing) acc.addView(ctx.label("⚠️ Minerva не приймає ключ цього телефона (відключили?) — підключіть знову."))
            button(acc, "🌐 Візитки в Minerva (браузер)") { ctx.open("${Prefs.server}/admin/inventory/supplier/cards/") }
            button(acc, "🔗 Підключити знову (новий QR)", "outlined") { startActivity(Intent(ctx, PairActivity::class.java)) }
            button(acc, "🚪 Забути Minerva на цьому телефоні", "text") {
                ctx.confirm("Забути підключення?", "Ключ зникне з телефона. Фото, які ще не надіслано, лишаться — " +
                    "надішлемо після нового підключення. У Minerva телефон варто ще й «🚫 Відключити».", "🚪 Забути") {
                    Prefs.forget(); build()
                }
            }.setTextColor(ContextCompat.getColor(ctx, R.color.bad))
        } else {
            acc.addView(ctx.label("Ще не підключено. На комп'ютері: Minerva → Постачальники → 🪪 Візитки й флаєри → «📱 Додаток на телефон» → «🔗 Показати QR». Фотографувати можна вже зараз — надішлемо після підключення.", secondary = true))
            button(acc, "📷 Сканувати QR з Minerva", "filled") { startActivity(Intent(ctx, PairActivity::class.java)) }
        }

        val pre = card("📌 Пресети (виставка, зустріч)")
        pre.addView(ctx.label("Пресет підставляє «де взяли», позначки, категорії, примітку й AI у кожне нове фото. Ті самі, що в Minerva на комп'ютері.", 13f, true))
        // список з вибором (як радіокнопки): дотик — знімати з цим пресетом, ✏️ — змінити свій
        val current = Prefs.presetId ?: meta.defaultPreset ?: -1
        fun row(id: Int, title: String, sub: String, edit: (() -> Unit)?) {
            val line = LinearLayout(ctx).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
            val radio = MaterialRadioButton(ctx).apply {
                text = if (sub.isEmpty()) title else "$title\n$sub"
                isChecked = id == current
                layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
                setOnClickListener {
                    Prefs.presetId = id
                    meta.preset(id)?.let { Prefs.kind = it.kind }
                    ctx.toast(if (id > 0) "📌 Нові фото — з пресетом «$title»" else "Нові фото — без пресету")
                    build()
                }
            }
            line.addView(radio)
            if (edit != null) line.addView(ctx.button("text", "✏️") { edit() }.apply {
                layoutParams = LinearLayout.LayoutParams(ctx.dp(52), ViewGroup.LayoutParams.WRAP_CONTENT)
            })
            pre.addView(line)
        }
        meta.presets.forEach { p ->
            val bits = listOfNotNull(
                p.event.takeIf { it.isNotEmpty() && it != p.name }?.let { "🎪 $it" },
                "★ за замовчуванням".takeIf { meta.defaultPreset == p.id },
                when { p.shared && !p.mine -> "👥 ${p.owner}"; p.shared -> "👥 для всіх"; else -> null },
            )
            row(p.id, "📌 ${p.name}", bits.joinToString(" · "), if (p.mine) ({ Dialogs.preset(ctx, scope, p) { build() } }) else null)
        }
        row(-1, "— Без пресету", "", null)
        if (Prefs.paired && meta.perms.upload) button(pre, "＋ Новий пресет", "outlined") { Dialogs.preset(ctx, scope, null) { build() } }

        val cats = card("🗂 Категорії постачальників")
        cats.addView(ctx.label("Ті самі, що в Minerva. Нова тут — одразу і в Minerva.", 13f, true))
        val group = ChipGroup(ctx)
        meta.categories.forEach { c ->
            group.addAction(c.label) {
                if (meta.perms.catChange) Dialogs.category(ctx, scope, c) { build() }
                else ctx.alert(c.label, c.description.ifEmpty { "Без опису" })
            }
        }
        if (meta.perms.catAdd) group.addAction("＋ Нова категорія") { Dialogs.category(ctx, scope, null) { build() } }
        cats.addView(group)
        if (meta.categories.isEmpty()) cats.addView(ctx.label(if (Prefs.paired) "Оновлюю з Minerva…" else "З'являться після підключення.", 13f, true))

        val shoot = card("📷 Фото")
        switch(shoot, "📐 Візитку й флаєр обрізати по краях і вирівнювати (вимкнено — по рамці)", Prefs.edges) { Prefs.edges = it }
        switch(shoot, "Після лицьового боку пропонувати зворотний", Prefs.askBack) { Prefs.askBack = it }
        switch(shoot, "🧠 Читати AI за замовчуванням", Prefs.readAiDefault) { Prefs.readAiDefault = it }
        switch(shoot, "📶 Надсилати лише через Wi-Fi", Prefs.wifiOnly) {
            Prefs.wifiOnly = it; Sync.schedulePeriodic(ctx); Sync.uploadNow(ctx)
        }
        shoot.addView(ctx.label("Якість фото (довша сторона)", 14f))
        val q = ChipGroup(ctx).apply { isSingleSelection = true; isSelectionRequired = true }
        listOf(1600 to "1600 — швидко", 2400 to "2400 — звичайно", 3200 to "3200 — дрібний текст").forEach { (px, l) ->
            q.addToggle(l, Prefs.maxPx == px, px) { if (it) Prefs.maxPx = px }.isCheckedIconVisible = false
        }
        shoot.addView(q)
        if (meta.models.isNotEmpty()) {
            shoot.addView(ctx.label("🧠 Модель AI (якщо в пресеті не вибрано)", 14f))
            val m = ChipGroup(ctx).apply { isSingleSelection = true }
            val now = Prefs.model.ifEmpty { meta.defaultModel }
            meta.models.forEach { (k, l) -> m.addToggle(l.substringBefore(" —"), k == now, k) { if (it) Prefs.model = k }.isCheckedIconVisible = false }
            shoot.addView(m)
        }

        val about = card("ℹ️ Додаток")
        about.addView(ctx.label("Minerva Візитки ${BuildConfig.VERSION_NAME} · кнопка гучності — теж знімок · без інтернету фото чекають у «📤 Мої знімки».", 13f, true))
        button(about, "⬇ Остання версія додатка", "outlined") {
            ctx.open(meta.appUrl.ifEmpty { "https://github.com/Akustik13/minerva-bi/releases/latest" })
        }
    }
}
