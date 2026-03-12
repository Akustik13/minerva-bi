from django.db import models


LEVEL_CHOICES = [
    (1, "Базовий (Invoice + Payment)"),
    (2, "Стандарт (+ Витрати, VAT)"),
    (3, "Розширений (+ Журнал проводок)"),
]

ALL_MODULES = ["crm", "accounting", "sales", "shipping", "inventory", "bots"]

COUNTRY_CODE_FORMAT_CHOICES = [
    ("iso2", "ISO-2 (2 букви: DE, UA, PL)"),
    ("iso3", "ISO-3 (3 букви: DEU, UKR, POL)"),
]

WEIGHT_UNIT_CHOICES = [
    ("kg", "кг (кілограм)"),
    ("g",  "г (грам)"),
    ("lb", "lb (фунт)"),
]

DIMENSION_UNIT_CHOICES = [
    ("cm", "см (сантиметр)"),
    ("mm", "мм (міліметр)"),
    ("in", "in (дюйм)"),
]


class SystemSettings(models.Model):
    company_name     = models.CharField("Назва компанії", max_length=255, default="Моя компанія")
    logo             = models.ImageField("Логотип", upload_to="config/", null=True, blank=True)
    default_currency = models.CharField("Валюта за замовчуванням", max_length=3, default="EUR")
    timezone         = models.CharField("Часовий пояс", max_length=50, default="Europe/Kyiv")
    enabled_modules  = models.JSONField("Активні модулі", default=list,
                                        help_text="Список app labels: crm, accounting, sales, shipping, inventory, bots")
    accounting_level = models.IntegerField("Рівень бухгалтерії", choices=LEVEL_CHOICES, default=2)
    is_onboarding_complete = models.BooleanField("Онбординг завершено", default=False)

    # ── Формат коду країни ────────────────────────────────────────────────────
    country_code_format = models.CharField(
        "Формат коду країни", max_length=5,
        choices=COUNTRY_CODE_FORMAT_CHOICES, default="iso2",
        help_text="ISO-2: DE, UA, PL — ISO-3: DEU, UKR, POL",
    )

    # ── Фінанси ──────────────────────────────────────────────────────────────
    default_vat_rate = models.DecimalField(
        "ПДВ / MwSt за замовчуванням (%)", max_digits=5, decimal_places=2, default=19,
        help_text="Стандартна ставка ПДВ у % (наприклад 19, 7, 20). "
                  "Підставляється автоматично при створенні нового рахунку-фактури.",
    )

    # ── Одиниці виміру ────────────────────────────────────────────────────────
    weight_unit    = models.CharField(
        "Одиниця ваги", max_length=5,
        choices=WEIGHT_UNIT_CHOICES, default="kg",
        help_text="Використовується у відправленнях та пакувальних матеріалах",
    )
    dimension_unit = models.CharField(
        "Одиниця розмірів", max_length=5,
        choices=DIMENSION_UNIT_CHOICES, default="cm",
        help_text="Використовується для габаритів коробок та посилок",
    )

    class Meta:
        verbose_name = "Системні налаштування"
        verbose_name_plural = "Системні налаштування"

    def __str__(self):
        return self.company_name

    def save(self, *args, **kwargs):
        self.pk = 1  # Singleton — завжди pk=1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1, defaults={
            "company_name": "Моя компанія",
            "enabled_modules": list(ALL_MODULES),
        })
        return obj


# ── DocumentSettings ──────────────────────────────────────────────────────────

DOCUMENT_LANGUAGE_CHOICES = [
    ("en",  "English (EN)"),
    ("de",  "Deutsch / English (DE+EN двомовні)"),
    ("deu", "Deutsch (тільки DE)"),
]

