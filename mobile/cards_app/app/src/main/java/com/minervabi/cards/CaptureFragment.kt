package com.minervabi.cards

import android.Manifest
import android.annotation.SuppressLint
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.view.HapticFeedbackConstants
import android.view.LayoutInflater
import android.view.MotionEvent
import android.view.View
import android.view.ViewGroup
import androidx.activity.result.ActivityResult
import androidx.activity.result.IntentSenderRequest
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.FocusMeteringAction
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.Preview
import androidx.camera.core.UseCaseGroup
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.content.ContextCompat
import androidx.fragment.app.Fragment
import androidx.lifecycle.lifecycleScope
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

/**
 * 📷 Знімати: камера на весь екран з рамкою, велика кнопка (і кнопка гучності), лицьовий → зворотний бік → вікно
 * приміток. QR на візитці бачить ще в видошукачі. 🖼 — з галереї, 📄 — скан по контуру (Google).
 */
class CaptureFragment : Fragment() {
    private var _b: FragmentCaptureBinding? = null
    private val b get() = _b!!
    private lateinit var executor: ExecutorService
    private val scanner by lazy { Qr.scanner() }
    private var provider: ProcessCameraProvider? = null
    private var camera: Camera? = null
    private var imageCapture: ImageCapture? = null
    private var liveQr: List<String> = emptyList()
    private var liveQrAt = 0L
    private var front: File? = null
    private var frontQr = ""
    private var busy = false
    private val queueListener: () -> Unit = { queueChip() }

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
        b.btnScan.setOnClickListener { docScanner() }
        b.btnBack.setOnClickListener {
            b.backStrip.show(false)
            hint("📷 Зворотний бік: наведіть рамку й натисніть кнопку")
        }
        b.btnDone.setOnClickListener { front?.let { review(it, null, frontQr, "") } }
        b.frontThumb.setOnClickListener {
            requireContext().confirm("Зняти знову?", "Цей знімок лицьового боку прибрати й зняти заново.", "🔄 Знову") { resetShot(true) }
        }
        b.chipPreset.setOnClickListener {
            Dialogs.choosePreset(requireContext(), viewLifecycleOwner.lifecycleScope) { chips() }
        }
        b.chipKind.setOnClickListener {
            val kinds = Prefs.meta().kinds
            val i = kinds.indexOfFirst { it.first == Prefs.kind }
            val next = kinds[(i + 1) % kinds.size].first
            Prefs.kind = next
            Prefs.frame = when (next) { "flyer" -> "flyer"; "photo", "other" -> "free"; else -> "card" }
            chips()
        }
        b.chipFrame.setOnClickListener {
            val modes = listOf("card", "flyer", "qr", "free")
            Prefs.frame = modes[(modes.indexOf(Prefs.frame) + 1) % modes.size]
            chips()
        }
        b.chipFlash.setOnClickListener { Prefs.flash = !Prefs.flash; applyFlash(); chips() }
        b.chipQueue.setOnClickListener { (activity as? MainActivity)?.select(R.id.nav_queue) }
        b.preview.setOnTouchListener { v, e ->
            if (e.action == MotionEvent.ACTION_UP) {
                camera?.cameraControl?.startFocusAndMetering(
                    FocusMeteringAction.Builder(b.preview.meteringPointFactory.createPoint(e.x, e.y)).build())
                v.performClick()
            }
            true
        }
        childFragmentManager.setFragmentResultListener("review", viewLifecycleOwner) { _, r ->
            resetShot(false)
            if (r.getString("action") == "save") (activity as? MainActivity)?.select(R.id.nav_queue)
        }
        CaptureStore.listen(queueListener)
        chips()
        queueChip()
        if (!isHidden) startCamera()
    }

    override fun onHiddenChanged(hidden: Boolean) {
        super.onHiddenChanged(hidden)
        if (hidden) provider?.unbindAll() else { chips(); startCamera() }
    }

    override fun onResume() {
        super.onResume()
        if (!isHidden) chips()
    }

    override fun onDestroyView() {
        CaptureStore.unlisten(queueListener)
        provider?.unbindAll()
        executor.shutdown()
        _b = null
        super.onDestroyView()
    }

    private fun chips() {
        val meta = Prefs.meta()
        val p = meta.preset(Prefs.presetId?.takeIf { it > 0 } ?: if (Prefs.presetId == null) meta.defaultPreset else null)
        b.chipPreset.text = if (p != null) "📌 ${p.name} ▾" else "📌 Без пресету ▾"
        b.chipKind.text = meta.kindLabel(Prefs.kind)
        b.chipFrame.text = when (Prefs.frame) { "flyer" -> "▯ A4"; "qr" -> "▢ QR"; "free" -> "⛶ Увесь кадр"; else -> "▭ Візитка" }
        b.chipFlash.text = if (Prefs.flash) "🔦 Світло" else "⚡ Вимк."
        b.overlay.mode = Prefs.frame
        if (front == null) hint(if (Prefs.frame == "qr") "Наведіть на QR-код" else "Лицьовий бік — наведіть рамку")
    }

    private fun queueChip() {
        val bb = _b ?: return
        val c = CaptureStore.counts()
        bb.chipQueue.text = when {
            !Prefs.paired -> "🔗 Не підключено"
            Prefs.needsPairing -> "🔗 Підключіть знову"
            (c["waiting"] ?: 0) > 0 -> "⏳ ${c["waiting"]} чекають"
            (c["failed"] ?: 0) > 0 -> "⚠️ ${c["failed"]} помилка"
            (c["reading"] ?: 0) > 0 -> "🧠 AI читає ${c["reading"]}"
            else -> "✓ усе надіслано"
        }
    }

    private fun hint(text: String) {
        _b?.hint?.text = text
    }

    private fun noCamera(text: String) {
        b.noCamera.text = text
        b.noCamera.show(true)
    }

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
                val analysis = ImageAnalysis.Builder()
                    .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST).build()
                    .also { it.setAnalyzer(executor, Qr.Analyzer(scanner, ::onLiveQr)) }
                val group = UseCaseGroup.Builder().addUseCase(preview).addUseCase(cap).addUseCase(analysis)
                bb.preview.viewPort?.let { group.setViewPort(it) }
                try {
                    p.unbindAll()
                    camera = p.bindToLifecycle(viewLifecycleOwner, CameraSelector.DEFAULT_BACK_CAMERA, group.build())
                    imageCapture = cap
                    applyFlash()
                } catch (e: Exception) {
                    noCamera("Камера недоступна: ${e.message}")
                }
            }
        }, ContextCompat.getMainExecutor(ctx))
    }

    private fun applyFlash() {
        camera?.cameraControl?.enableTorch(Prefs.flash)
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
        val side = if (front == null) "front" else "back"
        val raw = File(requireContext().cacheDir, "raw-${System.currentTimeMillis()}.jpg")
        val frame = bb.overlay.frame()
        val vw = bb.preview.width
        val vh = bb.preview.height
        val recentQr = if (System.currentTimeMillis() - liveQrAt < 3000) liveQr else emptyList()
        cap.takePicture(ImageCapture.OutputFileOptions.Builder(raw).build(), executor,
            object : ImageCapture.OnImageSavedCallback {
                override fun onImageSaved(r: ImageCapture.OutputFileResults) {
                    val out = CaptureStore.newFile(side)
                    val bmp = runCatching { ImageUtils.cropToFrame(raw, out, frame, vw, vh, Prefs.maxPx) }.getOrNull()
                    raw.delete()
                    val qr = Qr.join(Qr.read(bmp) + recentQr)
                    bmp?.recycle()
                    activity?.runOnUiThread {
                        busy = false
                        if (bmp == null || !out.exists()) { hint("⚠️ Не вийшло — спробуйте ще раз"); return@runOnUiThread }
                        shot(out, qr)
                    }
                }

                override fun onError(e: ImageCaptureException) {
                    raw.delete()
                    activity?.runOnUiThread { busy = false; hint("⚠️ Камера: ${e.message}") }
                }
            })
    }

    private fun shot(file: File, qr: String) {
        val bb = _b ?: return
        val f = front
        if (f == null) {
            front = file
            frontQr = qr
            val kindAsksBack = Prefs.kind == "card" || Prefs.kind == "flyer"
            if (Prefs.askBack && kindAsksBack) {
                ImageLoader.local(viewLifecycleOwner.lifecycleScope, bb.frontThumb, file.absolutePath)
                bb.backStrip.show(true)
                hint("✓ Лицьовий бік. Зворотний — ↩ або одразу ⭕")
            } else review(file, null, qr, "")
        } else {
            review(f, file, frontQr, qr)
        }
    }

    private fun review(f: File, back: File?, qrF: String, qrB: String) {
        _b?.backStrip?.show(false)
        if (childFragmentManager.findFragmentByTag("review") != null) return
        ReviewSheet.forNew(f.absolutePath, back?.absolutePath.orEmpty(), qrF, qrB).show(childFragmentManager, "review")
    }

    private fun resetShot(deleteFront: Boolean) {
        if (deleteFront) front?.delete()
        front = null
        frontQr = ""
        _b?.backStrip?.show(false)
        chips()
    }

    private fun importUri(uri: Uri) {
        val ctx = context ?: return
        viewLifecycleOwner.lifecycleScope.launch {
            val side = if (front == null) "front" else "back"
            val out = CaptureStore.newFile(side)
            val qr = withContext(Dispatchers.IO) {
                val bmp = runCatching { ImageUtils.fromUri(ctx, uri, out, Prefs.maxPx) }.getOrNull()
                val codes = Qr.read(bmp)
                bmp?.recycle()
                if (out.exists()) Qr.join(codes) else null
            }
            if (qr == null) ctx.toast("Не вдалося відкрити фото") else shot(out, qr)
        }
    }

    /** 📄 Сканер документів Google: сам знаходить краї, вирівнює й обрізає; до 2 сторінок — лицьовий і зворотний. */
    private fun docScanner() {
        val act = activity ?: return
        val options = GmsDocumentScannerOptions.Builder()
            .setGalleryImportAllowed(true)
            .setPageLimit(2)
            .setResultFormats(GmsDocumentScannerOptions.RESULT_FORMAT_JPEG)
            .setScannerMode(GmsDocumentScannerOptions.SCANNER_MODE_FULL)
            .build()
        GmsDocumentScanning.getClient(options).getStartScanIntent(act)
            .addOnSuccessListener { docScan.launch(IntentSenderRequest.Builder(it).build()) }
            .addOnFailureListener {
                act.alert("Скан по контуру недоступний", "Потрібні сервіси Google Play (модуль завантажиться сам за інтернету). " +
                    "Поки — фотографуйте кнопкою ⭕ з рамкою.\n\n${it.message.orEmpty()}")
            }
    }

    private fun scanned(r: ActivityResult) {
        val ctx = context ?: return
        val pages = GmsDocumentScanningResult.fromActivityResultIntent(r.data)?.pages.orEmpty()
        if (pages.isEmpty()) return
        viewLifecycleOwner.lifecycleScope.launch {
            val files = mutableListOf<Pair<File, String>>()
            withContext(Dispatchers.IO) {
                pages.take(2).forEachIndexed { i, page ->
                    val out = CaptureStore.newFile(if (i == 0) "front" else "back")
                    val bmp = runCatching { ImageUtils.fromUri(ctx, page.imageUri, out, Prefs.maxPx) }.getOrNull()
                    val qr = Qr.join(Qr.read(bmp))
                    bmp?.recycle()
                    if (out.exists()) files.add(out to qr)
                }
            }
            if (files.isEmpty()) { ctx.toast("Скан не вдалося прочитати"); return@launch }
            resetShot(true)
            review(files[0].first, files.getOrNull(1)?.first, files[0].second, files.getOrNull(1)?.second.orEmpty())
        }
    }
}
