"""Angebote — комерційні пропозиції.

Розрахунок робить код Minerva (Decimal, offers/pricing.py), ціни — з каталогу Minerva.
Надіслана версія — незмінний знімок (OfferRevision.snapshot); зміни після відправки — лише в новій версії.
Чернетка Angebot нічого не резервує на складі.
"""
from decimal import Decimal

from django.conf import settings
from django.core.validators import FileExtensionValidator, MinValueValidator
from django.db import IntegrityError, models, transaction
from django.utils import timezone

LANGUAGES = [("de", "Deutsch"), ("en", "English"), ("uk", "Українська")]


class OfferSettings(models.Model):
    """Налаштування Angebote — синглтон (pk=1)."""
    number_prefix = models.CharField("Префікс номера", max_length=5, default="A",
                                     help_text="Номер: <префікс>-РРРР-NNN, напр. A-2026-001")
    validity_days = models.PositiveSmallIntegerField("Дійсна, днів", default=30)
    default_currency = models.CharField("Валюта", max_length=3, default="EUR")
    default_vat_rate = models.DecimalField("ПДВ, %", max_digits=5, decimal_places=2, default=Decimal("19.00"))
    default_language = models.CharField("Мова документа", max_length=2, choices=LANGUAGES, default="de")
    intro_text = models.TextField(
        "Вступ", blank=True,
        default="vielen Dank für Ihre Anfrage. Gerne unterbreiten wir Ihnen folgendes Angebot:")
    payment_terms = models.TextField("Умови оплати", blank=True,
                                     default="Zahlbar innerhalb von 14 Tagen nach Rechnungsstellung ohne Abzug.")
    delivery_terms = models.TextField("Умови поставки", blank=True,
                                      default="Lieferung DAP, zzgl. Versandkosten. Lieferzeit wie angegeben.")
    outro_text = models.TextField("Завершення", blank=True,
                                  default="Für Rückfragen stehen wir Ihnen gerne zur Verfügung.")
    company_block = models.TextField(
        "Реквізити компанії (шапка)", blank=True, default="",
        help_text="Назва, адреса, USt-IdNr., телефон, email — кожне з нового рядка.")
    footer_text = models.TextField(
        "Нижній колонтитул", blank=True, default="",
        help_text="Банк, IBAN/BIC, реєстр, керівник — кожне з нового рядка.")
    email_subject = models.CharField("Тема листа", max_length=200, default="Angebot {number}",
                                     help_text="Змінні: {number} {customer}")
    template = models.FileField(
        "Власний шаблон DOCX", upload_to="offers/templates/", blank=True,
        validators=[FileExtensionValidator(["docx"])],
        help_text="Порожньо — стандартний шаблон Minerva (offers/templates_docx/offer_template.docx).")
    SCAN_OFF, SCAN_ALL = 0, -1
    SCAN_CHOICES = [(SCAN_OFF, "—"), (30, "30 днів"), (90, "3 місяці"), (180, "6 місяців"), (365, "1 рік"),
                    (SCAN_ALL, "Уся пошта")]
    quote_scan_depth = models.IntegerField(
        "Знайти запити в уже отриманій пошті", choices=SCAN_CHOICES, default=SCAN_OFF,
        help_text="Одноразово: після збереження обробка йде у фоні, а поле повертається на «—». "
                  "Нова пошта перевіряється автоматично; листи з уже прийнятим рішенням не змінюються.")
    quote_scan_status = models.CharField("Стан обробки", max_length=255, blank=True, default="", editable=False)
    quote_scan_at = models.DateTimeField("Оновлено", null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "Налаштування Angebote"
        verbose_name_plural = "⚙️ Налаштування Angebote"

    def __str__(self):
        return "Налаштування Angebote"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        return cls.objects.get_or_create(pk=1)[0]


class Offer(models.Model):
    DRAFT, REVIEW, APPROVED, SENT = "draft", "review", "approved", "sent"
    ACCEPTED, REJECTED, EXPIRED, CANCELLED = "accepted", "rejected", "expired", "cancelled"
    STATUS_CHOICES = [
        (DRAFT, "✏️ Чернетка"), (REVIEW, "👀 На перевірці"), (APPROVED, "✅ Погоджено"),
        (SENT, "📤 Надіслано"), (ACCEPTED, "🤝 Прийнято"), (REJECTED, "✖ Відхилено"),
        (EXPIRED, "⌛ Прострочено"), (CANCELLED, "🚫 Скасовано"),
    ]
    LOCKED = (SENT, ACCEPTED, REJECTED, EXPIRED, CANCELLED)

    number = models.CharField("Номер", max_length=30, unique=True, editable=False)
    revision = models.PositiveIntegerField("Версія", default=1, editable=False)
    status = models.CharField("Статус", max_length=10, choices=STATUS_CHOICES, default=DRAFT, db_index=True)
    customer = models.ForeignKey("crm.Customer", null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="offers", verbose_name="Клієнт")
    contact_name = models.CharField("Контактна особа", max_length=255, blank=True, default="")
    contact_email = models.EmailField("Email", blank=True, default="")
    billing_address = models.TextField("Адреса клієнта", blank=True, default="")
    project = models.ForeignKey("projects.Project", null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="offers", verbose_name="Проєкт")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="owned_offers", verbose_name="Відповідальний")
    language = models.CharField("Мова документа", max_length=2, choices=LANGUAGES, default="de")
    currency = models.CharField("Валюта", max_length=3, default="EUR")
    vat_rate = models.DecimalField("ПДВ, %", max_digits=5, decimal_places=2, default=Decimal("19.00"),
                                   validators=[MinValueValidator(Decimal("0"))],
                                   help_text="0 — напр. Reverse Charge / експорт (вкажіть у вступі чи умовах).")
    shipping_cost = models.DecimalField("Доставка (нетто)", max_digits=12, decimal_places=2, default=Decimal("0"),
                                        validators=[MinValueValidator(Decimal("0"))])
    valid_until = models.DateField("Дійсна до", null=True, blank=True)
    intro_text = models.TextField("Вступ", blank=True, default="")
    payment_terms = models.TextField("Умови оплати", blank=True, default="")
    delivery_terms = models.TextField("Умови поставки", blank=True, default="")
    outro_text = models.TextField("Завершення", blank=True, default="")
    internal_notes = models.TextField("Внутрішні нотатки", blank=True, default="",
                                      help_text="Не потрапляють у документ.")
    source_message = models.ForeignKey("email_assistant.EmailMessage", null=True, blank=True,
                                       on_delete=models.SET_NULL, related_name="offers",
                                       verbose_name="З листа")
    source_quote = models.ForeignKey("sales.SalesOrder", null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="quote_offers", verbose_name="Із запиту пропозиції")
    order = models.ForeignKey("sales.SalesOrder", null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="offers", verbose_name="Замовлення з пропозиції")
    net_total = models.DecimalField("Сума нетто", max_digits=14, decimal_places=2, default=Decimal("0"))
    vat_total = models.DecimalField("ПДВ", max_digits=14, decimal_places=2, default=Decimal("0"))
    gross_total = models.DecimalField("Сума брутто", max_digits=14, decimal_places=2, default=Decimal("0"))
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField("Створено", auto_now_add=True)
    updated_at = models.DateTimeField("Оновлено", auto_now=True)
    sent_at = models.DateTimeField("Надіслано", null=True, blank=True)
    decided_at = models.DateTimeField("Рішення клієнта", null=True, blank=True)

    class Meta:
        verbose_name = "Angebot"
        verbose_name_plural = "📝 Angebote"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.number} · {self.customer or self.contact_name or '—'}"

    @property
    def is_locked(self) -> bool:
        """Надіслану (і далі) версію не змінюють — лише «Нова версія»."""
        return self.status in self.LOCKED

    @property
    def display_number(self) -> str:
        return self.number if self.revision == 1 else f"{self.number}-V{self.revision}"

    def save(self, *args, **kwargs):
        if self.number:
            return super().save(*args, **kwargs)
        for _ in range(5):      # номер видається під час першого збереження; гонка → наступний номер
            self.number = self._next_number()
            try:
                with transaction.atomic():
                    return super().save(*args, **kwargs)
            except IntegrityError:
                self.number = ""
        raise IntegrityError("Не вдалося видати унікальний номер Angebot")

    @classmethod
    def _next_number(cls) -> str:
        prefix = f"{OfferSettings.get().number_prefix or 'A'}-{timezone.localdate().year}-"
        numbers = [int(n[len(prefix):]) for n in cls.objects.filter(number__startswith=prefix)
                   .values_list("number", flat=True) if n[len(prefix):].isdigit()]
        return f"{prefix}{(max(numbers) if numbers else 0) + 1:03d}"


