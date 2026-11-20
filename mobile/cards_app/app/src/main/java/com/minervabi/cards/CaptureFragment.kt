package com.minervabi.cards

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.RectF
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Size
import android.view.HapticFeedbackConstants
import android.view.LayoutInflater
import android.view.MotionEvent
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.activity.result.ActivityResult
import androidx.activity.result.IntentSenderRequest
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.annotation.OptIn
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.ExperimentalGetImage
import androidx.camera.core.FocusMeteringAction
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.core.UseCaseGroup
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.content.ContextCompat
import androidx.fragment.app.Fragment
import androidx.lifecycle.lifecycleScope
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.documentscanner.GmsDocumentScannerOptions
import com.google.mlkit.vision.documentscanner.GmsDocumentScanning
import com.google.mlkit.vision.documentscanner.GmsDocumentScanningResult
import com.minervabi.cards.databinding.FragmentCaptureBinding
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import kotlin.math.ceil
import kotlin.math.hypot
import kotlin.math.max
import kotlin.math.min

/**
 * Що зняти для вже наявного знімка (з вікна приміток): slot — front / back (🔄 перезняти, дознімати зворотний),
 * extra (📎 ще фото: друга візитка, флаєр…) або extra:<uuid> (перезняти ще фото); returnTab — куди повернутись.
 */
data class ShotTarget(val uuid: String, val slot: String, val kind: String = "card", val returnTab: Int = R.id.nav_capture)

/**
 * 📷 Знімати: камера на весь екран з рамкою, велика кнопка (і кнопка гучності), лицьовий → зворотний бік → вікно
 * приміток. Режими над кнопкою: 🪪 Візитка · 📄 Флаєр · 🔳 QR · 📷 Фото. 📐 «по краях» — візитку / флаєр сам
 * знаходить у кадрі (зелений контур), обрізає по краях і вирівнює перспективу, не знайшов — обрізає по рамці;
 * утримати 📐 — скан Google. QR на візитці бачить ще в видошукачі. 🖼 — з галереї. Знімок для наявного (ShotTarget) —
 * одне фото, далі знову вікно приміток цього знімка.
 */
class CaptureFragment : Fragment() {
    private var _b: FragmentCaptureBinding? = null
    private val b get() = _b!!
    private lateinit var executor: ExecutorService
    private val scanner by lazy { Qr.scanner() }
    private val main = Handler(Looper.getMainLooper())
    private var provider: ProcessCameraProvider? = null
    private var camera: Camera? = null
    private var imageCapture: ImageCapture? = null
    private var liveQr: List<String> = emptyList()
    private var liveQrAt = 0L
    private var front: File? = null
    private var frontAlt: File? = null
    private var frontQr = ""
    private var busy = false
    private var pendingReview: (() -> Unit)? = null
    private val queueListener: () -> Unit = { queueChip() }
    private var target: ShotTarget? = null
    private var savedFrame: String? = null
    private var returnTab: Int? = null

    companion object {
        /** Знайдене займає ≥ 22 % ділянки пошуку (≈ третина рамки): менше — це логотип / QR на візитці, не вона. */
        const val MIN_FRAC = 0.22
    }

    /** Де шукати краї в живому кадрі (px PreviewView) — для потоку аналізу; null — не шукати. */
    private class LiveRoi(val rect: RectF, val vw: Int, val vh: Int)

    @Volatile private var live: LiveRoi? = null
    private var quad: FloatArray? = null
    private var quadAt = 0L

