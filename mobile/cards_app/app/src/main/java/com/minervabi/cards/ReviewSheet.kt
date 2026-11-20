package com.minervabi.cards

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.speech.RecognizerIntent
import android.view.Gravity
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.ArrayAdapter
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.os.bundleOf
import androidx.fragment.app.setFragmentResult
import androidx.lifecycle.lifecycleScope
import com.google.android.material.bottomsheet.BottomSheetBehavior
import com.google.android.material.bottomsheet.BottomSheetDialog
import com.google.android.material.bottomsheet.BottomSheetDialogFragment
import com.google.android.material.button.MaterialButton
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.minervabi.cards.databinding.SheetReviewBinding
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * Вікно знімка: фото (лицьовий / зворотний бік — 📷 знову, ↻, 🗑, ＋ зворотний бік пізніше; 📎 ще фото групи — друга
 * візитка тієї самої фірми, флаєр, каталог, продукт, стенд), примітка (🎤 голосом), де взяли, позначки, категорії
 * (＋ нова), що це, AI. Щойно знятий знімок — чернетка (Capture.DRAFT): «💾 Зберегти й наступну» / «✓ Зберегти» /
 * «✕ Не зберігати»; закрили жестом «назад» чи потягнули вниз — збережено. Правка (з «📤 Мої знімки» / картки) —
 * «💾 Зберегти»; зміни фото надісланого знімка UploadWorker дошле в Minerva. Зняти фото — камера (ShotTarget),
 * потім знову це вікно. 📐 Знімок «по краях» має й версію «по рамці» — перемикач вибирає, яку лишити.
 * Результат — setFragmentResult("review", {action: next | save | cancel, uuid}).
 */
class ReviewSheet : BottomSheetDialogFragment() {
    private var _b: SheetReviewBinding? = null
    private val b get() = _b!!
    private lateinit var meta: Meta
    private var uuid = ""
    private var edges = true
    private var done = false
    private var leaving = false          // пішли знімати — не зберігати як «закрили»
    private var initialRole = ""
    private var initialVis = "team"

