package com.minervabi.cards

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.minervabi.cards.databinding.ActivityPairBinding
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/**
 * 🔗 Підключення до Minerva: QR зі сторінки «📱 Додаток на телефон» ({"minerva": "cards", "url", "code"}) →
 * POST pair/ → ключ телефону. Один раз; далі телефон працює від імені того, хто показав QR.
 */
class PairActivity : AppCompatActivity() {
    private lateinit var b: ActivityPairBinding
    private lateinit var executor: ExecutorService
    private var busy = false
    private val scanner by lazy { Qr.scanner() }

    private val permission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { ok ->
        if (ok) startCamera() else b.status.text = "Без камери — «⌨ Ввести адресу й код вручну»."
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivityPairBinding.inflate(layoutInflater)
        setContentView(b.root)
        executor = Executors.newSingleThreadExecutor()
        b.overlay.mode = "qr"
        b.overlay.topInset = dp(170)
        b.overlay.bottomInset = dp(170)
        b.btnManual.setOnClickListener { manual() }
        if (Prefs.paired) b.status.text = "Зараз підключено: ${Prefs.userName} · ${Prefs.server.removePrefix("https://")}\nНовий QR — підключить заново."
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) startCamera()
        else permission.launch(Manifest.permission.CAMERA)
    }

    override fun onDestroy() {
        executor.shutdown()
        super.onDestroy()
    }

    private fun startCamera() {
        val f = ProcessCameraProvider.getInstance(this)
        f.addListener({
            val p = f.get()
            val preview = Preview.Builder().build().also { it.setSurfaceProvider(b.preview.surfaceProvider) }
            val analysis = ImageAnalysis.Builder().setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST).build()
                .also { it.setAnalyzer(executor, Qr.Analyzer(scanner) { codes -> codes.firstNotNullOfOrNull(::parse)?.let(::pair) }) }
            runCatching {
                p.unbindAll()
                p.bindToLifecycle(this, CameraSelector.DEFAULT_BACK_CAMERA, preview, analysis)
            }.onFailure { b.status.text = "Камера недоступна: ${it.message}" }
        }, ContextCompat.getMainExecutor(this))
    }

    /** Текст QR → (адреса, код) або null (чужий QR). */
    private fun parse(text: String): Pair<String, String>? {
        val j = runCatching { JSONObject(text) }.getOrNull() ?: return null
        if (j.optString("minerva") != "cards") return null
        val url = j.optString("url")
        val code = j.optString("code")
        return if (url.isNotEmpty() && code.isNotEmpty()) url to code else null
    }

    private fun pair(target: Pair<String, String>) {
        if (busy) return
        busy = true
        b.overlay.found = true
        b.status.text = "⏳ Підключаюсь до ${target.first.removePrefix("https://")}…"
        lifecycleScope.launch {
            try {
                val server = Api.checkServer(target.first)
                val j = Api.pair(server, target.second)
                val user = j.optJSONObject("user") ?: JSONObject()
                if (Prefs.server != server) {
                    Prefs.forget()       // інша Minerva — пресети, категорії й список звідти не беремо
                }
                Prefs.server = server
                Prefs.token = j.str("token")
                Prefs.userName = user.str("name")
                Prefs.userEmail = user.str("email")
                Prefs.needsPairing = false
                runCatching { Api.meta() }
                Sync.schedulePeriodic(this@PairActivity)
                Sync.uploadNow(this@PairActivity)
                toast("✅ Підключено: ${Prefs.userName}", true)
                finish()
            } catch (e: ApiException) {
                b.overlay.found = false
                b.status.text = "⚠️ ${e.message}\n\nПокажіть у Minerva новий QR і наведіть ще раз."
                busy = false
            }
        }
    }

    private fun manual() {
        val box = vbox()
        val (urlBox, url) = field("Адреса Minerva, напр. https://sevskiy.minerva-bi.com", Prefs.server)
        val (codeBox, code) = field("Код з QR (на сторінці «📱 Додаток на телефон»)")
        box.addView(urlBox)
        box.addView(codeBox)
        MaterialAlertDialogBuilder(this).setTitle("⌨ Підключити вручну").setView(box)
            .setPositiveButton("🔗 Підключити") { _, _ ->
                val text = code.text.toString().trim()
                val parsed = parse(text)
                pair(parsed ?: (url.text.toString().trim() to text))
            }
            .setNegativeButton("Скасувати", null).show()
    }
}