    private val permission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { ok ->
        if (ok) startCamera() else noCamera("Без дозволу на камеру фотографувати не вийде.\n\n🖼 — можна вибрати фото з галереї.")
    }
    private val pick = registerForActivityResult(ActivityResultContracts.PickVisualMedia()) { uri -> uri?.let(::importUri) }
    private val docScan = registerForActivityResult(ActivityResultContracts.StartIntentSenderForResult(), ::scanned)

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, s: Bundle?): View {
        _b = FragmentCaptureBinding.inflate(inflater, container, false)
        return b.root
    }

    @SuppressLint("ClickableViewAccessibility")
    override fun onViewCreated(view: View, s: Bundle?) {
        executor = Executors.newSingleThreadExecutor()
        b.shutter.setOnClickListener { shoot() }
        b.btnGallery.setOnClickListener {
            pick.launch(PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly))
        }
        b.btnEdges.setOnClickListener { toggleEdges() }
        b.btnEdges.setOnLongClickListener { docScanner(); true }
        b.btnBack.setOnClickListener {
            b.backStrip.show(false)
            b.modes.visibility = View.INVISIBLE
            hint("📷 Зворотний бік: наведіть рамку й натисніть ⭕")
        }
        b.btnDone.setOnClickListener { front?.let { review(it, frontAlt, null, null, frontQr, "") } }
        b.frontBox.setOnClickListener {
            requireContext().confirm("Зняти знову?", "Цей знімок лицьового боку прибрати й зняти заново.", "🔄 Знову") { resetShot(true) }
        }
        b.btnTargetCancel.setOnClickListener {
            val t = target ?: return@setOnClickListener
            endTarget()
            openReview(t.uuid)
        }
        b.chipPreset.setOnClickListener {
            Dialogs.choosePreset(requireContext(), viewLifecycleOwner.lifecycleScope) {
                Prefs.frame = frameForKind(Prefs.kind)
                chips()
            }
        }
        listOf(b.modeCard to "card", b.modeFlyer to "flyer", b.modeQr to "qr", b.modePhoto to "free").forEach { (v, m) ->
            v.setOnClickListener { setMode(m) }
        }
        b.btnFlash.setOnClickListener { Prefs.flash = !Prefs.flash; applyFlash(); chips() }
        b.chipQueue.setOnClickListener {
            if (!Prefs.paired) startActivity(android.content.Intent(requireContext(), PairActivity::class.java))
            else (activity as? MainActivity)?.select(R.id.nav_queue)
        }
        b.preview.setOnTouchListener { v, e ->
            if (e.action == MotionEvent.ACTION_UP) {
                camera?.cameraControl?.startFocusAndMetering(
                    FocusMeteringAction.Builder(b.preview.meteringPointFactory.createPoint(e.x, e.y)).build())
                v.performClick()
            }
            true
        }
        // рамка — між верхнім рядком і панеллю внизу (за їх справжніми розмірами)
        val insets = View.OnLayoutChangeListener { _, _, _, _, _, _, _, _, _ ->
            val bb = _b ?: return@OnLayoutChangeListener
            val ctx = context ?: return@OnLayoutChangeListener
            bb.overlay.topInset = bb.topBar.bottom + ctx.dp(4)
            bb.overlay.bottomInset = bb.root.height - bb.bottomPanel.top - bb.slot.top + ctx.dp(40)
            bb.overlay.post { updateLive() }
        }
        b.topBar.addOnLayoutChangeListener(insets)
        b.bottomPanel.addOnLayoutChangeListener(insets)
        childFragmentManager.setFragmentResultListener("review", viewLifecycleOwner) { _, r ->
            resetShot(false)
            val back = returnTab
            returnTab = null
            when {
                back != null && back != R.id.nav_capture -> (activity as? MainActivity)?.select(back)
                r.getString("action") == "save" -> (activity as? MainActivity)?.select(R.id.nav_queue)
            }
        }
        CaptureStore.listen(queueListener)
        chips()
        queueChip()
        if (!isHidden) startCamera()
    }

    override fun onHiddenChanged(hidden: Boolean) {
        super.onHiddenChanged(hidden)
        if (hidden) { live = null; provider?.unbindAll() } else { chips(); queueChip(); startCamera() }
    }

    override fun onResume() {
        super.onResume()
        if (!isHidden) chips()
        pendingReview?.let { pendingReview = null; it() }
    }

    override fun onDestroyView() {
        live = null
        CaptureStore.unlisten(queueListener)
        provider?.unbindAll()
        executor.shutdown()
        _b = null
        super.onDestroyView()
    }

    // ---------------------------------------------------------------- режими

    private fun frameForKind(kind: String) = when (kind) { "flyer" -> "flyer"; "photo", "other" -> "free"; else -> "card" }

    /** 🪪 Візитка · 📄 Флаєр · 🔳 QR · 📷 Фото — рамка й «що це» разом. */
    private fun setMode(m: String) {
        Prefs.frame = m
        Prefs.kind = when (m) {
            "flyer" -> "flyer"
            "free" -> if (Prefs.kind == "other") "other" else "photo"
            "qr" -> if (Prefs.kind == "other") "other" else "card"
            else -> "card"
        }
        clearQuad()
        chips()
        b.root.performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK)
    }

    private fun edgesApply() = Prefs.frame == "card" || Prefs.frame == "flyer"
    private fun edgesOn() = Prefs.edges && edgesApply()

    private fun toggleEdges() {
        val ctx = context ?: return
        if (!edgesApply()) {
            ctx.toast("📐 Краї — для візиток і флаєрів (🪪 / 📄)")
            return
        }
        Prefs.edges = !Prefs.edges
        clearQuad()
        chips()
        if (Prefs.edges) {
            ctx.toast(if (Prefs.edgesTip) "📐 Обрізаю по краях і вирівнюю" else
                "📐 Знайду краї візитки сам, обріжу й вирівняю. Утримуйте 📐 — скан Google", !Prefs.edgesTip)
            Prefs.edgesTip = true
        } else ctx.toast("▭ Обрізаю по рамці")
    }

    private fun chips() {
        val bb = _b ?: return
        val meta = Prefs.meta()
        val p = meta.preset(Prefs.presetId?.takeIf { it > 0 } ?: if (Prefs.presetId == null) meta.defaultPreset else null)
        bb.chipPreset.text = if (p != null) "📌 ${p.name} ▾" else "📌 Без пресету ▾"
        bb.btnFlash.text = if (Prefs.flash) "🔦" else "⚡"
        bb.btnFlash.setBackgroundResource(if (Prefs.flash) R.drawable.round_on else R.drawable.round_scrim)
        val mode = Prefs.frame
        listOf(bb.modeCard to "card", bb.modeFlyer to "flyer", bb.modeQr to "qr", bb.modePhoto to "free").forEach { (v, m) ->
            styleMode(v, m == mode)
        }
        val applies = edgesApply()
        bb.btnEdges.setBackgroundResource(if (applies && Prefs.edges) R.drawable.round_on else R.drawable.round_scrim)
        bb.edgesBox.alpha = if (applies) 1f else 0.45f
        bb.edgesLabel.text = when { !applies -> "увесь кадр"; Prefs.edges -> "по краях"; else -> "по рамці" }
        bb.overlay.mode = mode
        updateLive()
        if (front == null && !bb.backStrip.isShown) idleHint()
    }

    // ---------------------------------------------------------------- знімок для наявного (ShotTarget)

    private fun frameFor(t: ShotTarget, c: Capture) = when {
        t.slot == "front" || t.slot == "back" -> frameForKind(c.kind)
        t.kind == "card" -> "card"
        t.kind == "flyer" || t.kind == "catalog" -> "flyer"
        else -> "free"
    }

    private fun targetLabel(t: ShotTarget, c: Capture): String {
        val meta = Prefs.meta()
        val name = c.card?.company?.ifEmpty { null } ?: c.event.ifEmpty { meta.kindLabel(c.kind) }
        return when {
            t.slot == "back" && !c.hasBack -> "↩ Зворотний бік · $name"
            t.slot == "back" -> "🔄 Зворотний бік знову · $name"
            t.slot == "front" -> "🔄 Лицьовий бік знову · $name"
            t.slot.startsWith("extra:") -> "🔄 ${meta.photoKindLabel(t.kind)} знову · $name"
            else -> "📎 ${meta.photoKindLabel(t.kind)} · $name"
        }
    }

    /** Зняти одне фото для наявного знімка (з вікна приміток) — і назад у це вікно. */
    fun shootFor(t: ShotTarget) {
        val bb = _b ?: return
        val c = CaptureStore.get(t.uuid) ?: return
        front?.let { f ->                     // лицьовий бік, знятий щойно, не губиться — чернеткою
            CaptureStore.newDraft(f.absolutePath, "", frontAlt?.absolutePath.orEmpty(), "", frontQr, "")
            context?.toast("✏️ Щойно знятий лицьовий бік — у «📤 Мої знімки» чернеткою")
            front = null; frontAlt = null; frontQr = ""
        }
        if (savedFrame == null) savedFrame = Prefs.frame
        target = t
        // вікно відкрили з картки / «Мої знімки» — повертатись туди, навіть якщо знімати пішли вже з камери
        if (t.returnTab != R.id.nav_capture || returnTab == null) returnTab = t.returnTab
        Prefs.frame = frameFor(t, c)
        clearQuad()
        bb.backStrip.show(false)
        bb.modes.visibility = View.INVISIBLE
        bb.targetText.text = targetLabel(t, c)
        bb.targetStrip.show(true)
        chips()
    }

    private fun endTarget() {
        target = null
        savedFrame?.let { Prefs.frame = it }
        savedFrame = null
        _b?.targetStrip?.show(false)
        _b?.modes?.visibility = View.VISIBLE
        clearQuad()
        chips()
    }

    private fun applyTarget(file: File, alt: File?, qr: String) {
        val t = target ?: return
        if (CaptureStore.get(t.uuid) == null) {            // знімок тим часом прибрали
            file.delete(); alt?.delete(); endTarget(); return
        }
        when {
            t.slot == "front" || t.slot == "back" ->
                CaptureEdit.setSide(t.uuid, t.slot, file.absolutePath, qr, alt?.absolutePath.orEmpty())
            t.slot.startsWith("extra:") -> {
                alt?.delete(); CaptureEdit.replaceExtra(t.uuid, t.slot.removePrefix("extra:"), file.absolutePath, qr)
            }
            else -> { alt?.delete(); CaptureEdit.addExtra(t.uuid, file.absolutePath, t.kind, qr) }
        }
        endTarget()
        openReview(t.uuid)
    }

    /** Вікно приміток знімка (чернетка чи правка); returnTab — куди повернутись після «Зберегти» / «Закрити». */
    fun openReview(uuid: String, returnTab: Int? = null) {
        if (returnTab != null) this.returnTab = returnTab
        if (_b == null) return
        if (childFragmentManager.isStateSaved) {           // повернулись зі сканера, а екран ще не відновився
            pendingReview = { openReview(uuid) }
            return
        }
        if (childFragmentManager.findFragmentByTag("review") != null) return
        ReviewSheet.forEdit(uuid).show(childFragmentManager, "review")
    }

    private fun styleMode(v: TextView, on: Boolean) {
        if (on) v.setBackgroundResource(R.drawable.mode_on) else v.background = null
        v.setTextColor(if (on) 0xFF101820.toInt() else 0xFFFFFFFF.toInt())
        v.paint.isFakeBoldText = on
        v.invalidate()
    }

    private fun idleHint() {
        if (target != null) { hint("⭕ — зняти · 🖼 — з галереї"); return }
        hint(when {
            Prefs.frame == "qr" -> "🔳 Наведіть на QR-код — фото буде всього кадру"
            Prefs.frame == "free" -> "📷 Знімаю весь кадр"
            edgesOn() && quad != null -> "📐 Бачу краї — знімайте"
            edgesOn() -> "📐 Наведіть рамку на візитку — краї знайду сам"
            else -> "Лицьовий бік — наведіть рамку"
        })
    }

    private fun queueChip() {
        val bb = _b ?: return
        val c = CaptureStore.counts()
        bb.chipQueue.text = when {
            !Prefs.paired -> "🔗 Не підключено" + ((c["local"] ?: 0).takeIf { it > 0 }?.let { " · 📱 $it" } ?: "")
            Prefs.needsPairing -> "🔗 Підключіть знову"
            (c["waiting"] ?: 0) > 0 -> "⏳ ${c["waiting"]} чекають"
            (c["failed"] ?: 0) > 0 -> "⚠️ ${c["failed"]} помилка"
            (c["local"] ?: 0) > 0 -> "📱 ${c["local"]} на телефоні"
            (c["reading"] ?: 0) > 0 -> "🧠 AI читає ${c["reading"]}"
            else -> "✓ усе в Minerva"
        }
    }

    private fun hint(text: String) {
        _b?.hint?.text = text
    }

    private fun noCamera(text: String) {
        b.noCamera.text = text
        b.noCamera.show(true)
    }

    // ---------------------------------------------------------------- камера

    private fun startCamera() {
        val ctx = context ?: return
        if (_b == null || isHidden) return
        if (ContextCompat.checkSelfPermission(ctx, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            permission.launch(Manifest.permission.CAMERA); return
        }
        b.noCamera.show(false)
        val future = ProcessCameraProvider.getInstance(ctx)
        future.addListener({
            val bb = _b ?: return@addListener
            if (isHidden) return@addListener
            val p = runCatching { future.get() }.getOrNull() ?: run { noCamera("Камера недоступна."); return@addListener }
            provider = p
            bb.preview.post {
                if (_b == null || isHidden) return@post
                val preview = Preview.Builder().build().also { it.setSurfaceProvider(bb.preview.surfaceProvider) }
                val cap = ImageCapture.Builder().setCaptureMode(ImageCapture.CAPTURE_MODE_MAXIMIZE_QUALITY).build()
                // ~1280 px: дрібний QR і краї візитки видно краще, ніж на стандартних 640
                val analysis = ImageAnalysis.Builder()
                    .setResolutionSelector(ResolutionSelector.Builder().setResolutionStrategy(ResolutionStrategy(
                        Size(1280, 960), ResolutionStrategy.FALLBACK_RULE_CLOSEST_LOWER_THEN_HIGHER)).build())
                    .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST).build()
                    .also { it.setAnalyzer(executor, CamAnalyzer()) }
                val group = UseCaseGroup.Builder().addUseCase(preview).addUseCase(cap).addUseCase(analysis)
                bb.preview.viewPort?.let { group.setViewPort(it) }
                try {
                    p.unbindAll()
                    camera = p.bindToLifecycle(viewLifecycleOwner, CameraSelector.DEFAULT_BACK_CAMERA, group.build())
                    imageCapture = cap
                    applyFlash()
                    updateLive()
                } catch (e: Exception) {
                    noCamera("Камера недоступна: ${e.message}")
                }
            }
        }, ContextCompat.getMainExecutor(ctx))
    }

    private fun applyFlash() {
        camera?.cameraControl?.enableTorch(Prefs.flash)
    }

    /** Де шукати краї: рамка + 12 % з кожного боку (візитка в рамці, а не стіл навколо). */
    private fun searchArea(frame: RectF, vw: Int, vh: Int): RectF {
        val ex = frame.width() * 0.12f; val ey = frame.height() * 0.12f
        return RectF(max(0f, frame.left - ex), max(0f, frame.top - ey), min(vw.toFloat(), frame.right + ex),
            min(vh.toFloat(), frame.bottom + ey))
    }

    private fun updateLive() {
        val bb = _b ?: return
        val frame = bb.overlay.frame()
        val vw = bb.preview.width; val vh = bb.preview.height
        live = if (edgesOn() && frame != null && vw > 0 && vh > 0 && camera != null && !isHidden)
            LiveRoi(searchArea(frame, vw, vh), vw, vh) else null
        if (live == null) clearQuad()
    }

    private fun clearQuad() {
        quad = null
        _b?.overlay?.quad = null
    }

    /** Аналіз кадру: 📐 краї (раз на ~150 мс, лише коли увімкнено) і QR-коди. */
    private inner class CamAnalyzer : ImageAnalysis.Analyzer {
        private var last = 0L

        @OptIn(ExperimentalGetImage::class)
        override fun analyze(proxy: ImageProxy) {
            val media = proxy.image
            if (media == null) { proxy.close(); return }
            val roi = live
            val now = SystemClock.uptimeMillis()
            if (roi != null && now - last >= 150) {
                last = now
                val q = runCatching { edgesIn(proxy, roi) }.getOrNull()
                main.post { onLiveQuad(q, roi) }
            }
            val img = InputImage.fromMediaImage(media, proxy.imageInfo.rotationDegrees)
            scanner.process(img)
                .addOnSuccessListener { codes -> onLiveQr(codes.mapNotNull { it.rawValue?.trim() }.filter { it.isNotEmpty() }) }
                .addOnCompleteListener { proxy.close() }
        }
    }

    /**
     * Краї в живому кадрі: яскравість (площина Y) у межах видимої частини (cropRect — те, що в PreviewView), повернута
     * як на екрані, вужча сторона ≈ 280 px (рамка візитки — на всю ширину) → DocDetect → px PreviewView.
     */
    private fun edgesIn(proxy: ImageProxy, roi: LiveRoi): FloatArray? {
        val plane = proxy.planes[0]
        val buf = plane.buffer
        val rs = plane.rowStride; val ps = plane.pixelStride
        val crop = proxy.cropRect
        val rot = proxy.imageInfo.rotationDegrees
        val cw = crop.width(); val ch = crop.height()
        val uw = if (rot % 180 == 0) cw else ch
        val uh = if (rot % 180 == 0) ch else cw
        val step = max(1, ceil(min(uw, uh) / 280.0).toInt())
        val gw = uw / step; val gh = uh / step
        if (gw < 40 || gh < 40) return null
        val px = IntArray(gw * gh)
        for (gy in 0 until gh) {
            val uy = gy * step
            for (gx in 0 until gw) {
                val ux = gx * step
                val sx: Int; val sy: Int
                when (rot) {
                    90 -> { sx = uy; sy = ch - 1 - ux }
                    180 -> { sx = cw - 1 - ux; sy = ch - 1 - uy }
                    270 -> { sx = cw - 1 - uy; sy = ux }
                    else -> { sx = ux; sy = uy }
                }
                px[gy * gw + gx] = buf.get((crop.top + sy) * rs + (crop.left + sx) * ps).toInt() and 0xFF
            }
        }
        // сіре → PreviewView (FILL_CENTER; з ViewPort пропорції ті самі, тож зсуву зазвичай немає)
        val scale = max(roi.vw / gw.toFloat(), roi.vh / gh.toFloat())
        val ox = (gw * scale - roi.vw) / 2; val oy = (gh * scale - roi.vh) / 2
        val r = roi.rect
        val area = intArrayOf(((r.left + ox) / scale).toInt(), ((r.top + oy) / scale).toInt(),
            ceil((r.right + ox) / scale).toInt(), ceil((r.bottom + oy) / scale).toInt())
        val q = DocDetect.detect(DocDetect.Gray(gw, gh, px), area, MIN_FRAC) ?: return null
        return FloatArray(8) { i -> if (i % 2 == 0) q[i] * scale - ox else q[i] * scale - oy }
    }

    private fun onLiveQuad(q: FloatArray?, roi: LiveRoi) {
        val bb = _b ?: return
        if (live !== roi) return
        val now = SystemClock.uptimeMillis()
        val had = quad != null
        if (q == null) {
            if (now - quadAt > 700) clearQuad()
        } else {
            val prev = quad
            val near = prev != null && (0 until 4).all { k ->
                hypot((prev[2 * k] - q[2 * k]).toDouble(), (prev[2 * k + 1] - q[2 * k + 1]).toDouble()) < roi.rect.width() * 0.06
            }
            quad = if (near) FloatArray(8) { prev!![it] * 0.5f + q[it] * 0.5f } else q
            quadAt = now
            bb.overlay.quad = quad
        }
        if (had != (quad != null) && front == null && !bb.backStrip.isShown) idleHint()
    }

    private fun onLiveQr(codes: List<String>) {
        val bb = _b ?: return
        if (codes.isNotEmpty()) {
            liveQr = codes; liveQrAt = System.currentTimeMillis()
            bb.qrChip.text = "🔳 " + codes.joinToString(" · ") { Qr.short(it) }
            bb.qrChip.show(true)
            bb.overlay.found = true
        } else if (System.currentTimeMillis() - liveQrAt > 1500) {
            bb.qrChip.show(false)
            bb.overlay.found = false
        }
    }

    // ---------------------------------------------------------------- знімок

    /** ⭕ / кнопка гучності: лицьовий бік, потім (якщо хочете) зворотний. */
    fun shoot() {
        val cap = imageCapture ?: return
        val bb = _b ?: return
        if (busy) return
        busy = true
        bb.shutter.performHapticFeedback(HapticFeedbackConstants.VIRTUAL_KEY)
        bb.shutter.animate().scaleX(0.85f).scaleY(0.85f).setDuration(90).withEndAction {
            _b?.shutter?.animate()?.scaleX(1f)?.scaleY(1f)?.setDuration(90)
        }
        hint("⏳ Знімаю…")
        val side = if (target != null) "extra" else if (front == null) "front" else "back"
        val raw = File(requireContext().cacheDir, "raw-${System.currentTimeMillis()}.jpg")
        val frame = bb.overlay.frame()
        val vw = bb.preview.width
        val vh = bb.preview.height
        val mode = Prefs.frame
        val edges = edgesOn() && frame != null
        val seen = quad?.takeIf { SystemClock.uptimeMillis() - quadAt < 800 }?.copyOf()
        val recentQr = if (System.currentTimeMillis() - liveQrAt < 3000) liveQr else emptyList()
        cap.takePicture(ImageCapture.OutputFileOptions.Builder(raw).build(), executor,
            object : ImageCapture.OnImageSavedCallback {
                override fun onImageSaved(r: ImageCapture.OutputFileResults) {
                    val out = CaptureStore.newFile(side)
                    var alt: File? = null
                    val bmp = runCatching {
                        if (edges) cropEdges(raw, out, frame!!, vw, vh, seen, side)?.let { alt = it.second; it.first }
                        // QR — фото всього кадру (стенд, плакат), рамка — лише щоб навести на код
                        else ImageUtils.cropToFrame(raw, out, if (mode == "qr") null else frame, vw, vh, Prefs.maxPx)
                    }.getOrNull()
                    raw.delete()
                    val qr = Qr.join(Qr.read(bmp) + recentQr)
                    bmp?.recycle()
                    activity?.runOnUiThread {
                        busy = false
                        if (bmp == null || !out.exists()) {
                            alt?.delete(); hint("⚠️ Не вийшло — спробуйте ще раз"); return@runOnUiThread
                        }
                        shot(out, alt, qr)
                    }
                }

                override fun onError(e: ImageCaptureException) {
                    raw.delete()
                    activity?.runOnUiThread { busy = false; hint("⚠️ Камера: ${e.message}") }
                }
            })
    }

    /**
     * 📐 Знімок → краї візитки (на самому знімку, точніше за видошукач; не знайшов — контур з видошукача) → вирівняно
     * в [out]. Друга версія «по рамці» — у файл поруч (у вікні після знімка можна вибрати її). Країв немає — по рамці.
     */
    private fun cropEdges(raw: File, out: File, frame: RectF, vw: Int, vh: Int, seen: FloatArray?, side: String): Pair<Bitmap, File?>? {
        val maxPx = Prefs.maxPx
        val region = searchArea(frame, vw, vh)
        val big = ImageUtils.viewRegion(raw, region, vw, vh, (maxPx * 1.2f).toInt()) ?: return null
        val k = big.width / region.width()                   // px знімка в одному px екрана
        val (g, s) = ImageUtils.gray(big, 420)
        var q = DocDetect.detect(g, null, MIN_FRAC)?.let { a -> FloatArray(8) { a[it] * s } }
        if (q == null && seen != null) {
            q = FloatArray(8) { i -> if (i % 2 == 0) (seen[i] - region.left) * k else (seen[i] - region.top) * k }
        }
        val inFrame = RectF((frame.left - region.left) * k, (frame.top - region.top) * k,
            (frame.right - region.left) * k, (frame.bottom - region.top) * k)
        val warped = q?.let { ImageUtils.warp(big, it, maxPx) }
        val framed = ImageUtils.cut(big, inFrame, maxPx)
        big.recycle()
        if (warped == null) {
            framed ?: return null
            ImageUtils.save(framed, out)
            return framed to null
        }
        ImageUtils.save(warped, out)
        val alt = framed?.let { f -> CaptureStore.newFile("$side-frame").also { ImageUtils.save(f, it); f.recycle() } }
        return warped to alt
    }

    private fun shot(file: File, alt: File?, qr: String) {
        val bb = _b ?: return
        if (target != null) { applyTarget(file, alt, qr); return }
        val f = front
        if (f == null) {
            front = file
            frontAlt = alt
            frontQr = qr
            val kindAsksBack = Prefs.kind == "card" || Prefs.kind == "flyer"
            if (Prefs.askBack && kindAsksBack) {
                ImageLoader.local(viewLifecycleOwner.lifecycleScope, bb.frontThumb, file.absolutePath)
                bb.modes.visibility = View.INVISIBLE
                bb.backStrip.show(true)
                hint("✓ Лицьовий бік. Зворотний — ↩ або одразу ⭕")
            } else review(file, alt, null, null, qr, "")
        } else {
            review(f, frontAlt, file, alt, frontQr, qr)
        }
    }

    /** Знято (лицьовий і, може, зворотний бік) → чернетка в «📤 Мої знімки» (не загубиться) → вікно приміток. */
    private fun review(f: File, fAlt: File?, back: File?, bAlt: File?, qrF: String, qrB: String) {
        val bb = _b ?: return
        val c = CaptureStore.newDraft(f.absolutePath, back?.absolutePath.orEmpty(), fAlt?.absolutePath.orEmpty(),
            bAlt?.absolutePath.orEmpty(), qrF, qrB)
        front = null; frontAlt = null; frontQr = ""
        bb.backStrip.show(false)
        bb.modes.visibility = View.VISIBLE
        openReview(c.uuid)
    }

    private fun resetShot(deleteFront: Boolean) {
        if (deleteFront) { front?.delete(); frontAlt?.delete() }
        front = null
        frontAlt = null
        frontQr = ""
        _b?.backStrip?.show(false)
        _b?.modes?.visibility = View.VISIBLE
        chips()
    }

    private fun importUri(uri: Uri) {
        val ctx = context ?: return
        viewLifecycleOwner.lifecycleScope.launch {
            val side = if (target != null) "extra" else if (front == null) "front" else "back"
            val out = CaptureStore.newFile(side)
            val qr = withContext(Dispatchers.IO) {
                val bmp = runCatching { ImageUtils.fromUri(ctx, uri, out, Prefs.maxPx) }.getOrNull()
                val codes = Qr.read(bmp)
                bmp?.recycle()
                if (out.exists()) Qr.join(codes) else null
            }
            if (qr == null) ctx.toast("Не вдалося відкрити фото") else shot(out, null, qr)
        }
    }

    // ---------------------------------------------------------------- скан Google (утримати 📐)

    /** 📄 Сканер документів Google: окреме вікно, сам знаходить краї; до 2 сторінок — лицьовий і зворотний. */
    private fun docScanner() {
        val act = activity ?: return
        b.btnEdges.performHapticFeedback(HapticFeedbackConstants.LONG_PRESS)
        val options = GmsDocumentScannerOptions.Builder()
            .setGalleryImportAllowed(true)
            .setPageLimit(2)
            .setResultFormats(GmsDocumentScannerOptions.RESULT_FORMAT_JPEG)
            .setScannerMode(GmsDocumentScannerOptions.SCANNER_MODE_BASE_WITH_FILTER)
            .build()
        GmsDocumentScanning.getClient(options).getStartScanIntent(act)
            .addOnSuccessListener { docScan.launch(IntentSenderRequest.Builder(it).build()) }
            .addOnFailureListener {
                act.alert("Скан Google недоступний", "Потрібні сервіси Google Play (модуль завантажиться сам за " +
                    "інтернету). Кнопка ⭕ з 📐 «по краях» робить те саме в цій камері.\n\n${it.message.orEmpty()}")
            }
    }

    private fun scanned(r: ActivityResult) {
        val ctx = context ?: return
        if (r.resultCode != Activity.RESULT_OK) return
        val pages = runCatching { GmsDocumentScanningResult.fromActivityResultIntent(r.data)?.pages.orEmpty() }
            .getOrDefault(emptyList())
        if (pages.isEmpty()) { ctx.toast("Скан повернувся без сторінок — спробуйте ще раз або ⭕", true); return }
        viewLifecycleOwner.lifecycleScope.launch {
            val files = mutableListOf<Pair<File, String>>()
            var error = ""
            withContext(Dispatchers.IO) {
                pages.take(2).forEachIndexed { i, page ->
                    val out = CaptureStore.newFile(if (i == 0) "front" else "back")
                    try {
                        val bmp = ImageUtils.fromUri(ctx, page.imageUri, out, Prefs.maxPx)
                        val qr = Qr.join(Qr.read(bmp))
                        bmp?.recycle()
                        if (out.exists() && out.length() > 0) files.add(out to qr) else error = "порожній файл"
                    } catch (e: Exception) {
                        out.delete(); error = e.message ?: e.javaClass.simpleName
                    }
                }
            }
            if (files.isEmpty()) {
                ctx.alert("Скан не вдалося прочитати", "Сторінку зі сканера не відкрито: $error. Сфотографуйте ⭕ з 📐 «по краях».")
                return@launch
            }
            if (target != null) {                          // знімок для наявного — лише перша сторінка
                files.drop(1).forEach { it.first.delete() }
                shot(files[0].first, null, files[0].second)
                return@launch
            }
            resetShot(true)
            review(files[0].first, null, files.getOrNull(1)?.first, null, files[0].second, files.getOrNull(1)?.second.orEmpty())
        }
    }
}
