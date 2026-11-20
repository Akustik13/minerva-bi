package com.minervabi.cards

import android.content.Context
import android.os.Handler
import android.os.Looper
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.UUID

/** 📎 Ще фото до знімка: друга візитка тієї самої фірми, флаєр, каталог, продукт, стенд. serverId > 0 — уже в Minerva. */
data class Extra(
    val uuid: String = UUID.randomUUID().toString(),
    val path: String = "",
    val kind: String = "card",
    val serverId: Int = 0,
    val note: String = "",
    val qr: String = "",
) {
    fun toJson(): JSONObject = JSONObject().put("uuid", uuid).put("path", path).put("kind", kind)
        .put("server_id", serverId).put("note", note).put("qr", qr)

    companion object {
        fun parse(o: JSONObject) = Extra(o.str("uuid").ifEmpty { UUID.randomUUID().toString() }, o.str("path"),
            o.str("kind").ifEmpty { "card" }, o.optInt("server_id"), o.str("note"), o.str("qr"))

        fun list(arr: JSONArray?): List<Extra> =
            if (arr == null) emptyList() else (0 until arr.length()).mapNotNull { arr.optJSONObject(it) }.map(::parse)
    }
}

/**
 * Знімок на телефоні: фото (лицьовий, зворотний бік, ще фото групи), примітки, стан вивантаження в Minerva.
 * Уже надісланий знімок пам'ятає, що змінили у фото (frontNew / backNew / backGone / dropPhotos / нові extras) —
 * UploadWorker дошле це в Minerva, коли буде інтернет, і попросить AI прочитати групу заново (reread).
 */