class QuoteRequest(models.Model):
    """Лист, схожий на запит пропозиції (offers/detection.py) — підказка «📝 запит» у пошті."""
    NEW, DONE, DISMISSED = "new", "done", "dismissed"
    STATUS_CHOICES = [(NEW, "📝 Новий запит"), (DONE, "✅ Angebot створено"), (DISMISSED, "✕ Не запит")]

    message = models.OneToOneField("email_assistant.EmailMessage", on_delete=models.CASCADE,
                                   related_name="quote_request", verbose_name="Лист")
    thread = models.ForeignKey("email_assistant.EmailThread", null=True, blank=True, on_delete=models.CASCADE,
                               related_name="quote_requests", verbose_name="Гілка")
    reason = models.CharField("Підстава", max_length=255, blank=True, default="")
    items = models.JSONField("Позиції з листа", default=list, blank=True)
    status = models.CharField("Статус", max_length=10, choices=STATUS_CHOICES, default=NEW, db_index=True)
    offer = models.ForeignKey("Offer", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
                              verbose_name="Angebot")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField("Знайдено", auto_now_add=True)

    class Meta:
        verbose_name = "Запит пропозиції з пошти"
        verbose_name_plural = "📝 Запити пропозицій з пошти"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_status_display()}: {self.message.subject or '(без теми)'}"

    @property
    def items_label(self) -> str:
        return ", ".join(f"{i['sku']} × {i['qty']}" if i.get("qty") else i["sku"] for i in self.items[:6])


