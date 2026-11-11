"""
suppliers/models.py — 🏭 Постачальники: категорії, контактні особи, візитки й флаєри, історія, аналізи, запити (RFQ).

Сам постачальник — inventory.Supplier (картка в «📦 Склад → Постачальники»); тут — усе, що навколо нього.
Логіка — suppliers/services/*.py, сторінки — suppliers/admin.py і suppliers/admin_supplier.py (вбудовано в картку).
"""
import os
import uuid

from django.conf import settings
from django.db import IntegrityError, models, transaction
from django.utils import timezone


class SupplierCategory(models.Model):
    """Група постачальників: «Виробники кабелів», «Виробники плат», «Закупка компонентів»… Можна вкладати."""
    name = models.CharField("Назва", max_length=120, unique=True)
    icon = models.CharField("Значок", max_length=8, blank=True, default="🏭")
    color = models.CharField("Колір", max_length=7, blank=True, default="#5b8def", help_text="#rrggbb")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="children",
                               verbose_name="Входить до")
    order = models.PositiveIntegerField("Порядок", default=100)
    description = models.TextField("Опис", blank=True, default="")
    request_template = models.CharField(
        "Форма запиту (RFQ)", max_length=20, blank=True, default="",
        help_text="Які поля пропонувати в запиті постачальникам цієї категорії (плати — стекап, шари…). "
                  "Порожньо — загальна форма.")
    is_active = models.BooleanField("Діє", default=True)

    class Meta:
        ordering = ["order", "name"]
        verbose_name = "Категорія постачальників"
        verbose_name_plural = "🗂 Категорії постачальників"

    def __str__(self):
        return f"{self.icon} {self.name}".strip()

    @property
    def path(self) -> str:
        names, node, seen = [], self, set()
        while node is not None and node.pk not in seen:
            seen.add(node.pk)
            names.append(node.name)
            node = node.parent
        return " → ".join(reversed(names))


class SupplierContact(models.Model):
    """Контактна особа постачальника (у фірмі їх кілька: продаж, інженер, бухгалтерія…)."""
    SOURCES = [("manual", "вручну"), ("mail", "з листа"), ("card", "з візитки"), ("ai", "AI")]
    supplier = models.ForeignKey("inventory.Supplier", on_delete=models.CASCADE, related_name="contacts",
                                 verbose_name="Постачальник")
    name = models.CharField("Ім'я", max_length=200)
    position = models.CharField("Посада / відділ", max_length=200, blank=True, default="")
    email = models.EmailField("Email", blank=True, default="")
    phone = models.CharField("Телефон", max_length=60, blank=True, default="")
    mobile = models.CharField("Мобільний / WhatsApp / WeChat", max_length=80, blank=True, default="")
    notes = models.TextField("Нотатки", blank=True, default="")
    is_primary = models.BooleanField("Основний", default=False)
    is_active = models.BooleanField("Працює там", default=True)
    source = models.CharField("Звідки", max_length=10, choices=SOURCES, default="manual", blank=True)
    created_at = models.DateTimeField("Додано", default=timezone.now)

    class Meta:
        ordering = ["-is_primary", "name"]
        verbose_name = "Контактна особа"
        verbose_name_plural = "Контактні особи"

    def __str__(self):
        return self.name


def _media_path(instance, filename):
    ext = os.path.splitext(filename or "")[1].lower()[:8] or ".jpg"
    return f"suppliers/cards/{timezone.now():%Y/%m}/{uuid.uuid4().hex}{ext}"