data class Capture(
    val uuid: String = UUID.randomUUID().toString(),
    val createdAt: Long = System.currentTimeMillis(),
    var front: String = "",
    var back: String = "",
    var frontAlt: String = "",        // 📐 той самий бік «по рамці» (лише чернетка)
    var backAlt: String = "",
    var extras: List<Extra> = emptyList(),
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
    var frontNew: Boolean = false,    // надісланий: лицьовий бік перезняли
    var backNew: Boolean = false,     // зворотний бік дознімали / перезняли
    var backGone: Boolean = false,    // зворотний бік прибрали
    var dropPhotos: List<Int> = emptyList(),   // ще фото, які прибрали (id у Minerva)
    var reread: Boolean = false,      // після змін фото — AI прочитати групу заново
    var pushError: String = "",       // Minerva не прийняла зміну фото (не повторюється само)
    var adopted: Boolean = false,     // візитка з «🪪 У Minerva», відкрита для правки фото (не моя зйомка)
    var visibility: String = "team",  // 👁 у Minerva: team — бачать усі, private — лише я
    var held: Boolean = false,        // 📱 знято без підключення — у Minerva лише після «⬆ Надіслати» людини
) {
    val card get() = server?.let { ServerCard(it) }
    val isDraft get() = status == DRAFT
    /** Надісланий знімок, а зміни фото ще не в Minerva. */
    val dirty get() = status == UPLOADED && (frontNew || backNew || backGone || dropPhotos.isNotEmpty() ||
        extras.any { it.serverId == 0 } || reread)
    val waiting get() = (status == PENDING && !held) || status == UPLOADING || status == DRAFT ||
        (dirty && pushError.isEmpty())
    /** 📱 Лише на телефоні: знято без Minerva, ще не вирішили надсилати. */
    val local get() = status == PENDING && held
    val failed get() = status == FAILED || (dirty && pushError.isNotEmpty())
    /** Є зворотний бік: свій файл або в Minerva (і його не прибрали). */
    val hasBack get() = back.isNotEmpty() || (!backGone && card?.hasBack == true)

    fun toJson(): JSONObject = JSONObject()
        .put("uuid", uuid).put("created_at", createdAt).put("front", front).put("back", back).put("kind", kind)
        .put("front_alt", frontAlt).put("back_alt", backAlt).put("extras", JSONArray(extras.map { it.toJson() }))
        .put("role", role)
        .put("event", event).put("note", note).put("marks", JSONArray(marks)).put("categories", JSONArray(categories))
        .put("preset", presetId ?: JSONObject.NULL).put("read_ai", readAi).put("model", model)
        .put("qr_front", qrFront).put("qr_back", qrBack).put("status", status).put("server_id", serverId)
        .put("error", error).put("attempts", attempts).put("server", server ?: JSONObject.NULL)
        .put("front_new", frontNew).put("back_new", backNew).put("back_gone", backGone)
        .put("drop_photos", JSONArray(dropPhotos)).put("reread", reread).put("push_error", pushError)
        .put("adopted", adopted).put("visibility", visibility).put("held", held)

    companion object {
        const val DRAFT = "draft"            // ✏️ щойно знято, вікно приміток ще не закрили
        const val PENDING = "pending"        // ⏳ чекає інтернету
        const val UPLOADING = "uploading"    // ⬆ надсилається
        const val UPLOADED = "uploaded"      // ✅ у Minerva
        const val FAILED = "failed"          // ⚠️ Minerva не прийняла (не повторюється само)

        fun parse(o: JSONObject) = Capture(
            uuid = o.str("uuid"), createdAt = o.optLong("created_at"), front = o.str("front"), back = o.str("back"),
            frontAlt = o.str("front_alt"), backAlt = o.str("back_alt"), extras = Extra.list(o.optJSONArray("extras")),
            kind = o.str("kind").ifEmpty { "card" }, role = o.str("role"), event = o.str("event"), note = o.str("note"),
            marks = o.optJSONArray("marks").strings(), categories = o.optJSONArray("categories").ints(),
            presetId = if (o.isNull("preset")) null else o.optInt("preset"), readAi = o.optBoolean("read_ai", true),
            model = o.str("model"), qrFront = o.str("qr_front"), qrBack = o.str("qr_back"),
            status = o.str("status").ifEmpty { PENDING }, serverId = o.optInt("server_id"), error = o.str("error"),
            attempts = o.optInt("attempts"), server = o.optJSONObject("server"),
            frontNew = o.optBoolean("front_new"), backNew = o.optBoolean("back_new"),
            backGone = o.optBoolean("back_gone"), dropPhotos = o.optJSONArray("drop_photos").ints(),
            reread = o.optBoolean("reread"), pushError = o.str("push_error"), adopted = o.optBoolean("adopted"),
            visibility = o.str("visibility").ifEmpty { "team" }, held = o.optBoolean("held"),
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

    /** Для «📤 Мої знімки»: без відкритих з «🪪 У Minerva», у яких нічого не змінено. */
    fun mine(): List<Capture> = all().filter { !it.adopted || it.dirty || it.failed }

    fun get(uuid: String): Capture? = synchronized(this) { items.firstOrNull { it.uuid == uuid }?.copy() }

    fun byServer(id: Int): Capture? = synchronized(this) { items.firstOrNull { it.serverId == id }?.copy() }

    fun pending(): List<Capture> = synchronized(this) {
        items.filter { it.status == Capture.PENDING && !it.held }.sortedBy { it.createdAt }.map { it.copy() }
    }

    /** 📱 Зняті без підключення — ще чекають рішення «⬆ Надіслати в Minerva». */
    fun local(): List<Capture> = synchronized(this) { items.filter { it.local }.sortedBy { it.createdAt }.map { it.copy() } }

    /** ⬆ Надіслати в Minerva ці (зняті без підключення). */
    fun release(uuids: Collection<String>) {
        val set = uuids.toSet()
        synchronized(this) {
            items.filter { it.uuid in set }.forEach { it.held = false }
            save()
        }
        changed()
    }

    /** Надіслані, у яких змінили фото, — дослати в Minerva. */
    fun toPush(): List<Capture> = synchronized(this) {
        items.filter { it.dirty && it.pushError.isEmpty() }.sortedBy { it.createdAt }.map { it.copy() }
    }

    fun counts(): Map<String, Int> = synchronized(this) {
        val shown = items.filter { !it.adopted || it.dirty || it.failed }
        mapOf(
            "waiting" to shown.count { it.waiting },
            "local" to shown.count { it.local },
            "failed" to shown.count { it.failed },
            "reading" to shown.count { it.status == Capture.UPLOADED && it.card?.reading == true },
            "done" to shown.count { it.status == Capture.UPLOADED },
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
                (listOf(c.front, c.back, c.frontAlt, c.backAlt) + c.extras.map { it.path })
                    .filter { it.isNotEmpty() }.forEach { File(it).delete() }
                items.remove(c)
            }
            save()
        }
        changed()
    }

    /** Надіслані давно — прибрати з телефона (у Minerva вони лишаються); зі змінами, ще не досланими, — ні. */
    fun clearUploaded(): Int {
        val gone = synchronized(this) {
            items.filter { it.status == Capture.UPLOADED && !it.dirty }.map { it.uuid }
        }
        gone.forEach { remove(it) }
        return gone.size
    }

    /** ✏️ Щойно знято — чернетка (вікно приміток ще відкрите): значення — з пресету. */
    fun newDraft(front: String, back: String, frontAlt: String, backAlt: String, qrFront: String, qrBack: String): Capture {
        val meta = Prefs.meta()
        val preset = meta.preset(Prefs.presetId?.takeIf { it > 0 } ?: if (Prefs.presetId == null) meta.defaultPreset else null)
        val c = Capture(
            front = front, back = back, frontAlt = frontAlt, backAlt = backAlt, qrFront = qrFront, qrBack = qrBack,
            status = Capture.DRAFT, kind = preset?.kind ?: Prefs.kind, role = preset?.role.orEmpty(),
            event = preset?.event.orEmpty(), note = preset?.note.orEmpty(), marks = preset?.marks.orEmpty(),
            categories = preset?.categories.orEmpty(), presetId = preset?.id,
            readAi = preset?.readAi ?: Prefs.readAiDefault, model = preset?.model?.ifEmpty { null } ?: Prefs.model,
            visibility = Prefs.visibility,
        )
        add(c)
        return c
    }

    /** Візитка з Minerva → знімок для правки фото (є свій — він). */
    fun adopt(card: ServerCard): Capture {
        byServer(card.id)?.let { c ->
            update(c.uuid) { mergeServer(it, card.json) }
            return get(c.uuid)!!
        }
        val c = Capture(
            front = "", back = "", kind = card.kind.ifEmpty { "card" }, role = card.role, event = card.event,
            note = card.note, marks = card.marks, categories = card.ownCategories, readAi = false,
            visibility = card.visibility,
            status = Capture.UPLOADED, serverId = card.id, adopted = true,
            createdAt = System.currentTimeMillis(),
        )
        mergeServer(c, card.json)
        add(c)
        return c
    }

    /**
     * Відповідь Minerva → знімок: картка й ще фото, які додали / прибрали в Minerva (на сторінці фото). Свої ще не
     * надіслані й ті, що чекають видалення, не чіпає.
     */
    fun mergeServer(c: Capture, json: JSONObject?) {
        if (json == null) return
        c.server = json
        val photos = ServerCard(json).photos
        val ids = photos.map { it.id }.toSet()
        val byUuid = photos.filter { it.uuid.isNotEmpty() }.associateBy { it.uuid }
        val mapped = c.extras.mapNotNull { e ->
            val sent = if (e.serverId == 0) byUuid[e.uuid] else null
            when {
                sent != null -> e.copy(serverId = sent.id)            // уже дійшло (відповідь загубилась)
                e.serverId > 0 && e.serverId !in ids && e.serverId !in c.dropPhotos -> {
                    if (e.path.isNotEmpty()) File(e.path).delete()
                    null                                               // прибрали в Minerva
                }
                else -> e
            }
        }
        val have = mapped.map { it.serverId }.toSet()
        c.extras = mapped + photos.filter { it.id !in have && it.id !in c.dropPhotos }
            .map { Extra(uuid = it.uuid.ifEmpty { "s${it.id}" }, kind = it.kind, serverId = it.id, note = it.note) }
        c.dropPhotos = c.dropPhotos.filter { it in ids }
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
