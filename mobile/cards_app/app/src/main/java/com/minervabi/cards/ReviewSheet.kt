package com.minervabi.cards

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.speech.RecognizerIntent
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.ArrayAdapter
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.os.bundleOf
import androidx.fragment.app.setFragmentResult
import androidx.lifecycle.lifecycleScope
import com.google.android.material.bottomsheet.BottomSheetBehavior
import com.google.android.material.bottomsheet.BottomSheetDialog
import com.google.android.material.bottomsheet.BottomSheetDialogFragment
import com.google.android.material.chip.Chip
import com.minervabi.cards.databinding.SheetReviewBinding
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * Після знімка (mode new) або правка знімка з «📤 Мої знімки» (mode edit): примітка (🎤 голосом), де взяли, позначки,
 * категорії (＋ нова), що це, AI. Значення за замовчуванням — з пресету. Великі кнопки «Зберегти» — угорі вікна.
 * 📐 Знімок «по краях» має й версію «по рамці» — перемикач вибирає, яку надіслати. Нове фото не губиться: закрили
 * вікно жестом «назад» або потягнули вниз — збережено, як «💾 Зберегти й наступну»; прибрати — «✕ Не зберігати».
 * Результат — setFragmentResult("review", {action: next | save | cancel, uuid}).
 */
class ReviewSheet : BottomSheetDialogFragment() {
    private var _b: SheetReviewBinding? = null
    private val b get() = _b!!
    private lateinit var meta: Meta
    private var front: String = ""
    private var back: String = ""
    private var frontFrame: String = ""     // 📐 та сама сторона «по рамці»
    private var backFrame: String = ""
    private var edges = true
    private var done = false
    private var editing: Capture? = null

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

    /** «Назад» або потягнули вниз: нове фото — зберегти (його ж і знімали), правка — без змін. */
    override fun onCancel(dialog: android.content.DialogInterface) {
        super.onCancel(dialog)
        if (editing == null && !done && _b != null) save("next", close = false)
    }

    override fun onViewCreated(view: View, s: Bundle?) {
        meta = Prefs.meta()
        val args = requireArguments()
        editing = args.getString("uuid")?.let { CaptureStore.get(it) }
        front = editing?.front ?: args.getString("front").orEmpty()
        back = editing?.back ?: args.getString("back").orEmpty()
        if (editing == null) {
            frontFrame = args.getString("front_alt").orEmpty()
            backFrame = args.getString("back_alt").orEmpty()
        }
        val qr = listOfNotNull(editing?.qrFront ?: args.getString("qr_front"), editing?.qrBack ?: args.getString("qr_back"))
            .filter { it.isNotBlank() }
        showFront()
        showBack()
        b.imgFront.setOnClickListener { PhotoActivity.openFile(requireContext(), shownFront()) }
        b.imgBack.setOnClickListener { if (back.isNotEmpty()) PhotoActivity.openFile(requireContext(), shownBack()) }
        b.dropBack.setOnClickListener {
            if (editing == null) { File(back).delete(); if (backFrame.isNotEmpty()) File(backFrame).delete() }
            back = ""; backFrame = ""
            showBack()
            b.edges.show(frontFrame.isNotEmpty())
        }
        b.edges.show(frontFrame.isNotEmpty() || backFrame.isNotEmpty())
        b.edges.setOnCheckedChangeListener { _, on ->
            edges = on
            b.edges.text = if (on) "📐 Обрізано по краях і вирівняно (вимкніть — по рамці)" else "▭ Обрізано по рамці (увімкніть — по краях)"
            showFront(); showBack()
        }
        if (qr.isNotEmpty()) {
            b.qrInfo.show(true)
            b.qrInfo.text = "🔳 QR-код: " + qr.joinToString(" · ") { Qr.short(it) } + " — Minerva заповнить поля з нього"
        }

        val preset = if (editing != null) null else meta.preset(currentPresetId())
        val c = editing
        b.note.setText(c?.note ?: preset?.note.orEmpty())
        b.event.setText(c?.event ?: preset?.event.orEmpty())
        val events = (meta.events + meta.presets.map { it.event } +
            CaptureStore.all().map { it.event }).filter { it.isNotBlank() }.distinct()
        b.event.setAdapter(ArrayAdapter(requireContext(), android.R.layout.simple_dropdown_item_1line, events))
        b.noteBox.setEndIconOnClickListener { listen() }

        val marks = c?.marks ?: preset?.marks.orEmpty()
        meta.marks.forEach { (k, label) -> b.marks.addToggle(label, k in marks, k) }
        val cats = c?.categories ?: preset?.categories.orEmpty()
        fillCategories(cats)
        val kind = c?.kind ?: preset?.kind ?: Prefs.kind
        meta.kinds.forEach { (k, label) ->
            b.kinds.addToggle(label, k == kind, k).isCheckedIconVisible = false
        }
        b.readAi.isChecked = c?.readAi ?: preset?.readAi ?: Prefs.readAiDefault
        if (c != null && c.status == Capture.UPLOADED) {
            b.readAi.show(false)         // уже в Minerva: AI — у картці візитки
        }
        if (c != null) {
            b.btnSaveNext.text = "💾 Зберегти"
            b.btnSave.show(false)
            b.btnCancel.text = "Закрити"
        }
        b.btnSaveNext.setOnClickListener { save(if (c != null) "save" else "next") }
        b.btnSave.setOnClickListener { save("save") }
        b.btnCancel.setOnClickListener {
            done = true
            if (c == null) listOf(front, back, frontFrame, backFrame).filter { it.isNotEmpty() }.forEach { File(it).delete() }
            setFragmentResult("review", bundleOf("action" to "cancel"))
            dismiss()
        }
    }

