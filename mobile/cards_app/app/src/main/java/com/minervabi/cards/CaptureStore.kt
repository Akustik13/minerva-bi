package com.minervabi.cards

import android.content.Context
import android.os.Handler
import android.os.Looper
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.UUID

/** Знімок на телефоні: фото, примітки, стан вивантаження в Minerva. */
data class Capture(
    val uuid: String = UUID.randomUUID().toString(),
    val createdAt: Long = System.currentTimeMillis(),
    var front: String = "",
    var back: String = "",
    var kind: String = "card",
    var role: String = "",
    var event: String = "",
    var note: String = "",
    var marks: List<String> = emptyList(),
    var categories: List<Int> = emptyList(),
    var presetId: Int? = null,
    var readAi: Boolean = true,
    var model: String = "",
    var qrFront: String = "",
    var qrBack: String = "",
    var status: String = PENDING,
    var serverId: Int = 0,
    var error: String = "",
    var attempts: Int = 0,
    var server: JSONObject? = null,
) {
    val card get() = server?.let { ServerCard(it) }
    val waiting get() = status == PENDING || status == UPLOADING

    fun toJson(): JSONObject = JSONObject()
        .put("uuid", uuid).put("created_at", createdAt).put("front", front).put("back", back).put("kind", kind)
        .put("role", role)
        .put("event", event).put("note", note).put("marks", JSONArray(marks)).put("categories", JSONArray(categories))
        .put("preset", presetId ?: JSONObject.NULL).put("read_ai", readAi).put("model", model)
        .put("qr_front", qrFront).put("qr_back", qrBack).put("status", status).put("server_id", serverId)
        .put("error", error).put("attempts", attempts).put("server", server ?: JSONObject.NULL)

    companion object {
        const val PENDING = "pending"        // ⏳ чекає інтернету
        const val UPLOADING = "uploading"    // ⬆ надсилається
        const val UPLOADED = "uploaded"      // ✅ у Minerva
        const val FAILED = "failed"          // ⚠️ Minerva не прийняла (не повторюється само)

        fun parse(o: JSONObject) = Capture(
            uuid = o.str("uuid"), createdAt = o.optLong("created_at"), front = o.str("front"), back = o.str("back"),
            kind = o.str("kind").ifEmpty { "card" }, role = o.str("role"), event = o.str("event"), note = o.str("note"),
            marks = o.optJSONArray("marks").strings(), categories = o.optJSONArray("categories").ints(),
            presetId = if (o.isNull("preset")) null else o.optInt("preset"), readAi = o.optBoolean("read_ai", true),
            model = o.str("model"), qrFront = o.str("qr_front"), qrBack = o.str("qr_back"),
            status = o.str("status").ifEmpty { PENDING }, serverId = o.optInt("server_id"), error = o.str("error"),
            attempts = o.optInt("attempts"), server = o.optJSONObject("server"),
        )
    }
}

/** Черга знімків — JSON-файл у пам'яті додатка (фото — у files/captures/). Без інтернету все чекає тут. */
object CaptureStore {
    private lateinit var file: File
    lateinit var dir: File
        private set
    private val items = mutableListOf<Capture>()
    private val listeners = mutableListOf<() -> Unit>()
    private val main = Handler(Looper.getMainLooper())

    fun init(context: Context) {
        dir = File(context.filesDir, "captures").apply { mkdirs() }
        file = File(context.filesDir, "captures.json")
        synchronized(this) {
            items.clear()
            if (file.exists()) {
                runCatching {
                    val arr = JSONArray(file.readText())
                    for (i in 0 until arr.length()) items.add(Capture.parse(arr.getJSONObject(i)))
                }
            }
            // перерване вивантаження (додаток закрили) — знову в чергу
            items.filter { it.status == Capture.UPLOADING }.forEach { it.status = Capture.PENDING }
        }
    }

    fun all(): List<Capture> = synchronized(this) { items.sortedByDescending { it.createdAt }.map { it.copy() } }

    fun get(uuid: String): Capture? = synchronized(this) { items.firstOrNull { it.uuid == uuid }?.copy() }

    fun pending(): List<Capture> = synchronized(this) {
        items.filter { it.status == Capture.PENDING }.sortedBy { it.createdAt }.map { it.copy() }
    }

    fun counts(): Map<String, Int> = synchronized(this) {
        mapOf(
            "waiting" to items.count { it.waiting },
            "failed" to items.count { it.status == Capture.FAILED },
            "reading" to items.count { it.status == Capture.UPLOADED && it.card?.reading == true },
            "done" to items.count { it.status == Capture.UPLOADED },
        )
    }

    fun add(c: Capture) {
        synchronized(this) { items.add(c); save() }
        changed()
    }

    fun update(uuid: String, block: (Capture) -> Unit) {
        synchronized(this) {
            items.firstOrNull { it.uuid == uuid }?.let(block)
            save()
        }
        changed()
    }

    fun remove(uuid: String) {
        synchronized(this) {
            items.firstOrNull { it.uuid == uuid }?.let { c ->
                listOf(c.front, c.back).filter { it.isNotEmpty() }.forEach { File(it).delete() }
                items.remove(c)
            }
            save()
        }
        changed()
    }

    /** Надіслані давно — прибрати з телефона (у Minerva вони лишаються). */
    fun clearUploaded(): Int {
        val gone = synchronized(this) { items.filter { it.status == Capture.UPLOADED }.map { it.uuid } }
        gone.forEach { remove(it) }
        return gone.size
    }

    private fun save() {
        val arr = JSONArray()
        items.forEach { arr.put(it.toJson()) }
        val tmp = File(file.parentFile, file.name + ".part")
        tmp.writeText(arr.toString())
        if (!tmp.renameTo(file)) {
            file.writeText(arr.toString())
            tmp.delete()
        }
    }

    fun listen(l: () -> Unit) {
        synchronized(listeners) { listeners.add(l) }
    }

    fun unlisten(l: () -> Unit) {
        synchronized(listeners) { listeners.remove(l) }
    }

    private fun changed() {
        val copy = synchronized(listeners) { listeners.toList() }
        main.post { copy.forEach { it() } }
    }

    fun newFile(side: String): File = File(dir, "${System.currentTimeMillis()}-$side-${UUID.randomUUID().toString().take(8)}.jpg")
}
