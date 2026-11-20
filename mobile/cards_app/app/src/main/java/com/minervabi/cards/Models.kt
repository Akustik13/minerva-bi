package com.minervabi.cards

import org.json.JSONArray
import org.json.JSONObject

fun JSONArray?.strings(): List<String> =
    if (this == null) emptyList() else (0 until length()).map { optString(it) }

fun JSONArray?.ints(): List<Int> =
    if (this == null) emptyList() else (0 until length()).mapNotNull { optString(it).toIntOrNull() }

fun JSONObject.str(key: String): String = if (isNull(key)) "" else optString(key, "")

fun pairs(arr: JSONArray?): List<Pair<String, String>> {
    if (arr == null) return emptyList()
    return (0 until arr.length()).mapNotNull { i ->
        val p = arr.optJSONArray(i) ?: return@mapNotNull null
        p.optString(0) to p.optString(1)
    }
}

data class Category(val id: Int, val name: String, val icon: String, val color: String, val description: String) {
    val label get() = "$icon $name".trim()

    companion object {
        fun parse(o: JSONObject) = Category(o.optInt("id"), o.str("name"), o.str("icon"), o.str("color"),
            o.str("description"))
    }
}

data class Preset(
    val id: Int, val name: String, val event: String, val kind: String, val marks: List<String>, val note: String,
    val categories: List<Int>, val readAi: Boolean, val model: String, val shared: Boolean, val mine: Boolean,
    val owner: String, val role: String = "",
    /** 📱 Лише на телефоні (створено без Minerva) — перейде в Minerva після підключення. */
    val local: Boolean = false,
) {
    companion object {
        fun parse(o: JSONObject) = Preset(
            o.optInt("id"), o.str("name"), o.str("event"), o.str("kind").ifEmpty { "card" },
            o.optJSONArray("marks").strings(), o.str("note"), o.optJSONArray("categories").ints(),
            o.optBoolean("read_ai", true), o.str("model"), o.optBoolean("shared"), o.optBoolean("mine"),
            o.str("owner"), o.str("role"), o.optBoolean("local"),
        )
    }
}

data class Perms(
    val add: Boolean = false, val change: Boolean = false, val view: Boolean = false,
    val catAdd: Boolean = false, val catChange: Boolean = false, val upload: Boolean = false,
    val crmAdd: Boolean = false, val crmChange: Boolean = false,
) {
    val crm get() = crmAdd || crmChange
}

/** Відповідь /meta/ (кешується в Prefs.metaJson — без інтернету теж є категорії й пресети). */
data class Meta(
    val categories: List<Category> = emptyList(),
    val presets: List<Preset> = emptyList(),
    val defaultPreset: Int? = null,
    val kinds: List<Pair<String, String>> = DEFAULT_KINDS,
    val marks: List<Pair<String, String>> = DEFAULT_MARKS,
    /** 👥 Хто це для нас — без «не вказано» (його показує відсутність вибору). */
    val roles: List<Pair<String, String>> = DEFAULT_ROLES,
    /** 📎 Що ще можна дознімати до візитки (друга візитка, флаєр, каталог…). */
    val photoKinds: List<Pair<String, String>> = DEFAULT_PHOTO_KINDS,
    /** 👁 Хто бачить у Minerva: team — усі, private — лише я. */
    val visibility: List<Pair<String, String>> = DEFAULT_VISIBILITY,
    val models: List<Pair<String, String>> = emptyList(),
    val defaultModel: String = "",
    val events: List<String> = emptyList(),
    val perms: Perms = Perms(),
    val appUrl: String = "",
) {
    fun category(id: Int) = categories.firstOrNull { it.id == id }
    fun preset(id: Int?) = presets.firstOrNull { it.id == id }
    fun kindLabel(k: String) = kinds.firstOrNull { it.first == k }?.second ?: k
    fun markLabel(k: String) = marks.firstOrNull { it.first == k }?.second ?: k
    fun roleLabel(k: String) = if (k.isEmpty()) "" else roles.firstOrNull { it.first == k }?.second ?: k
    fun roleIcon(k: String) = roleLabel(k).substringBefore(" ")
    fun photoKindLabel(k: String) = photoKinds.firstOrNull { it.first == k }?.second ?: k

    companion object {
        val DEFAULT_KINDS = listOf("card" to "🪪 Візитка", "flyer" to "📄 Флаєр / каталог", "photo" to "📷 Фото",
            "other" to "📎 Інше")
        val DEFAULT_MARKS = listOf("star" to "⭐ Важливий", "fair" to "🎪 З виставки", "meet" to "🤝 Зустрічались",
            "check" to "❗ Перевірити", "sample" to "📦 Попросити зразки", "price" to "💶 Чекаємо ціни",
            "cheap" to "💸 Дешево", "quality" to "🏅 Якість", "no" to "👎 Не підходить")
        val DEFAULT_ROLES = listOf("supplier" to "🏭 Постачальник", "customer" to "👤 Клієнт", "other" to "🤝 Інше")
        val DEFAULT_VISIBILITY = listOf("team" to "👥 Бачать усі", "private" to "🔒 Лише я")
        val DEFAULT_PHOTO_KINDS = listOf("card" to "🪪 Ще візитка", "flyer" to "📄 Флаєр", "catalog" to "📚 Каталог / прайс",
            "product" to "📦 Продукт / зразок", "stand" to "🎪 Стенд", "other" to "📷 Інше")

        fun parse(o: JSONObject?): Meta {
            if (o == null) return Meta()
            val p = o.optJSONObject("perms") ?: JSONObject()
            val cats = o.optJSONArray("categories") ?: JSONArray()
            val pres = o.optJSONArray("presets") ?: JSONArray()
            return Meta(
                categories = (0 until cats.length()).map { Category.parse(cats.getJSONObject(it)) },
                presets = (0 until pres.length()).map { Preset.parse(pres.getJSONObject(it)) },
                defaultPreset = if (o.isNull("default_preset")) null else o.optInt("default_preset"),
                kinds = pairs(o.optJSONArray("kinds")).ifEmpty { DEFAULT_KINDS },
                marks = pairs(o.optJSONArray("marks")).ifEmpty { DEFAULT_MARKS },
                roles = pairs(o.optJSONArray("roles")).filter { it.first.isNotEmpty() }.ifEmpty { DEFAULT_ROLES },
                photoKinds = pairs(o.optJSONArray("photo_kinds")).ifEmpty { DEFAULT_PHOTO_KINDS },
                visibility = pairs(o.optJSONArray("visibility")).ifEmpty { DEFAULT_VISIBILITY },
                models = pairs(o.optJSONArray("models")),
                defaultModel = o.str("default_model"),
                events = o.optJSONArray("events").strings(),
                perms = Perms(p.optBoolean("add"), p.optBoolean("change"), p.optBoolean("view"),
                    p.optBoolean("cat_add"), p.optBoolean("cat_change"), p.optBoolean("upload"),
                    p.optBoolean("crm_add"), p.optBoolean("crm_change")),
                appUrl = o.str("app_url"),
            )
        }
    }
}