class OfferLine(models.Model):
    SHOP, SALE_PRICE, BASE, MANUAL, QUOTE = "shop", "sale_price", "base", "manual", "quote"
    PRICE_SOURCES = [(SHOP, "магазин"), (SALE_PRICE, "ціна продажу"), (BASE, "базова ціна × курс"),
                     (MANUAL, "вручну"), (QUOTE, "із запиту клієнта")]

    offer = models.ForeignKey(Offer, on_delete=models.CASCADE, related_name="lines")
    position = models.PositiveSmallIntegerField("№", default=1)
    product = models.ForeignKey("inventory.Product", null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="+", verbose_name="Товар")
    sku = models.CharField("Артикул", max_length=255, blank=True, default="")
    description = models.TextField("Опис", blank=True, default="")
    qty = models.DecimalField("К-сть", max_digits=12, decimal_places=3, default=Decimal("1"),
                              validators=[MinValueValidator(Decimal("0.001"))])
    unit = models.CharField("Од.", max_length=16, default="Stk")
    unit_price = models.DecimalField("Ціна за од.", max_digits=18, decimal_places=4, null=True, blank=True,
                                     help_text="Порожньо — ціна з каталогу Minerva (магазин → ціна продажу → базова).")
    discount_percent = models.DecimalField("Знижка, %", max_digits=5, decimal_places=2, default=Decimal("0"),
                                           validators=[MinValueValidator(Decimal("0"))])
    price_source = models.CharField("Джерело ціни", max_length=12, choices=PRICE_SOURCES, default=MANUAL)
    price_note = models.CharField("Звідки ціна", max_length=255, blank=True, default="")
    lead_time = models.CharField("Строк поставки", max_length=100, blank=True, default="")
    line_total = models.DecimalField("Сума", max_digits=14, decimal_places=2, default=Decimal("0"))

    class Meta:
        verbose_name = "Позиція"
        verbose_name_plural = "Позиції"
        ordering = ["position", "pk"]

    def __str__(self):
        return f"{self.position}. {self.sku or self.description[:40]}"


def _revision_path(instance, filename):
    return f"offers/{instance.offer.number}/{filename}"


class OfferRevision(models.Model):
    """Знімок версії для документа. Після відправки не змінюється (зміна → нова версія)."""
    offer = models.ForeignKey(Offer, on_delete=models.CASCADE, related_name="revisions")
    revision = models.PositiveIntegerField()
    snapshot = models.JSONField(default=dict)
    docx_file = models.FileField(upload_to=_revision_path, blank=True)
    pdf_file = models.FileField(upload_to=_revision_path, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    sent_message_id = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        verbose_name = "Версія Angebot"
        verbose_name_plural = "Версії Angebot"
        ordering = ["-revision"]
        constraints = [models.UniqueConstraint(fields=["offer", "revision"], name="unique_offer_revision")]

    def __str__(self):
        return f"{self.offer.number} V{self.revision}"

    @property
    def file_name(self) -> str:
        suffix = "" if self.revision == 1 else f"-V{self.revision}"
        return f"Angebot_{self.offer.number}{suffix}"