CN23_TYPE_CHOICES = [
    ("SALE",     "Sale / Verkauf — продаж"),
    ("SAMPLE",   "Gift / Geschenk — подарунок"),
    ("TRANSFER", "Sample / Muster — зразок"),
    ("WARRANTY", "Return / Rücksendung — повернення"),
    ("OTHER",    "Other / Sonstiges — інше"),
]


class DocumentSettings(models.Model):
    # ── Загальне ──────────────────────────────────────────────────────────────
    doc_language = models.CharField(
        "Мова документів", max_length=5,
        choices=DOCUMENT_LANGUAGE_CHOICES, default="en",
        help_text="Мова заголовків і тексту у PDF (EN — англійська, DE — двомовні DE/EN).",
    )

    # ── Packing List ──────────────────────────────────────────────────────────
    packing_list_show_prices = models.BooleanField(
        "Показувати ціни у пакувальному листі", default=False,
        help_text="Додається колонка Unit Price та Total у таблицю позицій.",
    )
    packing_list_footer_note = models.TextField(
        "Нотатка внизу пакувального листа", blank=True, default="",
        help_text='Додатковий текст у нижньому колонтитулі. Наприклад: "For customs use only."',
    )

    # ── Proforma Invoice ──────────────────────────────────────────────────────
    proforma_payment_terms = models.CharField(
        "Умови оплати (Proforma)", max_length=255,
        default="Payment within 30 days",
        help_text="Відображається в нижній частині proforma invoice.",
    )
    proforma_notes = models.TextField(
        "Примітки до proforma", blank=True, default="",
        help_text="Додатковий текст у нижній частині proforma invoice.",
    )

    # ── CN23 / Митна декларація ────────────────────────────────────────────────
    customs_default_type = models.CharField(
        "Тип декларації CN23 за замовчуванням", max_length=10,
        choices=CN23_TYPE_CHOICES, default="SALE",
        help_text="Використовується коли у замовленні не вказано тип документа.",
    )
    customs_reason = models.CharField(
        "Опис відправлення (fallback)", max_length=255,
        default="Gewerblich / Commercial", blank=True,
        help_text="Підставляється у CN23 якщо у товарі немає назви для документів.",
    )

    class Meta:
        verbose_name = "Налаштування документів"
        verbose_name_plural = "Налаштування документів"

    def __str__(self):
        return "Налаштування документів"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


# ── NotificationSettings ──────────────────────────────────────────────────────