class SupplierMedia(models.Model):
    """📷 Візитка, флаєр, фото стенду… — з них створено / доповнено постачальника; наші примітки й позначки."""
    CARD, FLYER, PHOTO, OTHER = "card", "flyer", "photo", "other"
    KINDS = [(CARD, "🪪 Візитка"), (FLYER, "📄 Флаєр / каталог"), (PHOTO, "📷 Фото"), (OTHER, "📎 Інше")]
    NEW, READ, APPLIED, FAILED = "new", "read", "applied", "failed"
    STATES = [(NEW, "нове"), (READ, "прочитано AI"), (APPLIED, "внесено"), (FAILED, "не вдалося прочитати")]
    MARKS = [("star", "⭐ Важливий"), ("fair", "🎪 З виставки"), ("meet", "🤝 Зустрічались"),
             ("check", "❗ Перевірити"), ("sample", "📦 Попросити зразки"), ("price", "💶 Чекаємо ціни"),
             ("cheap", "💸 Дешево"), ("quality", "🏅 Якість"), ("no", "👎 Не підходить")]

    supplier = models.ForeignKey("inventory.Supplier", null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="media", verbose_name="Постачальник")
    contact = models.ForeignKey(SupplierContact, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="media", verbose_name="Контактна особа")
    # візитка покупця — у CRM клієнтом (suppliers/services/cards.py to_customer)
    customer = models.ForeignKey("crm.Customer", null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="business_cards", verbose_name="Клієнт CRM")
    kind = models.CharField("Що це", max_length=8, choices=KINDS, default=CARD)
    image = models.FileField("Фото", upload_to=_media_path)
    image_back = models.FileField("Зворотний бік", upload_to=_media_path, blank=True, default="")
    # перша версія знімка до «✏️ Редагувати фото» (повернути / обрізати) — «↺ Оригінал» повертає її
    image_original = models.FileField("Оригінал", upload_to=_media_path, blank=True, default="")
    image_back_original = models.FileField("Оригінал зворотного боку", upload_to=_media_path, blank=True, default="")
    title = models.CharField("Назва", max_length=200, blank=True, default="")
    event = models.CharField("Де взяли", max_length=200, blank=True, default="",
                             help_text="Виставка, зустріч… напр. «electronica 2026, хол B3»")
    note = models.TextField("Наші примітки", blank=True, default="")
    marks = models.JSONField("Позначки", default=list, blank=True)
    state = models.CharField("Стан", max_length=8, choices=STATES, default=NEW)
    extracted = models.JSONField("Що прочитав AI", default=dict, blank=True)
    qr_text = models.TextField("QR-код", blank=True, default="",
                               help_text="Розшифрований текст QR-коду з фото (vCard, посилання…) — suppliers/services/qr.py")
    error = models.TextField(blank=True, default="")
    cost_usd = models.DecimalField(max_digits=8, decimal_places=4, default=0)
    categories = models.ManyToManyField(SupplierCategory, blank=True, related_name="+",
                                        verbose_name="Категорії для нового постачальника")
    preset = models.ForeignKey("SupplierCardPreset", null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="+", verbose_name="Пресет")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField("Додано", default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Візитка / флаєр"
        verbose_name_plural = "📷 Візитки й флаєри"

    def __str__(self):
        return self.title or self.get_kind_display()

    def marks_text(self) -> str:
        names = dict(self.MARKS)
        return " ".join(names[m] for m in (self.marks or []) if m in names)


class SupplierCardPreset(models.Model):
    """📌 Пресет візиток: «electronica 2026, хол B3» — що підставити в кожне нове фото (де взяли, вид, позначки,
    примітка, категорії, AI). Свій або спільний для всіх (колеги на тій самій виставці); за замовчуванням —
    UserProfile.ui_prefs["supplier_card_preset"] людини (suppliers/admin_supplier.py)."""
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    name = models.CharField("Назва", max_length=120)
    event = models.CharField("Де взяли", max_length=200, blank=True, default="")
    kind = models.CharField("Що це", max_length=8, choices=SupplierMedia.KINDS, default=SupplierMedia.CARD)
    marks = models.JSONField("Позначки", default=list, blank=True)
    note = models.TextField("Примітка", blank=True, default="")
    categories = models.ManyToManyField(SupplierCategory, blank=True, related_name="+")
    read_ai = models.BooleanField("Читати AI", default=True)
    model = models.CharField("Модель AI", max_length=40, blank=True, default="")
    shared = models.BooleanField("Для всіх", default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name"]
        verbose_name = "Пресет візиток"
        verbose_name_plural = "Пресети візиток"

    def __str__(self):
        return self.name


class SupplierEvent(models.Model):
    """🕘 Історія постачальника: звідки взявся, що додали, які аналізи й запити — «звідки все пішло»."""
    CREATED, MAIL, CARD, CONTACT, AI, REQUEST, NOTE, MERGE = (
        "created", "mail", "card", "contact", "ai", "request", "note", "merge")
    KINDS = [(CREATED, "🆕 Створено"), (MAIL, "✉️ Лист"), (CARD, "📷 Візитка / флаєр"), (CONTACT, "👤 Контакт"),
             (AI, "🧠 AI"), (REQUEST, "📨 Запит"), (NOTE, "📝 Примітка"), (MERGE, "🔀 Злиття")]
    supplier = models.ForeignKey("inventory.Supplier", on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=10, choices=KINDS, default=NOTE)
    text = models.TextField(blank=True, default="")
    message = models.ForeignKey("email_assistant.EmailMessage", null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="+")
    media = models.ForeignKey(SupplierMedia, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    request = models.ForeignKey("SupplierRequest", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                             related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-pk"]
        verbose_name = "Подія постачальника"
        verbose_name_plural = "Історія постачальників"


class SupplierAnalysis(models.Model):
    """Збережений аналіз: ✉️ пошта за період (кожна людина — свій, листи лише її скриньок), 🧠 AI-оцінка,
    🌐 інформація з інтернету. Показується останній кожного виду."""
    MAIL, AI, WEB = "mail", "ai", "web"
    KINDS = [(MAIL, "✉️ Пошта"), (AI, "🧠 AI-оцінка"), (WEB, "🌐 З інтернету")]
    supplier = models.ForeignKey("inventory.Supplier", on_delete=models.CASCADE, related_name="analyses")
    kind = models.CharField(max_length=6, choices=KINDS)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="+")
    date_from = models.DateField(null=True, blank=True)
    date_to = models.DateField(null=True, blank=True)
    result = models.JSONField(default=dict, blank=True)
    text = models.TextField(blank=True, default="")
    model = models.CharField(max_length=40, blank=True, default="")
    cost_usd = models.DecimalField(max_digits=8, decimal_places=4, default=0)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]


class SupplierMailIgnore(models.Model):
    """«🔎 Постачальники з пошти»: адреса / @домен — не постачальник (більше не пропонувати)."""
    key = models.CharField(max_length=254, unique=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                             related_name="+")
    created_at = models.DateTimeField(default=timezone.now)


# ── 📨 Запити постачальникам (RFQ) ────────────────────────────────────────────

class SupplierRequest(models.Model):
    """Один запит — кільком постачальникам: що треба (форма + текст), файли (ZIP, Gerber, креслення), кому.
    Кожен отримувач — свій лист і своя гілка; відповіді Minerva сама знаходить у пошті."""
    DRAFT, SENT, ANSWERED, CLOSED, CANCELLED = "draft", "sent", "answered", "closed", "cancelled"
    STATUSES = [(DRAFT, "📝 Чернетка"), (SENT, "📤 Надіслано, чекаємо"), (ANSWERED, "📥 Є відповіді"),
                (CLOSED, "✅ Вирішено"), (CANCELLED, "✖ Скасовано")]
    OPEN = (SENT, ANSWERED)
    LANGS = [("en", "English"), ("de", "Deutsch"), ("uk", "Українська")]

    code = models.CharField("Номер", max_length=20, unique=True, editable=False)
    title = models.CharField("Що запитуємо", max_length=200)
    status = models.CharField("Стан", max_length=10, choices=STATUSES, default=DRAFT, db_index=True)
    template = models.CharField("Форма", max_length=20, blank=True, default="generic")
    category = models.ForeignKey(SupplierCategory, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="requests", verbose_name="Категорія постачальників")
    project = models.ForeignKey("projects.Project", null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="supplier_requests", verbose_name="Проєкт")
    group = models.CharField("Група / тема", max_length=120, blank=True, default="", db_index=True,
                             help_text="Щоб не плутати: «Плати для X», «Ціни на виробництво 2026»… — запити з тією "
                                       "самою групою стоять разом.")
    specs = models.JSONField("Параметри", default=dict, blank=True)
    quantities = models.CharField("Кількості", max_length=120, blank=True, default="",
                                  help_text="Через «;», напр. «10; 100; 500» — ціна на кожну.")
    language = models.CharField("Мова листа", max_length=2, choices=LANGS, default="en")
    subject = models.CharField("Тема листа", max_length=250, blank=True, default="")
    body = models.TextField("Текст листа", blank=True, default="")
    reply_by = models.DateField("Чекаємо відповідь до", null=True, blank=True)
    account = models.ForeignKey("email_assistant.EmailAccount", null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="+", verbose_name="З якої скриньки")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="+", verbose_name="Хто веде")
    decision = models.TextField("Рішення / підсумок", blank=True, default="")
    analysis = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    sent_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Запит постачальникам"
        verbose_name_plural = "📨 Запити постачальникам (RFQ)"

    def __str__(self):
        return f"{self.code} · {self.title}"

    def save(self, *args, **kwargs):
        if self.code:
            return super().save(*args, **kwargs)
        for _ in range(5):   # RFQ-РРРР-NNN; два збереження збіглися — повтор з новим номером
            self.code = self._next_code()
            try:
                with transaction.atomic():
                    return super().save(*args, **kwargs)
            except IntegrityError:
                self.code = ""
        raise IntegrityError("Не вдалося видати номер запиту")

    @classmethod
    def _next_code(cls) -> str:
        prefix = f"RFQ-{timezone.localdate().year}-"
        numbers = [int(c[len(prefix):]) for c in cls.objects.filter(code__startswith=prefix)
                   .values_list("code", flat=True) if c[len(prefix):].isdigit()]
        return f"{prefix}{(max(numbers) if numbers else 0) + 1:03d}"

    @property
    def is_open(self) -> bool:
        return self.status in self.OPEN


def _request_path(instance, filename):
    from inventory.services.po_documents import safe_name
    return f"supplier_requests/{instance.request_id}/{uuid.uuid4().hex[:8]}_{safe_name(filename)}"


class SupplierRequestFile(models.Model):
    """Файл запиту: ZIP, Gerber, креслення, BOM… — іде вкладенням кожному отримувачу."""
    request = models.ForeignKey(SupplierRequest, on_delete=models.CASCADE, related_name="files")
    file = models.FileField(upload_to=_request_path, max_length=300)
    name = models.CharField(max_length=255)
    size = models.PositiveIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True, default="")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["pk"]


