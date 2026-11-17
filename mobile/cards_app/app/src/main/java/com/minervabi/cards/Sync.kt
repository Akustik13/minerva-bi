package com.minervabi.cards

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/** Вивантаження знімків: WorkManager сам чекає інтернету (або Wi-Fi) і переживає закриття додатка / перезапуск. */
object Sync {
    private const val NOW = "minerva-upload"
    private const val PERIODIC = "minerva-upload-periodic"
    /** Один вивантажувач одночасно (разовий і періодичний не надсилають те саме двічі). */
    val running = AtomicBoolean(false)

    private fun constraints() = Constraints.Builder()
        .setRequiredNetworkType(if (Prefs.wifiOnly) NetworkType.UNMETERED else NetworkType.CONNECTED)
        .build()

    fun uploadNow(context: Context) {
        if (!Prefs.paired) return
        val req = OneTimeWorkRequestBuilder<UploadWorker>()
            .setConstraints(constraints())
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork(NOW, ExistingWorkPolicy.APPEND_OR_REPLACE, req)
    }

    fun schedulePeriodic(context: Context) {
        val req = PeriodicWorkRequestBuilder<UploadWorker>(15, TimeUnit.MINUTES)
            .setConstraints(constraints())
            .build()
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(PERIODIC, ExistingPeriodicWorkPolicy.UPDATE, req)
    }

    /** Оновити з Minerva стан уже надісланих (AI прочитав? внесли в постачальники? додали фото на сторінці?). */
    suspend fun refreshStatuses(): Int {
        val ids = CaptureStore.all().filter { it.status == Capture.UPLOADED && it.serverId > 0 }
            .take(150).map { it.serverId }
        if (ids.isEmpty()) return 0
        val (cards, _) = Api.cards(mapOf("ids" to ids.joinToString(",")))
        val byId = cards.associateBy { it.id }
        CaptureStore.all().filter { it.status == Capture.UPLOADED && it.serverId in byId }.forEach { c ->
            CaptureStore.update(c.uuid) { CaptureStore.mergeServer(it, byId[c.serverId]?.json) }
        }
        return cards.size
    }
}

class UploadWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    private enum class Outcome { OK, OFFLINE, UNAUTHORIZED }

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        if (!Prefs.paired || Prefs.needsPairing) return@withContext Result.success()
        if (!Sync.running.compareAndSet(false, true)) return@withContext Result.success()   // уже надсилає інший
        try {
            work()
        } finally {
            Sync.running.set(false)
        }
    }

    private suspend fun work(): Result {
        var guard = 0
        // поки є що надсилати: нові знімки, зроблені під час вивантаження, і зміни фото надісланих
        while (guard++ < 500) {
            val next = CaptureStore.pending().firstOrNull()
            val outcome = if (next != null) upload(next) else {
                val push = CaptureStore.toPush().firstOrNull() ?: break
                push(push)
            }
            when (outcome) {
                Outcome.OFFLINE -> return Result.retry()
                Outcome.UNAUTHORIZED -> return Result.success()        // підключіть знову — тоді надішле
                Outcome.OK -> Unit
            }
        }
        runCatching { Sync.refreshStatuses() }
        return Result.success()
    }

    /** Помилка Minerva → що робити: немає зв'язку / 5xx — пізніше, 401 — підключити знову, інше — показати людині. */
    private fun outcomeOf(e: ApiException): Outcome? = when {
        e.offline || e.status >= 500 || e.status == 429 -> Outcome.OFFLINE
        e.unauthorized -> Outcome.UNAUTHORIZED
        else -> null
    }

    private fun upload(next: Capture): Outcome {
        if (!File(next.front).exists()) {
            CaptureStore.update(next.uuid) { it.status = Capture.FAILED; it.error = "Фото зникло з телефона" }
            return Outcome.OK
        }
        CaptureStore.update(next.uuid) { it.status = Capture.UPLOADING; it.attempts += 1 }
        try {
            val j = Api.upload(next)
            val card = j.optJSONObject("card") ?: JSONObject()
            CaptureStore.update(next.uuid) {
                it.status = Capture.UPLOADED; it.serverId = card.optInt("id"); it.error = ""
                CaptureStore.mergeServer(it, card)
                if (it.extras.isNotEmpty() && it.readAi) it.reread = true     // AI — коли дійдуть ще фото групи
            }
            return Outcome.OK
        } catch (e: ApiException) {
            val o = outcomeOf(e)
            CaptureStore.update(next.uuid) {
                it.status = if (o == null) Capture.FAILED else Capture.PENDING; it.error = e.message ?: ""
            }
            return o ?: Outcome.OK
        }
    }

    /** Надісланий знімок: перезняті боки, прибраний зворотний, ще фото, прибрані фото → Minerva; потім AI раз. */
    private fun push(c: Capture): Outcome {
        val id = c.serverId
        fun saved(card: ServerCard?, block: (Capture) -> Unit) {
            CaptureStore.update(c.uuid) { cur ->
                block(cur)
                if (card != null) CaptureStore.mergeServer(cur, card.json)
            }
            ImageLoader.forgetServer(applicationContext, id)
        }
        try {
            if (c.frontNew) {
                if (File(c.front).exists()) {
                    val card = Api.uploadSide(id, "front", c.front, c.qrFront)
                    saved(card) { if (it.front == c.front) it.frontNew = false }
                } else saved(null) { it.frontNew = false }
            }
            if (c.backGone) saved(Api.dropBack(id)) { it.backGone = false }
            if (c.backNew) {
                if (File(c.back).exists()) {
                    val card = Api.uploadSide(id, "back", c.back, c.qrBack)
                    saved(card) { if (it.back == c.back) it.backNew = false }
                } else saved(null) { it.backNew = false }
            }
            for (pid in c.dropPhotos) {
                val card = Api.deletePhoto(id, pid)
                saved(card) { it.dropPhotos = it.dropPhotos - pid }
            }
            for (e in c.extras.filter { it.serverId == 0 }) {
                if (!File(e.path).exists()) {
                    saved(null) { it.extras = it.extras.filter { x -> x.uuid != e.uuid } }
                    continue
                }
                val card = Api.addPhoto(id, e)
                saved(card) { }
            }
            val now = CaptureStore.get(c.uuid) ?: return Outcome.OK
            if (now.reread && !(now.frontNew || now.backNew || now.backGone || now.dropPhotos.isNotEmpty() ||
                    now.extras.any { it.serverId == 0 })) {
                CaptureStore.update(c.uuid) { it.reread = false }
                try {
                    val card = Api.readNow(id, now.model.ifEmpty { Prefs.model })
                    saved(card) { }
                } catch (e: ApiException) {
                    if (outcomeOf(e) == Outcome.OFFLINE) CaptureStore.update(c.uuid) { it.reread = true }
                    else if (e.status != 403) throw e                  // без права на AI — фото все одно в Minerva
                }
            }
            return Outcome.OK
        } catch (e: ApiException) {
            val o = outcomeOf(e)
            if (o != null) return o
            if (e.status == 404) {                                     // візитку видалили в Minerva
                CaptureStore.update(c.uuid) { it.pushError = "Візитки вже немає в Minerva" }
            } else CaptureStore.update(c.uuid) { it.pushError = e.message ?: "Minerva не прийняла фото" }
            return Outcome.OK
        }
    }
}
