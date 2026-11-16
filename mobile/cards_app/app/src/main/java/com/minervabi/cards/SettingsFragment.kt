package com.minervabi.cards

import android.content.Intent
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.LinearLayout
import androidx.core.widget.NestedScrollView
import androidx.fragment.app.Fragment
import androidx.lifecycle.lifecycleScope
import com.google.android.material.button.MaterialButton
import com.google.android.material.card.MaterialCardView
import com.google.android.material.chip.ChipGroup
import com.google.android.material.materialswitch.MaterialSwitch
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

    private fun button(box: LinearLayout, text: String, onClick: () -> Unit) {
        box.addView(MaterialButton(requireContext()).apply {
            this.text = text
            layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, requireContext().dp(52))
                .apply { topMargin = requireContext().dp(8) }
            setOnClickListener { onClick() }
        })
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
            button(acc, "🔗 Підключити знову (новий QR)") { startActivity(Intent(ctx, PairActivity::class.java)) }
            button(acc, "🌐 Візитки в Minerva (браузер)") { ctx.open("${Prefs.server}/admin/inventory/supplier/cards/") }
            button(acc, "🚪 Забути Minerva на цьому телефоні") {
                ctx.confirm("Забути підключення?", "Ключ зникне з телефона. Фото, які ще не надіслано, лишаться — " +
                    "надішлемо після нового підключення. У Minerva телефон варто ще й «🚫 Відключити».", "🚪 Забути") {
                    Prefs.forget(); build()
                }
            }
        } else {
            acc.addView(ctx.label("Ще не підключено. На комп'ютері: Minerva → Постачальники → 🪪 Візитки й флаєри → «📱 Додаток на телефон» → «🔗 Показати QR». Фотографувати можна вже зараз — надішлемо після підключення.", secondary = true))
            button(acc, "📷 Сканувати QR з Minerva") { startActivity(Intent(ctx, PairActivity::class.java)) }
        }

        val pre = card("📌 Пресети (виставка, зустріч)")
        pre.addView(ctx.label("Пресет підставляє «де взяли», позначки, категорії, примітку й AI у кожне нове фото. Ті самі, що в Minerva на комп'ютері.", 13f, true))
        val current = Prefs.presetId ?: meta.defaultPreset
        meta.presets.forEach { p ->
            val mark = when { p.id == current -> "✅ "; meta.defaultPreset == p.id -> "★ "; else -> "📌 " }
            val who = if (p.shared && !p.mine) " · 👥 ${p.owner}" else if (p.shared) " · 👥" else ""
            button(pre, "$mark${p.name}$who") {
                Prefs.presetId = p.id; Prefs.kind = p.kind
                ctx.toast("📌 Нові фото — з пресетом «${p.name}»")
                if (p.mine) Dialogs.preset(ctx, scope, p) { build() } else build()
            }
        }
        if (Prefs.paired && meta.perms.upload) button(pre, "＋ Новий пресет") { Dialogs.preset(ctx, scope, null) { build() } }
        if (current != null && current > 0) button(pre, "— Знімати без пресету") { Prefs.presetId = -1; build() }

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
        button(about, "⬇ Остання версія додатка") {
            ctx.open(meta.appUrl.ifEmpty { "https://github.com/Akustik13/minerva-bi/releases/latest" })
        }
    }
}