class NotificationSettings(models.Model):
    # ── Email (SMTP) ──────────────────────────────────────────────────────────
    email_enabled = models.BooleanField(
        "Надсилати email-сповіщення", default=False,
        help_text="Увімкніть після налаштування SMTP нижче.",
    )
    email_host = models.CharField("SMTP Host", max_length=255, default="smtp.gmail.com")
    email_port = models.PositiveSmallIntegerField("SMTP Port", default=587)
    email_use_tls = models.BooleanField(
        "TLS (STARTTLS)", default=True,
        help_text="Зазвичай порт 587. Вимкни якщо використовуєш SSL (порт 465).",
    )
    email_use_ssl = models.BooleanField(
        "SSL", default=False,
        help_text="Зазвичай порт 465. Несумісне з TLS — увімкни лише одне.",
    )
    email_host_user = models.CharField("SMTP Login (email)", max_length=255, blank=True)
    email_host_password = models.CharField("SMTP Password", max_length=255, blank=True)
    email_from = models.CharField(
        "Від кого (From)", max_length=255, blank=True,
        help_text="Якщо порожньо — використовується SMTP Login.",
    )
    email_to = models.TextField(
        "Отримувачі (To)", blank=True,
        help_text="Email-адреси через кому: admin@example.com, boss@company.com",
    )

    # ── Sync result alerts ────────────────────────────────────────────────────
    sync_result_email = models.BooleanField(
        "Email: звіт після синхронізації", default=False,
        help_text="Надсилати email якщо DigiKey або авто-трекінг знайшли зміни.",
    )
    sync_result_telegram = models.BooleanField(
        "Telegram: звіт після синхронізації", default=False,
        help_text="Надсилати Telegram якщо DigiKey або авто-трекінг знайшли зміни.",
    )
    sync_skip_if_no_changes = models.BooleanField(
        "Не надсилати якщо змін не виявлено", default=True,
        help_text="Увімкнено — сповіщення надходить лише коли є реальні зміни.",
    )

    # ── Stock alerts ──────────────────────────────────────────────────────────
    stock_alerts_enabled = models.BooleanField(
        "Critical stock alerts", default=True,
        help_text="Товари з запасом менше 1.5 місяців (на основі продажів за 90 днів).",
    )

    # ── Deadline alerts ───────────────────────────────────────────────────────
    deadline_alerts_enabled = models.BooleanField(
        "Overdue deadline alerts", default=True,
        help_text="Замовлення з простроченим дедлайном доставки.",
    )
    deadline_overdue_days = models.PositiveSmallIntegerField(
        "Оповіщати через N днів після дедлайну", default=0,
        help_text="0 = в день дедлайну і кожного наступного. 1 = наступного дня і далі.",
    )

    # ── Anti-spam ─────────────────────────────────────────────────────────────
    alert_min_interval_hours = models.PositiveSmallIntegerField(
        "Мінімальний інтервал між надсиланнями (год)", default=12,
        help_text="Захист від дублювання при автоматичному запуску через cron.",
    )
    last_alert_sent = models.DateTimeField(
        "Останнє надсилання", null=True, blank=True,
    )

    # ── New order alerts ──────────────────────────────────────────────────────
    new_order_email = models.BooleanField(
        "Email: нове замовлення", default=False,
        help_text="Надсилати email коли надходить нове замовлення.",
    )
    new_order_telegram = models.BooleanField(
        "Telegram: нове замовлення", default=False,
        help_text="Надсилати Telegram коли надходить нове замовлення.",
    )

    # ── Status change alerts ───────────────────────────────────────────────────
    status_change_email = models.BooleanField(
        "Email: зміна статусу", default=False,
        help_text="Надсилати email при зміні статусу замовлення.",
    )
    status_change_telegram = models.BooleanField(
        "Telegram: зміна статусу", default=False,
        help_text="Надсилати Telegram при зміні статусу замовлення.",
    )
    notify_on_processing = models.BooleanField(
        "→ В обробці", default=False,
        help_text="Сповістити коли статус змінився на «В обробці».",
    )
    notify_on_shipped = models.BooleanField(
        "→ Відправлено", default=True,
        help_text="Сповістити коли замовлення відправлено.",
    )
    notify_on_delivered = models.BooleanField(
        "→ Доставлено", default=True,
        help_text="Сповістити коли замовлення доставлено.",
    )
    notify_on_cancelled = models.BooleanField(
        "→ Скасовано", default=False,
        help_text="Сповістити при скасуванні замовлення.",
    )

    # ── Telegram ──────────────────────────────────────────────────────────────
    telegram_enabled = models.BooleanField(
        "Надсилати Telegram-сповіщення", default=False,
        help_text="Увімкніть після налаштування Bot Token та Chat ID нижче.",
    )
    telegram_bot_token = models.CharField(
        "Bot Token", max_length=200, blank=True,
        help_text="Отримати у @BotFather: /newbot → скопіювати токен вигляду 123456:ABC-...",
    )
    telegram_chat_id = models.CharField(
        "Chat ID", max_length=100, blank=True,
        help_text=(
            "ID чату, групи або каналу. "
            "Для каналу: @mychannel або -100xxxxxxxxxx. "
            "Для особистого чату: числовий ID (дізнатись через @userinfobot)."
        ),
    )

    class Meta:
        verbose_name = "Налаштування сповіщень"
        verbose_name_plural = "Налаштування сповіщень"

    def __str__(self):
        return "Налаштування сповіщень"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
