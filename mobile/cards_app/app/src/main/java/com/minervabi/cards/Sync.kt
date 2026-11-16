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

/** Вивантаження знімків: WorkManager сам чекає інтернету (або Wi-Fi) і переживає закриття додатка / перезапуск. */
object Sync {
    private const val NOW = "minerva-upload"
    private const val PERIODIC = "minerva-upload-periodic"

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

    /** Оновити з Minerva стан уже надісланих (AI прочитав? внесли в постачальники?). */
    suspend fun refreshStatuses(): Int {
        val ids = CaptureStore.all().filter { it.status == Capture.UPLOADED && it.serverId > 0 }
            .take(150).map { it.serverId }
        if (ids.isEmpty()) return 0
        val (cards, _) = Api.cards(mapOf("ids" to ids.joinToString(",")))
        val byId = cards.associateBy { it.id }
        CaptureStore.all().filter { it.status == Capture.UPLOADED && it.serverId in byId }.forEach { c ->
            CaptureStore.update(c.uuid) { it.server = byId[c.serverId]?.json }
        }
        return cards.size
    }
}

class UploadWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        if (!Prefs.paired || Prefs.needsPairing) return@withContext Result.success()
        var offline = false
        // поки є що надсилати: нові знімки, зроблені під час вивантаження, теж підуть
        var guard = 0
        while (guard++ < 500) {
            val next = CaptureStore.pending().firstOrNull() ?: break
            if (!File(next.front).exists()) {
                CaptureStore.update(next.uuid) { it.status = Capture.FAILED; it.error = "Фото зникло з телефона" }
                continue
            }
            CaptureStore.update(next.uuid) { it.status = Capture.UPLOADING; it.attempts += 1 }
            try {
                val j = Api.upload(next)
                val card = j.optJSONObject("card") ?: JSONObject()
                CaptureStore.update(next.uuid) {
                    it.status = Capture.UPLOADED; it.serverId = card.optInt("id"); it.server = card; it.error = ""
                }
            } catch (e: ApiException) {
                when {
                    e.offline || e.status >= 500 || e.status == 429 -> {
                        CaptureStore.update(next.uuid) { it.status = Capture.PENDING; it.error = e.message ?: "" }
                        offline = true
                    }
                    e.unauthorized -> {
                        CaptureStore.update(next.uuid) { it.status = Capture.PENDING; it.error = e.message ?: "" }
                        return@withContext Result.success()      // підключіть знову — тоді надішле
                    }
                    else -> CaptureStore.update(next.uuid) { it.status = Capture.FAILED; it.error = e.message ?: "" }
                }
            }
            if (offline) return@withContext Result.retry()
        }
        runCatching { Sync.refreshStatuses() }
        Result.success()
    }
}
