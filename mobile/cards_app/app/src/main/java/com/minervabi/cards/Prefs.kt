package com.minervabi.cards

import android.content.Context
import android.content.SharedPreferences
import org.json.JSONObject

/** Налаштування й підключення: адреса Minerva, ключ телефону, вибраний пресет, режими камери. */
object Prefs {
    private lateinit var sp: SharedPreferences

    fun init(context: Context) {
        sp = context.getSharedPreferences("minerva_cards", Context.MODE_PRIVATE)
    }

    var server: String
        get() = sp.getString("server", "") ?: ""
        set(v) = sp.edit().putString("server", v.trimEnd('/')).apply()

    var token: String
        get() = sp.getString("token", "") ?: ""
        set(v) = sp.edit().putString("token", v).apply()

    var userName: String
        get() = sp.getString("user_name", "") ?: ""
        set(v) = sp.edit().putString("user_name", v).apply()

    var userEmail: String
        get() = sp.getString("user_email", "") ?: ""
        set(v) = sp.edit().putString("user_email", v).apply()

    /** Роль у Minerva того, від чийого імені працює телефон («🔑 Адміністратор»). */
    var userRole: String
        get() = sp.getString("user_role", "") ?: ""
        set(v) = sp.edit().putString("user_role", v).apply()

    /** Вікно підключення вже показували при першому запуску (далі — лише з «⚙ Налаштування»). */
    var pairPrompted: Boolean
        get() = sp.getBoolean("pair_prompted", false)
        set(v) = sp.edit().putBoolean("pair_prompted", v).apply()

    /** 👁 Хто бачить нові знімки в Minerva: team — усі, private — лише я (останній вибір). */
    var visibility: String
        get() = sp.getString("visibility", "team") ?: "team"
        set(v) = sp.edit().putString("visibility", v).apply()

    /** «Viacheslav · 🔑 Адміністратор». */
    val who: String get() = listOf(userName, userRole).filter { it.isNotEmpty() }.joinToString(" · ")

    /** Ключ відкликали в Minerva (401) — треба підключити знову. */
    var needsPairing: Boolean
        get() = sp.getBoolean("needs_pairing", false)
        set(v) = sp.edit().putBoolean("needs_pairing", v).apply()

    val paired: Boolean get() = token.isNotEmpty() && server.isNotEmpty()

    /** Вибраний пресет: id з Minerva, -1 — без пресету, null — ще не вибирали (тоді — за замовчуванням з Minerva). */
    var presetId: Int?
        get() = if (sp.contains("preset")) sp.getInt("preset", -1) else null
        set(v) = if (v == null) sp.edit().remove("preset").apply() else sp.edit().putInt("preset", v).apply()

    /** Рамка: card (візитка 1,586 : 1), flyer (A4 стоячи), free (весь кадр). */
    var frame: String
        get() = sp.getString("frame", "card") ?: "card"
        set(v) = sp.edit().putString("frame", v).apply()

    var kind: String
        get() = sp.getString("kind", "card") ?: "card"
        set(v) = sp.edit().putString("kind", v).apply()

    var flash: Boolean
        get() = sp.getBoolean("flash", false)
        set(v) = sp.edit().putBoolean("flash", v).apply()

    /** 📐 Візитку / флаєр обрізати по знайдених краях і вирівняти (ні — по рамці). */
    var edges: Boolean
        get() = sp.getBoolean("edges", true)
        set(v) = sp.edit().putBoolean("edges", v).apply()

    /** Підказку «утримуйте 📐 — скан Google» вже показали. */
    var edgesTip: Boolean
        get() = sp.getBoolean("edges_tip", false)
        set(v) = sp.edit().putBoolean("edges_tip", v).apply()

    /** Після лицьового боку одразу пропонувати зворотний. */
    var askBack: Boolean
        get() = sp.getBoolean("ask_back", true)
        set(v) = sp.edit().putBoolean("ask_back", v).apply()

    var readAiDefault: Boolean
        get() = sp.getBoolean("read_ai", true)
        set(v) = sp.edit().putBoolean("read_ai", v).apply()

    var model: String
        get() = sp.getString("model", "") ?: ""
        set(v) = sp.edit().putString("model", v).apply()

    var wifiOnly: Boolean
        get() = sp.getBoolean("wifi_only", false)
        set(v) = sp.edit().putBoolean("wifi_only", v).apply()

    /** Найбільша сторона фото, px (менше — швидше на виставковому інтернеті). */
    var maxPx: Int
        get() = sp.getInt("max_px", 2400)
        set(v) = sp.edit().putInt("max_px", v).apply()

    /** Остання відповідь /meta/ — категорії, пресети, позначки працюють і без інтернету. */
    var metaJson: String
        get() = sp.getString("meta", "") ?: ""
        set(v) = sp.edit().putString("meta", v).apply()

    var browseJson: String
        get() = sp.getString("browse", "") ?: ""
        set(v) = sp.edit().putString("browse", v).apply()

    /** Пресети Minerva (з /meta/) і 📱 створені без Minerva — разом. */
    fun meta(): Meta {
        val m = Meta.parse(metaJson.takeIf { it.isNotBlank() }?.let { runCatching { JSONObject(it) }.getOrNull() })
        val local = localPresets().map { Preset.parse(it) }
        return if (local.isEmpty()) m else m.copy(presets = m.presets + local)
    }

    /** 📱 Пресети, створені без підключення (id ≤ −1000; −1 — «без пресету»). Після підключення — у Minerva. */
    fun localPresets(): List<JSONObject> {
        val a = runCatching { org.json.JSONArray(sp.getString("local_presets", "[]")) }.getOrNull() ?: return emptyList()
        return (0 until a.length()).mapNotNull { a.optJSONObject(it) }
    }

    private fun storeLocalPresets(items: List<JSONObject>) =
        sp.edit().putString("local_presets", org.json.JSONArray(items).toString()).apply()

    /** Зберегти 📱 пресет (новий — новий id). Повертає id. */
    fun saveLocalPreset(body: JSONObject): Int {
        val items = localPresets().toMutableList()
        val id = body.optInt("id").takeIf { it <= LOCAL_PRESET_FROM } ?: ((items.minOfOrNull { it.optInt("id") } ?: (LOCAL_PRESET_FROM + 1)) - 1)
            .coerceAtMost(LOCAL_PRESET_FROM)
        val o = JSONObject(body.toString()).put("id", id).put("local", true).put("mine", true).put("shared", body.optBoolean("shared"))
        o.remove("action"); o.remove("make_default")
        items.removeAll { it.optInt("id") == id }
        items.add(o)
        storeLocalPresets(items)
        return id
    }

    fun removeLocalPreset(id: Int) = storeLocalPresets(localPresets().filter { it.optInt("id") != id })

    const val LOCAL_PRESET_FROM = -1000

    fun forget() {
        // 📱 пресет з телефона лишається вибраним — після підключення він перейде в Minerva (Api.pushLocalPresets)
        val keep = presetId?.takeIf { it <= LOCAL_PRESET_FROM && localPresets().any { p -> p.optInt("id") == it } }
        sp.edit().remove("token").remove("user_name").remove("user_email").remove("user_role").remove("needs_pairing")
            .remove("meta").remove("browse").remove("preset").apply()
        if (keep != null) presetId = keep
    }
}
