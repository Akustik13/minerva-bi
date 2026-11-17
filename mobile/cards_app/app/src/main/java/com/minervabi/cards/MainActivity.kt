package com.minervabi.cards

import android.content.Intent
import android.os.Bundle
import android.view.KeyEvent
import androidx.appcompat.app.AppCompatActivity
import androidx.fragment.app.Fragment
import androidx.lifecycle.lifecycleScope
import com.minervabi.cards.databinding.ActivityMainBinding
import kotlinx.coroutines.launch

/** Чотири вкладки внизу: 📷 Знімати · 📤 Мої знімки · 🪪 У Minerva · ⚙ Налаштування. Камера — за замовчуванням. */
class MainActivity : AppCompatActivity() {
    private lateinit var b: ActivityMainBinding
    private val tabs = mutableMapOf<Int, Fragment>()
    private var current = R.id.nav_capture
    private val badgeListener: () -> Unit = { badge() }
    private var syncing = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivityMainBinding.inflate(layoutInflater)
        setContentView(b.root)
        listOf(R.id.nav_capture to "capture", R.id.nav_queue to "queue", R.id.nav_browse to "browse",
            R.id.nav_settings to "settings").forEach { (id, tag) ->
            supportFragmentManager.findFragmentByTag(tag)?.let { tabs[id] = it }
        }
        b.nav.setOnItemSelectedListener { if (!syncing) select(it.itemId); true }
        current = savedInstanceState?.getInt("tab") ?: R.id.nav_capture
        b.nav.selectedItemId = current
        select(current)
        if (!Prefs.paired && savedInstanceState == null) startActivity(Intent(this, PairActivity::class.java))
        CaptureStore.listen(badgeListener)
        badge()
        if (savedInstanceState == null) handle(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handle(intent)
    }

    /** Картка візитки → «📷 Фото…»: вікно приміток цього знімка (фото, зворотний бік, ще фото). */
    private fun handle(intent: Intent?) {
        val uuid = intent?.getStringExtra(EXTRA_REVIEW) ?: return
        intent.removeExtra(EXTRA_REVIEW)
        select(R.id.nav_capture)
        (tabs[R.id.nav_capture] as? CaptureFragment)?.openReview(uuid, intent.getIntExtra(EXTRA_RETURN, R.id.nav_browse))
    }

    val currentTab get() = current

    /** З вікна приміток: зняти фото для цього знімка — камера, потім знову вікно. */
    fun shootFor(t: ShotTarget) {
        select(R.id.nav_capture)
        (tabs[R.id.nav_capture] as? CaptureFragment)?.shootFor(t)
    }

    override fun onDestroy() {
        CaptureStore.unlisten(badgeListener)
        super.onDestroy()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putInt("tab", current)
    }

    override fun onResume() {
        super.onResume()
        if (Prefs.paired) {
            lifecycleScope.launch { runCatching { Api.meta() } }
            Sync.uploadNow(this)
        }
        badge()
    }

    fun select(id: Int) {
        current = id
        val tx = supportFragmentManager.beginTransaction()
        val tag = when (id) {
            R.id.nav_queue -> "queue"; R.id.nav_browse -> "browse"; R.id.nav_settings -> "settings"; else -> "capture"
        }
        val f = tabs.getOrPut(id) {
            when (id) {
                R.id.nav_queue -> QueueFragment()
                R.id.nav_browse -> BrowseFragment()
                R.id.nav_settings -> SettingsFragment()
                else -> CaptureFragment()
            }.also { tx.add(R.id.host, it, tag) }
        }
        tabs.forEach { (k, frag) -> if (k == id) tx.show(frag) else tx.hide(frag) }
        tx.commitNowAllowingStateLoss()
        if (b.nav.selectedItemId != id) {
            syncing = true
            b.nav.selectedItemId = id
            syncing = false
        }
    }

    private fun badge() {
        val n = CaptureStore.counts()["waiting"] ?: 0
        val bd = b.nav.getOrCreateBadge(R.id.nav_queue)
        bd.isVisible = n > 0
        bd.number = n
    }

    companion object {
        const val EXTRA_REVIEW = "review_uuid"
        const val EXTRA_RETURN = "return_tab"

        /** Відкрити вікно приміток знімка з іншого екрана (картка візитки). */
        fun review(context: android.content.Context, uuid: String) {
            context.startActivity(Intent(context, MainActivity::class.java).putExtra(EXTRA_REVIEW, uuid)
                .putExtra(EXTRA_RETURN, R.id.nav_browse)
                .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP))
        }
    }

    /** Кнопка гучності — теж знімок (зручно однією рукою). */
    override fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean {
        if ((keyCode == KeyEvent.KEYCODE_VOLUME_DOWN || keyCode == KeyEvent.KEYCODE_VOLUME_UP) && current == R.id.nav_capture) {
            (tabs[R.id.nav_capture] as? CaptureFragment)?.shoot()
            return true
        }
        return super.onKeyDown(keyCode, event)
    }
}