class SupplierRequestRecipient(models.Model):
    """Кому пішов запит: свій лист, своя гілка, відповідь, ціна й строк (вручну або з AI), рішення."""
    PLANNED, SENT, FAILED, REPLIED, QUOTED, DECLINED, SILENT = (
        "planned", "sent", "failed", "replied", "quoted", "declined", "silent")
    STATES = [(PLANNED, "⏳ Ще не надіслано"), (SENT, "📤 Надіслано"), (FAILED, "⚠️ Не надіслано"),
              (REPLIED, "📥 Відповів"), (QUOTED, "💶 Є пропозиція"), (DECLINED, "🙅 Відмовився"),
              (SILENT, "🔕 Не відповів")]
    WAITING = (SENT,)

    request = models.ForeignKey(SupplierRequest, on_delete=models.CASCADE, related_name="recipients")
    supplier = models.ForeignKey("inventory.Supplier", on_delete=models.CASCADE, related_name="request_recipients")
    contact = models.ForeignKey(SupplierContact, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    email = models.CharField("Email", max_length=254)
    cc = models.CharField("Копія", max_length=500, blank=True, default="")
    state = models.CharField("Стан", max_length=8, choices=STATES, default=PLANNED, db_index=True)
    error = models.TextField(blank=True, default="")
    message_id = models.CharField(max_length=500, blank=True, default="")
    sent_message = models.ForeignKey("email_assistant.EmailMessage", null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name="+")
    thread = models.ForeignKey("email_assistant.EmailThread", null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="+")
    reply_message = models.ForeignKey("email_assistant.EmailMessage", null=True, blank=True,
                                      on_delete=models.SET_NULL, related_name="+")
    sent_at = models.DateTimeField(null=True, blank=True)
    replied_at = models.DateTimeField(null=True, blank=True)
    reminded_at = models.DateTimeField(null=True, blank=True)
    price = models.DecimalField("Ціна за шт.", max_digits=14, decimal_places=4, null=True, blank=True)
    total = models.DecimalField("Сума", max_digits=14, decimal_places=2, null=True, blank=True)
    currency = models.CharField("Валюта", max_length=3, blank=True, default="")
    tiers = models.JSONField("Ціни за кількістю", default=list, blank=True)
    lead_days = models.PositiveIntegerField("Строк, днів", null=True, blank=True)
    shipping = models.DecimalField("Доставка", max_digits=12, decimal_places=2, null=True, blank=True)
    valid_until = models.DateField("Пропозиція діє до", null=True, blank=True)
    quote_note = models.TextField("Умови / примітки", blank=True, default="")
    ai = models.JSONField(default=dict, blank=True)
    chosen = models.BooleanField("Вибрано", default=False)

    class Meta:
        ordering = ["pk"]
        constraints = [models.UniqueConstraint(fields=["request", "supplier"], name="rfq_one_per_supplier")]

    def __str__(self):
        return f"{self.request.code} → {self.supplier}"