    private val speech = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { r ->
        if (r.resultCode == Activity.RESULT_OK) {
            val text = r.data?.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)?.firstOrNull().orEmpty()
            if (text.isNotEmpty()) {
                val now = b.note.text?.toString().orEmpty()
                b.note.setText(if (now.isBlank()) text else "$now $text")
                b.note.setSelection(b.note.text?.length ?: 0)
            }
        }
    }

    private fun cap(): Capture? = CaptureStore.get(uuid)

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, s: Bundle?): View {
        _b = SheetReviewBinding.inflate(inflater, container, false)
        return b.root
    }

    override fun onStart() {
        super.onStart()
        (dialog as? BottomSheetDialog)?.behavior?.apply {
            state = BottomSheetBehavior.STATE_EXPANDED
            skipCollapsed = true
        }
        dialog?.setCanceledOnTouchOutside(false)       // випадковий дотик поза вікном не прибирає фото
    }

    /** «Назад» або потягнули вниз: нове фото — зберегти (його ж і знімали), правка — без змін полів. */
    override fun onCancel(dialog: android.content.DialogInterface) {
        super.onCancel(dialog)
        if (done || leaving || _b == null) return
        if (cap()?.isDraft == true) save("next", close = false)
        else {
            done = true
            Sync.uploadNow(requireContext())           // зміни фото — у Minerva
            setFragmentResult("review", bundleOf("action" to "cancel", "uuid" to uuid))
        }
    }

    override fun onViewCreated(view: View, s: Bundle?) {
        meta = Prefs.meta()
        uuid = requireArguments().getString("uuid").orEmpty()
        val c = cap() ?: run { dismissAllowingStateLoss(); return }
        val draft = c.isDraft
        b.edges.setOnCheckedChangeListener { _, on ->
            edges = on
            b.edges.text = if (on) "📐 Обрізано по краях і вирівняно (вимкніть — по рамці)" else "▭ Обрізано по рамці (увімкніть — по краях)"
            renderPhotos()
        }
        renderPhotos()

        b.note.setText(c.note)
        b.event.setText(c.event)
        val events = (meta.events + meta.presets.map { it.event } +
            CaptureStore.all().map { it.event }).filter { it.isNotBlank() }.distinct()
        b.event.setAdapter(ArrayAdapter(requireContext(), android.R.layout.simple_dropdown_item_1line, events))
        b.noteBox.setEndIconOnClickListener { listen() }

        // 👥 хто це: що в Minerva (AI міг підказати) → знімок (пресет «наш стенд» — клієнти)
        initialRole = c.card?.role ?: c.role
        val aiHint = if (c.card?.roleAi == true && initialRole.isNotEmpty())
            "🧠 Підказав AI" + (c.card?.roleWhy?.takeIf { it.isNotEmpty() }?.let { ": $it" } ?: "") + ". Не так — виберіть інше."
        else null
        // підказка — за тим, що вибрано зараз (раніше «Не вибрано…» стояло й під вибраним з пресету)
        fun roleHint() {
            val sel = b.roles.checkedTags().filterIsInstance<String>().firstOrNull().orEmpty()
            b.roleHint.text = when {
                sel.isEmpty() -> "Не вибрано — AI підкаже з візитки. 👤 Клієнт — потім «👤 У CRM»."
                sel == initialRole && aiHint != null -> aiHint
                sel == "customer" -> "👤 Клієнт — у Minerva потім «👤 У CRM». Зніміть вибір — AI підкаже з візитки."
                else -> "Зніміть вибір — AI підкаже з візитки."
            }
        }
        meta.roles.forEach { (k, label) ->
            b.roles.addToggle(label, k == initialRole, k) { roleHint() }.isCheckedIconVisible = false
        }
        roleHint()
        // 👁 хто бачить у Minerva: свій знімок — вибір; чужа візитка з «🪪 У Minerva» — не змінити
        val canVis = c.status != Capture.UPLOADED || (c.card?.canSetVisibility ?: !c.adopted)
        val vis = c.card?.visibility?.takeIf { c.status == Capture.UPLOADED } ?: c.visibility
        initialVis = vis
        meta.visibility.forEach { (k, label) ->
            b.vis.addToggle(label, k == vis, k).apply { isCheckedIconVisible = false; isEnabled = canVis }
        }
        b.visTitle.text = if (canVis) "👁 Хто бачить у Minerva" else "👁 Хто бачить у Minerva (змінює лише той, хто зняв)"
        meta.marks.forEach { (k, label) -> b.marks.addToggle(label, k in c.marks, k) }
        fillCategories(c.categories)
        meta.kinds.forEach { (k, label) -> b.kinds.addToggle(label, k == c.kind, k).isCheckedIconVisible = false }
        b.readAi.isChecked = c.readAi
        if (c.status == Capture.UPLOADED) b.readAi.show(false)        // уже в Minerva: AI — у картці візитки
        if (!draft) {
            b.btnSaveNext.text = "💾 Зберегти"
            b.btnSave.show(false)
            b.btnCancel.text = "Закрити"
        }
        b.btnSaveNext.setOnClickListener { save(if (draft) "next" else "save") }
        b.btnSave.setOnClickListener { save("save") }
        b.btnCancel.setOnClickListener {
            done = true
            if (cap()?.isDraft == true) CaptureStore.remove(uuid) else Sync.uploadNow(requireContext())
            setFragmentResult("review", bundleOf("action" to "cancel", "uuid" to uuid))
            dismiss()
        }
    }

    // ---------------------------------------------------------------- фото

    private fun ctx() = requireContext()

    private fun local(path: String) = path.isNotEmpty() && File(path).exists()

    private fun shownFront(c: Capture) = if (!edges && c.frontAlt.isNotEmpty()) c.frontAlt else c.front
    private fun shownBack(c: Capture) = if (!edges && c.backAlt.isNotEmpty()) c.backAlt else c.back

    /** Фото знімка: свій файл або (надісланий, відкритий з «🪪 У Minerva») — з Minerva. */
    private fun showImage(img: ImageView, path: String, serverSide: String?, c: Capture) {
        if (local(path)) {
            ImageLoader.local(viewLifecycleOwner.lifecycleScope, img, path)
            img.setOnClickListener { PhotoActivity.openFile(ctx(), path) }
        } else if (serverSide != null && c.serverId > 0) {
            ImageLoader.server(ctx(), viewLifecycleOwner.lifecycleScope, img, c.serverId, serverSide)
            img.setOnClickListener { PhotoActivity.openServer(ctx(), c.serverId, serverSide) }
        } else img.setImageDrawable(null)
    }

    private fun small(text: String, desc: String, onClick: () -> Unit): MaterialButton =
        ctx().button("text", text, onClick).apply {
            contentDescription = desc
            minWidth = 0; minimumWidth = 0
            setPadding(ctx().dp(10), 0, ctx().dp(10), 0)
            insetTop = 0; insetBottom = 0
            textSize = if (text.length <= 2) 22f else 15f          // ↻ і 🗑 — великі, щоб влучити пальцем
            layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ctx().dp(44))
        }

    /** Плитка «＋ …» (пунктирна рамка): зворотний бік, ще фото. */
    private fun addBox(text: String, desc: String, height: Int, onClick: () -> Unit): View =
        FrameLayout(ctx()).apply {
            layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ctx().dp(height))
            setBackgroundResource(R.drawable.tile_add)
            contentDescription = desc
            isClickable = true
            isFocusable = true
            setOnClickListener { onClick() }
            addView(TextView(ctx()).apply {
                this.text = text
                gravity = Gravity.CENTER
                textSize = 15f
                setTextColor(androidx.core.content.ContextCompat.getColor(ctx(), R.color.accent_dark))
                paint.isFakeBoldText = true
            }, FrameLayout.LayoutParams(-1, -1))
        }

    private fun badge(text: String) = TextView(ctx()).apply {
        this.text = text
        textSize = 11f
        setTextColor(0xFFFFFFFF.toInt())
        setBackgroundResource(R.drawable.qr_badge)
        setPadding(ctx().dp(6), ctx().dp(1), ctx().dp(6), ctx().dp(1))
    }

    private fun box(height: Int) = FrameLayout(ctx()).apply {
        layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ctx().dp(height))
        setBackgroundResource(R.drawable.panel_round)
    }

    private fun image() = ImageView(ctx()).apply {
        scaleType = ImageView.ScaleType.FIT_CENTER
        layoutParams = FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
    }

    fun renderPhotos() {
        val bb = _b ?: return
        val c = cap() ?: return
        val area = bb.photoArea
        area.removeAllViews()
        val row = LinearLayout(ctx()).apply { orientation = LinearLayout.HORIZONTAL }
        // лицьовий бік
        val fCol = LinearLayout(ctx()).apply { orientation = LinearLayout.VERTICAL }
        val fBox = box(118)
        val fImg = image()
        showImage(fImg, shownFront(c), "front", c)
        fBox.addView(fImg)
        fBox.addView(badge(if (c.frontNew) "Лицьовий · ⏳" else "Лицьовий"),
            FrameLayout.LayoutParams(-2, -2, Gravity.TOP or Gravity.START))
        fCol.addView(fBox)
        val fTools = LinearLayout(ctx()).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER }
        fTools.addView(small("📷 Знову", "Зняти лицьовий бік знову") { shoot("front") })
        if (local(c.front)) fTools.addView(small("↻", "Повернути на 90°") { rotate("front") })
        fCol.addView(fTools)
        row.addView(fCol, LinearLayout.LayoutParams(0, -2, 1f).apply { marginEnd = ctx().dp(6) })
        // зворотний бік
        val bCol = LinearLayout(ctx()).apply { orientation = LinearLayout.VERTICAL }
        if (c.hasBack) {
            val bBox = box(118)
            val bImg = image()
            showImage(bImg, shownBack(c), if (c.back.isEmpty()) "back" else null, c)
            bBox.addView(bImg)
            bBox.addView(badge(if (c.backNew) "Зворотний · ⏳" else "Зворотний"),
                FrameLayout.LayoutParams(-2, -2, Gravity.TOP or Gravity.START))
            bCol.addView(bBox)
            val bTools = LinearLayout(ctx()).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER }
            bTools.addView(small("📷 Знову", "Зняти зворотний бік знову") { shoot("back") })
            if (local(c.back)) bTools.addView(small("↻", "Повернути на 90°") { rotate("back") })
            bTools.addView(small("🗑", "Прибрати зворотний бік") {
                ctx().confirm("Прибрати зворотний бік?", if (c.status == Capture.UPLOADED)
                    "Фото зворотного боку зникне й у Minerva." else "Фото зворотного боку зникне.", "🗑 Прибрати") {
                    CaptureEdit.dropBack(uuid); renderPhotos()
                }
            })
            bCol.addView(bTools)
        } else {
            bCol.addView(addBox("＋ Зворотний бік\n📷 зняти зараз", "Зняти зворотний бік", 118) { shoot("back") })
        }
        row.addView(bCol, LinearLayout.LayoutParams(0, -2, 1f))
        area.addView(row)
        b.edges.show(c.frontAlt.isNotEmpty() || c.backAlt.isNotEmpty())

        // 📎 ще фото групи
        area.addView(TextView(ctx()).apply {
            text = if (c.extras.isEmpty()) "📎 Ще фото до цієї фірми: друга візитка, флаєр, каталог…"
            else "📎 Ще фото (${c.extras.size}) — AI прочитає все разом"
            textSize = 13f
            alpha = 0.8f
            setPadding(0, ctx().dp(6), 0, ctx().dp(4))
        })
        val tiles = c.extras.map { e -> extraTile(c, e) } + addTile()
        tiles.chunked(3).forEach { chunk ->
            val r = LinearLayout(ctx()).apply { orientation = LinearLayout.HORIZONTAL }
            chunk.forEachIndexed { i, v ->
                r.addView(v, LinearLayout.LayoutParams(0, ctx().dp(92), 1f).apply {
                    if (i < 2) marginEnd = ctx().dp(6); bottomMargin = ctx().dp(6)
                })
            }
            repeat(3 - chunk.size) { r.addView(View(ctx()), LinearLayout.LayoutParams(0, 1, 1f)) }
            area.addView(r)
        }
        val qr = (listOf(c.qrFront, c.qrBack) + c.extras.map { it.qr }).filter { it.isNotBlank() }
        b.qrInfo.show(qr.isNotEmpty())
        if (qr.isNotEmpty()) b.qrInfo.text = "🔳 QR-код: " + qr.joinToString(" · ") { Qr.short(it) } + " — Minerva заповнить поля з нього"
    }

    private fun extraTile(c: Capture, e: Extra): View {
        val f = box(92)
        val img = image()
        if (local(e.path)) ImageLoader.local(viewLifecycleOwner.lifecycleScope, img, e.path)
        else if (e.serverId > 0 && c.serverId > 0) ImageLoader.server(ctx(), viewLifecycleOwner.lifecycleScope, img, c.serverId, "p${e.serverId}")
        f.addView(img)
        val label = meta.photoKindLabel(e.kind)
        f.addView(badge(label.substringBefore(" ") + if (e.serverId == 0 && c.status == Capture.UPLOADED) " ⏳" else ""),
            FrameLayout.LayoutParams(-2, -2, Gravity.TOP or Gravity.START))
        f.contentDescription = label
        f.setOnClickListener { extraMenu(c, e) }
        return f
    }

    private fun addTile(): View =
        addBox("＋ Ще фото", "Ще фото: друга візитка, флаєр, каталог, продукт, стенд", 92) { addExtra() }

    private fun extraMenu(c: Capture, e: Extra) {
        val labels = mutableListOf<String>()
        val acts = mutableListOf<() -> Unit>()
        labels += "👁 Переглянути"; acts += {
            if (local(e.path)) PhotoActivity.openFile(ctx(), e.path)
            else if (e.serverId > 0) PhotoActivity.openServer(ctx(), c.serverId, "p${e.serverId}")
        }
        labels += "📷 Зняти знову"; acts += { shoot("extra:${e.uuid}", e.kind) }
        if (local(e.path)) { labels += "↻ Повернути на 90°"; acts += { rotate(e.uuid) } }
        labels += "🗑 Прибрати"; acts += {
            ctx().confirm("Прибрати це фото?", if (e.serverId > 0) "Фото зникне й у Minerva." else "Фото зникне.", "🗑 Прибрати") {
                CaptureEdit.dropExtra(uuid, e.uuid); renderPhotos()
            }
        }
        MaterialAlertDialogBuilder(ctx()).setTitle(meta.photoKindLabel(e.kind))
            .setItems(labels.toTypedArray()) { _, i -> acts[i]() }.show()
    }

    /** ＋ Ще фото: що це → камера (там і 🖼 галерея). */
    private fun addExtra() {
        val kinds = meta.photoKinds
        MaterialAlertDialogBuilder(ctx()).setTitle("📎 Що дознімати?")
            .setItems(kinds.map { it.second }.toTypedArray()) { _, i -> shoot("extra", kinds[i].first) }
            .show()
    }

    private fun rotate(slot: String) {
        val app = ctx().applicationContext
        viewLifecycleOwner.lifecycleScope.launch {
            val ok = withContext(Dispatchers.IO) { runCatching { CaptureEdit.rotate(uuid, slot) }.getOrDefault(false) }
            if (!ok) app.toast("Не вдалося повернути фото")
            renderPhotos()
        }
    }

    /** Знімати фото для цього знімка: поля — запам'ятати, вікно — закрити, камера; потім знову це вікно. */
    private fun shoot(slot: String, kind: String = "card") {
        val act = activity as? MainActivity ?: return
        storeFields()
        if (cap()?.isDraft == true) {                   // 📐 вибрана версія — лишається
            CaptureEdit.resolveAlts(uuid, edges)
        }
        leaving = true
        val back = act.currentTab
        dismiss()
        act.shootFor(ShotTarget(uuid, slot, kind, back))
    }

    // ---------------------------------------------------------------- поля

    private fun fillCategories(selected: List<Int>) {
        b.cats.removeAllViews()
        meta.categories.forEach { cat -> b.cats.addToggle(cat.label, cat.id in selected, cat.id) }
        if (meta.perms.catAdd) b.cats.addAction("＋ Нова категорія") { newCategory() }
    }

    private fun newCategory() {
        val keep = b.cats.checkedTags().filterIsInstance<Int>()
        Dialogs.category(requireContext(), viewLifecycleOwner.lifecycleScope, null) { created ->
            meta = Prefs.meta()
            fillCategories(keep + created.id)
        }
    }

    private fun listen() {
        val i = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
            .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            .putExtra(RecognizerIntent.EXTRA_PROMPT, "Примітка до візитки")
        runCatching { speech.launch(i) }.onFailure { requireContext().toast("На телефоні немає розпізнавання мовлення") }
    }

    private class Fields(val kind: String, val role: String, val note: String, val event: String,
                         val marks: List<String>, val cats: List<Int>, val readAi: Boolean, val visibility: String)

    private fun fields() = Fields(
        kind = b.kinds.checkedTags().filterIsInstance<String>().firstOrNull() ?: "card",
        role = b.roles.checkedTags().filterIsInstance<String>().firstOrNull() ?: "",
        note = b.note.text?.toString()?.trim().orEmpty(), event = b.event.text?.toString()?.trim().orEmpty(),
        marks = b.marks.checkedTags().filterIsInstance<String>(), cats = b.cats.checkedTags().filterIsInstance<Int>(),
        readAi = b.readAi.isChecked,
        visibility = b.vis.checkedTags().filterIsInstance<String>().firstOrNull() ?: initialVis,
    )

    /** Поля — у знімок (без зміни стану): пішли знімати — повернемось до тих самих. */
    private fun storeFields() {
        val f = fields()
        CaptureStore.update(uuid) {
            it.kind = f.kind; it.role = f.role; it.note = f.note; it.event = f.event; it.marks = f.marks
            it.categories = f.cats; it.visibility = f.visibility
            if (it.status != Capture.UPLOADED) it.readAi = f.readAi
        }
    }

    /** close = false — вікно вже закривається («назад» / потягнули вниз). */
    private fun save(action: String, close: Boolean = true) {
        if (done) return
        done = true
        val c = cap() ?: run { if (close) dismiss(); return }
        val f = fields()
        if (c.isDraft) {
            CaptureEdit.resolveAlts(uuid, edges)
            CaptureStore.update(uuid) {
                it.kind = f.kind; it.role = f.role; it.event = f.event; it.note = f.note; it.marks = f.marks
                it.categories = f.cats; it.readAi = f.readAi; it.status = Capture.PENDING
                it.visibility = f.visibility
                it.held = !Prefs.paired                 // без підключення — у Minerva лише після «⬆ Надіслати»
            }
            Prefs.kind = f.kind
            Prefs.visibility = f.visibility
            Sync.uploadNow(requireContext())
            requireContext().toast(if (Prefs.paired) "✅ Збережено — надішлемо в Minerva" else "✅ Збережено на телефоні — Minerva не підключено; надіслати можна буде після підключення")
        } else {
            CaptureStore.update(uuid) {
                it.kind = f.kind; it.role = f.role; it.event = f.event; it.note = f.note; it.marks = f.marks
                it.categories = f.cats; it.visibility = f.visibility
                if (it.status != Capture.UPLOADED) {
                    it.readAi = f.readAi
                    if (it.status == Capture.FAILED) it.status = Capture.PENDING
                }
            }
            if (c.status == Capture.UPLOADED && c.serverId > 0) pushEdit(c.serverId, f, f.role.takeIf { it != initialRole },
                f.visibility.takeIf { it != initialVis })
            Sync.uploadNow(requireContext())
        }
        setFragmentResult("review", bundleOf("action" to action, "uuid" to uuid))
        if (close) dismiss()
    }

    /** Знімок уже в Minerva — правка полів іде туди ж (без інтернету — повідомити). */
    private fun pushEdit(id: Int, f: Fields, role: String?, visibility: String?) {
        val app = requireContext().applicationContext
        val body = JSONObject().put("kind", f.kind).put("event", f.event).put("note", f.note)
            .put("marks", JSONArray(f.marks)).put("categories", JSONArray(f.cats))
        if (role != null) body.put("role", role)          // лише якщо змінили — підказку AI не затираємо
        if (visibility != null) body.put("visibility", visibility)
        requireActivity().lifecycleScope.launch {
            try {
                val card = Api.updateCard(id, body)
                CaptureStore.byServer(id)?.let { cap ->
                    CaptureStore.update(cap.uuid) { CaptureStore.mergeServer(it, card.json) }
                }
                app.toast("💾 Збережено в Minerva")
            } catch (e: ApiException) {
                app.toast("⚠️ ${e.message}", true)
            }
        }
    }

    override fun onDestroyView() {
        _b = null
        super.onDestroyView()
    }

    companion object {
        fun forEdit(uuid: String) = ReviewSheet().apply { arguments = bundleOf("uuid" to uuid) }
    }
}