    private fun shownFront() = if (!edges && frontFrame.isNotEmpty()) frontFrame else front
    private fun shownBack() = if (!edges && backFrame.isNotEmpty()) backFrame else back

    private fun showFront() {
        ImageLoader.local(viewLifecycleOwner.lifecycleScope, b.imgFront, shownFront())
    }

    private fun currentPresetId(): Int? = Prefs.presetId?.takeIf { it > 0 } ?: if (Prefs.presetId == null) meta.defaultPreset else null

    private fun showBack() {
        val has = back.isNotEmpty()
        b.noBack.show(!has)
        b.dropBack.show(has)
        if (has) ImageLoader.local(viewLifecycleOwner.lifecycleScope, b.imgBack, shownBack()) else b.imgBack.setImageDrawable(null)
    }

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

    /** close = false — вікно вже закривається («назад» / потягнули вниз). */
    private fun save(action: String, close: Boolean = true) {
        if (done) return
        done = true
        // 📐 вибрана версія фото лишається, інша — геть з телефона
        val keepFront = shownFront(); val keepBack = shownBack()
        listOf(front, frontFrame).filter { it.isNotEmpty() && it != keepFront }.forEach { File(it).delete() }
        if (back.isNotEmpty()) listOf(back, backFrame).filter { it.isNotEmpty() && it != keepBack }.forEach { File(it).delete() }
        front = keepFront; back = if (back.isNotEmpty()) keepBack else ""
        val marks = b.marks.checkedTags().filterIsInstance<String>()
        val cats = b.cats.checkedTags().filterIsInstance<Int>()
        val kind = b.kinds.checkedTags().filterIsInstance<String>().firstOrNull() ?: "card"
        val note = b.note.text?.toString()?.trim().orEmpty()
        val event = b.event.text?.toString()?.trim().orEmpty()
        val c = editing
        if (c == null) {
            val preset = meta.preset(currentPresetId())
            val args = requireArguments()
            CaptureStore.add(Capture(
                front = front, back = back, kind = kind, event = event, note = note, marks = marks,
                categories = cats, presetId = preset?.id, readAi = b.readAi.isChecked,
                model = preset?.model?.ifEmpty { null } ?: Prefs.model,
                qrFront = args.getString("qr_front").orEmpty(), qrBack = if (back.isEmpty()) "" else args.getString("qr_back").orEmpty(),
            ))
            Prefs.kind = kind
            Sync.uploadNow(requireContext())
            requireContext().toast(if (Prefs.paired) "✅ Збережено — надішлемо в Minerva" else "✅ Збережено на телефоні — підключіть Minerva, щоб надіслати")
        } else {
            CaptureStore.update(c.uuid) {
                it.kind = kind; it.event = event; it.note = note; it.marks = marks; it.categories = cats
                if (it.status != Capture.UPLOADED) {
                    it.back = back; it.readAi = b.readAi.isChecked
                    if (back.isEmpty()) it.qrBack = ""
                    if (it.status == Capture.FAILED) it.status = Capture.PENDING
                }
            }
            if (c.status == Capture.UPLOADED && c.serverId > 0) pushEdit(c.serverId, kind, event, note, marks, cats)
            else Sync.uploadNow(requireContext())
        }
        setFragmentResult("review", bundleOf("action" to action, "uuid" to (c?.uuid ?: "")))
        if (close) dismiss()
    }

    /** Знімок уже в Minerva — правка йде туди ж (без інтернету — повідомити). */
    private fun pushEdit(id: Int, kind: String, event: String, note: String, marks: List<String>, cats: List<Int>) {
        val app = requireContext().applicationContext
        val body = JSONObject().put("kind", kind).put("event", event).put("note", note)
            .put("marks", JSONArray(marks)).put("categories", JSONArray(cats))
        requireActivity().lifecycleScope.launch {
            try {
                val card = Api.updateCard(id, body)
                CaptureStore.all().firstOrNull { it.serverId == id }?.let { cap ->
                    CaptureStore.update(cap.uuid) { it.server = card.json }
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
        fun forNew(front: String, back: String, qrFront: String, qrBack: String, frontAlt: String = "", backAlt: String = "") =
            ReviewSheet().apply {
                arguments = bundleOf("front" to front, "back" to back, "qr_front" to qrFront, "qr_back" to qrBack,
                    "front_alt" to frontAlt, "back_alt" to backAlt)
            }

        fun forEdit(uuid: String) = ReviewSheet().apply { arguments = bundleOf("uuid" to uuid) }
    }
}
