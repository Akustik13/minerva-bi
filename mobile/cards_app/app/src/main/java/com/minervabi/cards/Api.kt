package com.minervabi.cards

import android.os.Build
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.FormBody
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit

/** Minerva відповіла помилкою (status) або зв'язку немає (status = 0). */
class ApiException(val status: Int, message: String) : Exception(message) {
    val offline get() = status == 0
    val unauthorized get() = status == 401
}

/** JSON API Minerva для додатка (suppliers/app_api.py): ключ телефону в «Authorization: Bearer …». */
object Api {
    private const val BASE = "/suppliers/app/api/"

    val http: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .writeTimeout(120, TimeUnit.SECONDS)
        .build()

    fun deviceName(): String =
        listOf(Build.MANUFACTURER.replaceFirstChar { it.uppercase() }, Build.MODEL).distinct().joinToString(" ")

    /** https:// обов'язково; http — лише емулятор / цей телефон (розробка). */
    fun checkServer(url: String): String {
        val u = url.trim().trimEnd('/').toHttpUrlOrNull() ?: throw ApiException(400, "Це не адреса Minerva: $url")
        if (u.scheme != "https" && u.host !in setOf("10.0.2.2", "localhost", "127.0.0.1")) {
            throw ApiException(400, "Minerva має бути з https:// — інакше ключ ішов би відкритим текстом.")
        }
        return u.toString().trimEnd('/')
    }

    private fun url(path: String, server: String = Prefs.server) = server.trimEnd('/') + BASE + path

    private fun request(path: String, server: String = Prefs.server): Request.Builder {
        val b = Request.Builder().url(url(path, server))
            .header("User-Agent", "MinervaCards/${BuildConfig.VERSION_NAME} (Android ${Build.VERSION.RELEASE})")
        if (Prefs.token.isNotEmpty()) b.header("Authorization", "Bearer ${Prefs.token}")
        return b
    }

    private fun run(req: Request): JSONObject {
        val resp: Response = try {
            http.newCall(req).execute()
        } catch (e: IOException) {
            throw ApiException(0, "Немає зв'язку з Minerva")
        }
        resp.use { r ->
            val text = r.body?.string().orEmpty()
            val json = runCatching { JSONObject(text) }.getOrNull()
            if (!r.isSuccessful) {
                if (r.code == 401) Prefs.needsPairing = true
                val msg = json?.str("error")?.ifEmpty { null } ?: "Minerva відповіла ${r.code}"
                throw ApiException(r.code, msg)
            }
            if (json == null) throw ApiException(r.code, "Незрозуміла відповідь Minerva (це точно адреса Minerva?)")
            return json
        }
    }

    suspend fun get(path: String, params: Map<String, String> = emptyMap()): JSONObject = withContext(Dispatchers.IO) {
        val base = url(path).toHttpUrlOrNull() ?: throw ApiException(400, "Неправильна адреса Minerva")
        val u = base.newBuilder().apply { params.forEach { (k, v) -> if (v.isNotEmpty()) addQueryParameter(k, v) } }.build()
        run(request(path).url(u).get().build())
    }

    suspend fun postJson(path: String, body: JSONObject, server: String = Prefs.server): JSONObject =
        withContext(Dispatchers.IO) {
            run(request(path, server).post(body.toString().toRequestBody("application/json".toMediaType())).build())
        }

    suspend fun postForm(path: String, fields: Map<String, String>): JSONObject = withContext(Dispatchers.IO) {
        val b = FormBody.Builder()
        fields.forEach { (k, v) -> b.add(k, v) }
        run(request(path).post(b.build()).build())
    }

    suspend fun pair(server: String, code: String): JSONObject {
        val body = JSONObject().put("code", code).put("device", deviceName()).put("version", BuildConfig.VERSION_NAME)
        val saved = Prefs.token
        Prefs.token = ""          // підключення — без старого ключа
        return try {
            postJson("pair/", body, server)
        } catch (e: ApiException) {
            Prefs.token = saved
            throw e
        }
    }

    suspend fun meta(): Meta {
        val j = get("meta/")
        Prefs.metaJson = j.toString()
        return Meta.parse(j)
    }