/** Ще фото групи в Minerva (друга візитка, флаєр…). */
data class ServerPhoto(val id: Int, val kind: String, val label: String, val note: String, val uuid: String)

/** Людина з іншої візитки групи (прочитав AI). */
data class Person(val name: String, val position: String, val email: String, val phone: String, val mobile: String) {
    val text get() = listOf(name, position, email, listOf(phone, mobile).filter { it.isNotEmpty() }.joinToString(", "))
        .filter { it.isNotEmpty() }.joinToString(" · ")
}

/** Візитка в Minerva (suppliers/services/card_app.py card_json). */
data class ServerCard(val json: JSONObject) {
    val id get() = json.optInt("id")
    val company get() = json.str("company")
    val person get() = json.str("person")
    val position get() = json.str("position")
    val country get() = json.str("country")
    val flag get() = json.str("flag")
    val event get() = json.str("event")
    val note get() = json.str("note")
    val kind get() = json.str("kind")
    val role get() = json.str("role")
    val roleAi get() = json.optBoolean("role_ai")
    val roleWhy get() = json.str("role_why")
    val title get() = json.str("title")
    val ai get() = json.str("ai")
    val aiLabel get() = json.str("ai_label")
    val reading get() = json.optBoolean("reading")
    val error get() = json.str("error")
    val desc get() = json.str("desc")
    val site get() = json.str("site")
    val email get() = json.str("email")
    val phone get() = json.str("phone")
    val mobile get() = json.str("mobile")
    val address get() = json.str("address")
    val notes get() = json.str("notes")
    val marks get() = json.optJSONArray("marks").strings()
    val categories get() = json.optJSONArray("categories").ints()
    val ownCategories get() = json.optJSONArray("own_categories").ints()
    val catsGuessed get() = json.optBoolean("cats_guessed")
    val hasBack get() = json.optBoolean("has_back")
    val editable get() = json.optBoolean("editable")
    val mine get() = json.optBoolean("mine")
    val by get() = json.str("by")
    val byRole get() = json.str("by_role")
    val sourceLabel get() = json.str("source_label")
    val visibility get() = json.str("visibility").ifEmpty { "team" }
    val private get() = visibility == "private"
    val canSetVisibility get() = json.optBoolean("can_set_visibility")
    /** «👤 Viacheslav (🔑 Адміністратор) · 📱 Samsung SM-S918B». */
    val whoText: String
        get() = listOf(listOf(by, byRole.takeIf { it.isNotEmpty() }?.let { "($it)" }.orEmpty())
            .filter { it.isNotEmpty() }.joinToString(" "), sourceLabel).filter { it.isNotEmpty() }.joinToString(" · ")
    val createdAt get() = json.str("created_at")
    val supplierName get() = json.optJSONObject("supplier")?.str("name") ?: ""
    val customerName get() = json.optJSONObject("customer")?.str("name") ?: ""
    val newCategory get() = json.optJSONObject("new_category")?.str("name") ?: ""
    val photos: List<ServerPhoto>
        get() {
            val arr = json.optJSONArray("photos") ?: return emptyList()
            return (0 until arr.length()).mapNotNull { arr.optJSONObject(it) }.map {
                ServerPhoto(it.optInt("id"), it.str("kind"), it.str("label"), it.str("note"), it.str("uuid"))
            }
        }
    val people: List<Person>
        get() {
            val arr = json.optJSONArray("people") ?: return emptyList()
            return (0 until arr.length()).mapNotNull { arr.optJSONObject(it) }.map {
                Person(it.str("name"), it.str("position"), it.str("email"), it.str("phone"), it.str("mobile"))
            }
        }

    /** Стан AI одним рядком: 🧠 читає / прочитано / не вдалося / ще не читав. */
    fun aiText(): String = when {
        reading -> "🧠 AI читає…"
        else -> aiLabel.ifEmpty { "○ AI ще не читав" }
    }

    /** AI ще працює над цією візиткою — варто перепитати Minerva трохи згодом. */
    val pending get() = reading
}