    /** Знімок → нова візитка в Minerva (multipart). Той самий uuid удруге — та сама візитка (created = false). */
    fun upload(c: Capture): JSONObject {
        val b = MultipartBody.Builder().setType(MultipartBody.FORM)
        val jpeg = "image/jpeg".toMediaType()
        b.addFormDataPart("image", "front.jpg", File(c.front).asRequestBody(jpeg))
        if (c.back.isNotEmpty() && File(c.back).exists()) {
            b.addFormDataPart("image_back", "back.jpg", File(c.back).asRequestBody(jpeg))
        }
        val fields = mapOf(
            "uuid" to c.uuid, "kind" to c.kind, "role" to c.role, "event" to c.event, "note" to c.note,
            "marks" to c.marks.joinToString(","), "categories" to c.categories.joinToString(","),
            // є ще фото групи — AI прочитає все разом, коли їх дошлемо (Capture.reread)
            "preset" to (c.presetId?.toString() ?: ""), "read_ai" to (if (c.readAi && c.extras.isEmpty()) "1" else ""),
            "model" to c.model, "qr_front" to c.qrFront, "qr_back" to c.qrBack,
        )
        fields.forEach { (k, v) -> if (v.isNotEmpty()) b.addFormDataPart(k, v) }
        val body: RequestBody = b.build()
        return run(request("cards/").post(body).build())
    }

    /** 🔄 Новий лицьовий / зворотний бік уже надісланої візитки. */
    fun uploadSide(id: Int, side: String, path: String, qr: String): ServerCard {
        val b = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("side", side)
            .addFormDataPart("image", "$side.jpg", File(path).asRequestBody("image/jpeg".toMediaType()))
        if (qr.isNotEmpty()) b.addFormDataPart("qr", qr)
        return ServerCard(run(request("cards/$id/side/").post(b.build()).build()).getJSONObject("card"))
    }

    /** 🗑 Прибрати зворотний бік у Minerva. */
    fun dropBack(id: Int): ServerCard {
        val body = FormBody.Builder().add("side", "back").add("delete", "1").build()
        return ServerCard(run(request("cards/$id/side/").post(body).build()).getJSONObject("card"))
    }

    /** 📎 Ще фото групи (той самий uuid удруге — те саме фото). */
    fun addPhoto(id: Int, e: Extra): ServerCard {
        val b = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("image", "photo.jpg", File(e.path).asRequestBody("image/jpeg".toMediaType()))
            .addFormDataPart("uuid", e.uuid).addFormDataPart("kind", e.kind)
        if (e.note.isNotEmpty()) b.addFormDataPart("note", e.note)
        if (e.qr.isNotEmpty()) b.addFormDataPart("qr", e.qr)
        return ServerCard(run(request("cards/$id/photos/").post(b.build()).build()).getJSONObject("card"))
    }

    /** 🗑 Прибрати ще фото в Minerva (уже немає — теж добре). → свіжа візитка або null. */
    fun deletePhoto(id: Int, photoId: Int): ServerCard? {
        val j = run(request("cards/$id/photos/$photoId/delete/").post(FormBody.Builder().build()).build())
        return j.optJSONObject("card")?.let { ServerCard(it) }
    }

    /** 🧠 AI заново (без корутини — для UploadWorker). */
    fun readNow(id: Int, model: String): ServerCard =
        ServerCard(run(request("cards/$id/read/").post(JSONObject().put("model", model).toString()
            .toRequestBody("application/json".toMediaType())).build()).getJSONObject("card"))

    suspend fun cards(params: Map<String, String>): Pair<List<ServerCard>, Boolean> {
        val j = get("cards/", params)
        val arr = j.optJSONArray("items")
        val list = if (arr == null) emptyList() else (0 until arr.length()).map { ServerCard(arr.getJSONObject(it)) }
        return list to j.optBoolean("more")
    }

    suspend fun card(id: Int): ServerCard = ServerCard(get("cards/$id/").getJSONObject("card"))

    suspend fun updateCard(id: Int, body: JSONObject): ServerCard =
        ServerCard(postJson("cards/$id/", body).getJSONObject("card"))

    suspend fun readCard(id: Int, model: String): ServerCard =
        ServerCard(postJson("cards/$id/read/", JSONObject().put("model", model)).getJSONObject("card"))

    suspend fun toSupplier(id: Int): JSONObject = postJson("cards/$id/to-supplier/", JSONObject())

    suspend fun toCustomer(id: Int): JSONObject = postJson("cards/$id/to-customer/", JSONObject())

    suspend fun deleteCard(id: Int): JSONObject = postJson("cards/$id/delete/", JSONObject())

    suspend fun saveCategory(id: Int?, body: JSONObject): JSONObject =
        postJson(if (id == null) "categories/" else "categories/$id/", body)

    suspend fun savePreset(body: JSONObject): JSONObject = postJson("presets/", body)

    /** Фото візитки (мініатюра або повне) — з ключем телефону. */
    fun image(id: Int, side: String, full: Boolean): ByteArray? {
        val u = url("cards/$id/image/").toHttpUrlOrNull()?.newBuilder()
            ?.addQueryParameter("side", side)?.addQueryParameter("size", if (full) "full" else "thumb")?.build()
            ?: return null
        return try {
            http.newCall(request("cards/$id/image/").url(u).get().build()).execute().use { r ->
                if (r.isSuccessful) r.body?.bytes() else null
            }
        } catch (e: IOException) {
            null
        }
    }
}
