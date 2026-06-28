"""
bots/models.py — Моделі для управління ботами і API інтеграціями
"""
from django.db import models
from django.utils import timezone
from django.core.validators import RegexValidator


class DigiKeyConfig(models.Model):
    """
    Singleton (pk=1) — налаштування DigiKey Marketplace API.

    OAuth2 Client Credentials (2-legged):
      POST https://api.digikey.com/v1/oauth2/token
      → access_token (expires in 10 хв, кешується)

    Orders endpoint:
      GET https://api.digikey.com/orderstatus/v4/orders
    """
    # ── Credentials ───────────────────────────────────────────────────────────
    client_id     = models.CharField("Client ID",     max_length=200, blank=True, default="",
                                     help_text="DigiKey Developer Portal → My Apps → Client ID")
    client_secret = models.CharField("Client Secret", max_length=200, blank=True, default="",
                                     help_text="DigiKey Developer Portal → My Apps → Client Secret")
    account_id    = models.CharField("Account ID",    max_length=100, blank=True, default="",
                                     help_text="X-DIGIKEY-Account-Id (з діагностики API або DigiKey підтримки)")

    # ── Locale ────────────────────────────────────────────────────────────────
    locale_site     = models.CharField("Locale Site",     max_length=10, default="DE",
                                       help_text="DE / US / CA / GB / AT / CH / PL …")
    locale_language = models.CharField("Locale Language", max_length=10, default="en",
                                       help_text="en / de / fr …")
    locale_currency = models.CharField("Locale Currency", max_length=8,  default="EUR",
                                       help_text="EUR / USD / GBP …")

    # ── Синхронізація ─────────────────────────────────────────────────────────
    sync_enabled          = models.BooleanField("Синхронізація увімкнена", default=False)
    sync_interval_minutes = models.PositiveSmallIntegerField(
        "Інтервал синхронізації (хвилин)", default=30,
        help_text="Рекомендовано: 15–60 хв"
    )

    # ── Авто-перевірка статусу staged → published ─────────────────────────────
    poll_enabled          = models.BooleanField(
        "Авто-перевірка статусу (staged → published)", default=False,
        help_text="Автоматично перевіряти чи затвердив DigiKey лістинги у статусі 'Очікує затвердження'"
    )
    poll_interval_minutes = models.PositiveSmallIntegerField(
        "Інтервал перевірки (хвилин)", default=60,
        help_text="Рекомендовано: 30–120 хв. Cron: python manage.py poll_dk_status"
    )
    last_polled_at = models.DateTimeField(
        "Остання авто-перевірка статусу", null=True, blank=True
    )
    poll_notify_telegram = models.BooleanField(
        "Telegram — при затвердженні", default=False,
        help_text="Надіслати Telegram-повідомлення коли DigiKey затвердить лістинг (staged → published)"
    )
    poll_notify_email = models.BooleanField(
        "Email — при затвердженні", default=False,
        help_text="Надіслати email-повідомлення коли DigiKey затвердить лістинг (staged → published)"
    )
    poll_notify_email_to = models.CharField(
        "Email одержувачів (через кому)", max_length=500, blank=True, default="",
        help_text="Порожньо = використати адресу з загальних налаштувань сповіщень"
    )

    # ── Авто-стягування даних лістингів з DigiKey ─────────────────────────────
    pull_enabled        = models.BooleanField(
        "Авто-стягування лістингів", default=False,
        help_text="Автоматично оновлювати дані лістингів (ціни, назви, атрибути) з DigiKey за розкладом"
    )
    pull_interval_hours = models.PositiveSmallIntegerField(
        "Інтервал авто-стягування (годин)", default=24,
        help_text="Рекомендовано: 12–48 год. Запускати cron: python manage.py pull_dk_listings"
    )
    last_pulled_at = models.DateTimeField(
        "Останнє авто-стягування", null=True, blank=True
    )
    sync_order_status = models.BooleanField(
        "Оновлювати статус замовлення при синхронізації", default=True,
        help_text=(
            "Якщо увімкнено — статус замовлення оновлюється зі статусу DigiKey "
            "(тільки якщо новий статус вищий за поточний). "
            "Вимкніть якщо статус керується трекінгом перевізника (UPS/DHL) "
            "або виставляється вручну."
        ),
    )
    create_product_if_missing = models.BooleanField(
        "Створювати товар якщо нема на складі", default=False,
        help_text=(
            "При імпорті лістингів («🆕 Створити лістинги»): якщо товар з таким SKU "
            "відсутній на складі — автоматично створити картку товару і прив'язати лістинг. "
            "Якщо вимкнено — SKU без товару пропускаються (виводяться у звіті)."
        ),
    )
    use_sandbox = models.BooleanField(
        "Sandbox режим (тестовий)", default=True,
        help_text="sandbox-api.digikey.com — для тестування без реальних замовлень"
    )
    last_synced_at = models.DateTimeField("Остання успішна синхронізація", null=True, blank=True)

    # ── OAuth token cache 2-legged (не редагувати вручну) ─────────────────────
    access_token     = models.TextField("Access Token (кеш)", blank=True, default="")
    token_expires_at = models.DateTimeField("Токен дійсний до (UTC)", null=True, blank=True)

    # ── OAuth 3-legged (Marketplace API) ──────────────────────────────────────
    marketplace_access_token  = models.TextField("Marketplace Access Token",  blank=True, default="")
    marketplace_refresh_token = models.TextField("Marketplace Refresh Token", blank=True, default="")
    marketplace_token_expires_at = models.DateTimeField(
        "Marketplace Token дійсний до", null=True, blank=True
    )

    # ── Авто-підтвердження (Marketplace) ─────────────────────────────────────
    AUTO_CONFIRM_NEVER    = "never"
    AUTO_CONFIRM_ALWAYS   = "always"
    AUTO_CONFIRM_IN_STOCK = "in_stock"
    AUTO_CONFIRM_CHOICES  = [
        ("never",    "Мануально — не підтверджувати автоматично"),
        ("always",   "Завжди — підтверджувати одразу при надходженні"),
        ("in_stock", "Якщо є на складі — підтверджувати тільки якщо всі товари є"),
    ]
    auto_confirm_mode = models.CharField(
        "Авто-підтвердження на DigiKey",
        max_length=20,
        choices=AUTO_CONFIRM_CHOICES,
        default="never",
        help_text=(
            "Що робити коли нове Marketplace замовлення надходить: "
            "підтвердити одразу на DigiKey, перевірити залишки на складі, "
            "або залишити для ручного підтвердження."
        ),
    )

    # ── Публічний URL сайту ───────────────────────────────────────────────────
    public_base_url = models.CharField(
        "Публічний URL сайту", max_length=200, blank=True, default="",
        help_text="Базовий URL без слеша на кінці. Приклад: https://akustik.synology.me:81 — "
                  "використовується для OAuth Callback та Webhook URL."
    )

    # ── Webhook ───────────────────────────────────────────────────────────────
    webhook_enabled = models.BooleanField("Webhook увімкнений", default=False)
    webhook_secret  = models.CharField(
        "Webhook Secret", max_length=200, blank=True, default="",
        help_text="Довільний рядок — вкажи той самий і в DigiKey dev portal → Webhooks"
    )

    class Meta:
        verbose_name        = "DigiKey — Конфігурація"
        verbose_name_plural = "DigiKey — Конфігурація"

    def __str__(self):
        return "DigiKey API"

    def save(self, *args, **kwargs):
        self.pk = 1
        # Якщо змінились credentials або режим sandbox — скидаємо кеш токена
        if self.pk:
            try:
                old = DigiKeyConfig.objects.get(pk=1)
                if (old.client_id != self.client_id
                        or old.client_secret != self.client_secret
                        or old.use_sandbox != self.use_sandbox):
                    self.access_token              = ""
                    self.token_expires_at          = None
                    self.marketplace_access_token  = ""
                    self.marketplace_refresh_token = ""
                    self.marketplace_token_expires_at = None
            except DigiKeyConfig.DoesNotExist:
                pass
        super().save(*args, **kwargs)

    # ── Marketplace Supplier ID ───────────────────────────────────────────────
    marketplace_supplier_id = models.CharField(
        "Marketplace Vendor ID", max_length=64, blank=True, default="",
        help_text="Числовий Vendor ID з supplier.digikey.com → Account → Company Profile "
                  "(напр. 3228)"
    )

    # ── Messages check settings ───────────────────────────────────────────────
    msg_check_enabled = models.BooleanField(
        "Авто-перевірка повідомлень", default=False,
        help_text="Автоматично перевіряти нові повідомлення від покупців DigiKey"
    )
    msg_check_interval = models.PositiveSmallIntegerField(
        "Інтервал перевірки (хвилин)", default=15,
        help_text="Рекомендовано: 5–60 хв. Використовується cron_runner (MSG_CHECK_INTERVAL)."
    )
    msg_last_checked_at = models.DateTimeField(
        "Остання перевірка повідомлень", null=True, blank=True
    )
    msg_topics_cache = models.JSONField(
        "Кеш повідомлень", null=True, blank=True, default=None,
        help_text="Зберігається автоматично після check_digikey_messages або оновлення в хабі."
    )
    msg_cache_at = models.DateTimeField(
        "Кеш оновлено", null=True, blank=True
    )

    # ── API Error Handling & Retry Strategy ───────────────────────────────────
    api_retry_count = models.PositiveSmallIntegerField(
        "Кількість спроб при збої", default=3,
        help_text="Скільки разів повторити запит перед тим як надіслати сповіщення (1–10)"
    )
    api_retry_delay = models.PositiveSmallIntegerField(
        "Затримка між спробами (сек)", default=10,
        help_text="Секунди між повторними спробами підключення до DigiKey API"
    )
    api_notify_on_error = models.BooleanField(
        "Сповіщати при збоях з'єднання", default=True,
        help_text="Telegram/Email якщо синк падає після всіх спроб (timeout, network error)"
    )
    api_notify_on_reauth = models.BooleanField(
        "Сповіщати коли потрібна повторна авторизація", default=True,
        help_text="Якщо refresh_token недійсний — надіслати сповіщення з посиланням на OAuth"
    )
    api_notify_telegram = models.BooleanField(
        "Telegram сповіщення", default=True
    )
    api_notify_telegram_mode = models.CharField(
        "Кому в Telegram", max_length=10,
        choices=[('group', 'Загальний чат'), ('private', 'Приватно суперадміну')],
        default='group'
    )
    api_notify_email = models.BooleanField(
        "Email сповіщення", default=True
    )
    api_notify_email_to = models.CharField(
        "Email одержувачів (через кому)", max_length=500, blank=True, default="",
        help_text="Порожньо = взяти адресу з загальних налаштувань сповіщень"
    )

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


# ── DigiKey message seen tracker ──────────────────────────────────────────────

class DigiKeyMessageSeen(models.Model):
    """Зберігає id останнього побаченого повідомлення по кожній темі — для нотифікацій."""
    topic_id        = models.CharField("Topic ID", max_length=64, unique=True)
    last_message_id = models.CharField("Last Message ID", max_length=64, blank=True, default="")
    last_seen_at    = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name        = "DigiKey Message Seen"
        verbose_name_plural = "DigiKey Messages Seen"

    def __str__(self):
        return f"Topic {self.topic_id}"


# ── Background task tracker ───────────────────────────────────────────────────

class BotTask(models.Model):
    """Tracks state of long-running admin background operations."""
    IDLE    = 'idle'
    RUNNING = 'running'
    DONE    = 'done'
    ERROR   = 'error'

    name             = models.CharField(max_length=64, unique=True)
    status           = models.CharField(max_length=16, default=IDLE)
    started_at       = models.DateTimeField(null=True, blank=True)
    finished_at      = models.DateTimeField(null=True, blank=True)
    progress         = models.CharField(max_length=300, blank=True, default='')
    message          = models.TextField(blank=True, default='')
    cancel_requested = models.BooleanField(default=False)

    class Meta:
        verbose_name = 'Background Task'

    def __str__(self):
        return f"{self.name} [{self.status}]"

    @classmethod
    def start(cls, name: str) -> 'BotTask':
        from django.utils import timezone
        task, _ = cls.objects.get_or_create(name=name)
        task.status           = cls.RUNNING
        task.started_at       = timezone.now()
        task.finished_at      = None
        task.progress         = ''
        task.message          = ''
        task.cancel_requested = False
        task.save()
        return task

    def set_progress(self, text: str):
        BotTask.objects.filter(pk=self.pk).update(progress=text[:300])

    def is_cancelled(self) -> bool:
        return BotTask.objects.values_list('cancel_requested', flat=True).get(pk=self.pk)

    def finish(self, message: str = '', error: bool = False):
        from django.utils import timezone
        self.status      = self.ERROR if error else self.DONE
        self.finished_at = timezone.now()
        self.message     = message
        self.save(update_fields=['status', 'finished_at', 'message'])


# ── DigiKey Marketplace Listing ───────────────────────────────────────────────

class DigiKeyListing(models.Model):
    """Лістинг товару на DigiKey Marketplace.

    Зберігає всі поля для публікації: назва, опис, категорія,
    технічні атрибути (фільтри), цінові тири, статус синхронізації.
    """

    SYNC_DRAFT     = 'draft'
    SYNC_STAGED    = 'staged'
    SYNC_PUBLISHED = 'published'
    SYNC_ERROR     = 'error'
    SYNC_CHOICES = [
        ('draft',     'Чернетка'),
        ('staged',    '⏳ Очікує затвердження'),
        ('published', 'Опубліковано'),
        ('error',     'Помилка'),
    ]

    CAT_FILTER    = 'filter'
    CAT_ANTENNA   = 'antenna'
    CAT_CABLE     = 'cable'
    CAT_CONNECTOR = 'connector'
    CAT_OTHER     = 'other'
    CAT_CHOICES = [
        ('filter',    'RF Filter'),
        ('antenna',   'Antenna'),
        ('cable',     'Cable Assembly'),
        ('connector', 'Connector / Adapter'),
        ('other',     'Інше'),
    ]

    product = models.OneToOneField(
        'inventory.Product', on_delete=models.CASCADE,
        related_name='dk_listing', verbose_name='Товар',
        null=True, blank=True,
    )
    category_type = models.CharField(
        'Категорія', max_length=20, choices=CAT_CHOICES, default='other'
    )

    # ── Product stage fields ───────────────────────────────────────────────────
    dk_product_id   = models.CharField('DK Product ID',  max_length=36, blank=True, default='',
                                        help_text='UUID — заповнюється автоматично після публікації')
    dk_category_id  = models.CharField('DK Category ID', max_length=256, blank=True, default='',
                                        help_text='Код категорії DigiKey (напр. "29" для RF Filters). '
                                                  'Знайти в DigiKey Marketplace Portal → Product Catalog')
    dk_category_name = models.CharField('Назва категорії DK', max_length=200, blank=True, default='',
                                         help_text='Заповнюється автоматично з DigiKey')
    dk_title        = models.CharField('Назва (DK)', max_length=50,
                                        help_text='Назва товару на DigiKey, макс. 50 символів')
    dk_description  = models.TextField('Опис товару (DK)', max_length=2048,
                                        help_text='Повний опис товару, макс. 2048 символів')
    dk_manufacturer = models.CharField('Виробник (DK)', max_length=50, blank=True, default='',
                                        help_text='Назва виробника, макс. 50 символів')
    dk_image_url    = models.URLField('Фото (URL)', blank=True, default='',
                                       help_text='Пряме посилання на зображення товару')
    dk_datasheet_url = models.URLField('Datasheet (URL)', blank=True, default='',
                                        help_text='Пряме посилання на datasheet')

    # ── Offer fields ───────────────────────────────────────────────────────────
    dk_offer_id      = models.CharField('DK Offer ID', max_length=36, blank=True, default='',
                                         help_text='UUID — заповнюється автоматично після публікації')
    dk_supplier_sku  = models.CharField('Supplier SKU (DK)', max_length=50, blank=True, default='',
                                         help_text='Порожньо = використовує SKU товару')
    dk_min_order_qty = models.PositiveIntegerField('MOQ (мін. кількість)', default=1,
                                                    help_text='Мінімальна кількість для замовлення (≥ 1)')
    dk_lead_time_days = models.PositiveIntegerField('Термін відвантаження (дні)', default=11,
                                                     help_text='Кількість днів від замовлення до відправки')
    dk_qty_alert     = models.PositiveIntegerField('Мін. залишок (алерт)', default=3,
                                                    help_text='При залишку нижче цього — DigiKey виводить попередження')
    dk_quantity_override = models.IntegerField(
        'Кількість для DigiKey', null=True, blank=True,
        help_text='Кількість що передається на DigiKey. Якщо порожньо — використовується залишок складу. '
                  'Оновлюється автоматично при «📥 Стягнути з DigiKey».'
    )
    dk_quantity_available = models.IntegerField('Залишок на DigiKey', null=True, blank=True,
                                                help_text='Кількість, яку бачить покупець на DigiKey (оновлюється при імпорті)')
    dk_is_active     = models.BooleanField('Активне на DigiKey', default=True)

    # ── Volume pricing (up to 9 tiers) ────────────────────────────────────────
    # Stored as JSON: [{"qty": 1, "price": 11.99}, {"qty": 10, "price": 11.53}, ...]
    dk_prices = models.JSONField('Цінові тири', default=list,
                                  help_text='Список цінових тирів: [{"qty": 1, "price": 11.99}, ...]')

    # ── Filter attributes (category_type = "filter") ───────────────────────────
    # DK attribute codes in parentheses — passed as additionalFields in API
    fa_frequency      = models.CharField('Frequency (139)',       max_length=100, blank=True, default='',
                                          help_text='напр. "1.12GHz Center"')
    fa_bandwidth      = models.CharField('Bandwidth (398)',       max_length=100, blank=True, default='',
                                          help_text='напр. "210MHz"')
    fa_filter_type    = models.CharField('Filter Type (21)',      max_length=100, blank=True, default='',
                                          help_text='Band Pass / Band Stop / Low Pass / High Pass / Notch')
    fa_ripple         = models.CharField('Ripple (428)',          max_length=100, blank=True, default='',
                                          help_text='напр. "1.6dB"')
    fa_insertion_loss = models.CharField('Insertion Loss (327)',  max_length=100, blank=True, default='',
                                          help_text='напр. "4dB"')
    fa_mounting_type  = models.CharField('Mounting Type (69)',    max_length=100, blank=True, default='',
                                          help_text='напр. "Free Hanging (In-Line)"')
    fa_package_case   = models.CharField('Package / Case (16)',   max_length=100, blank=True, default='',
                                          help_text='напр. "Inline, SMA Connection, F and M"')
    fa_size_dimension = models.CharField('Size / Dimension (46)', max_length=200, blank=True, default='',
                                          help_text='напр. "1.496\\" L x 1.338\\" W (38mm x 34mm)"')
    fa_height_max     = models.CharField('Height Max (966)',      max_length=100, blank=True, default='',
                                          help_text='напр. "0.063\\" (1.60mm)"')

    # ── Required product attributes (codes hardcoded from Custom Fields API) ───
    dk_packaging        = models.CharField(
        'Packaging', max_length=100, blank=True, default='',
        help_text='Обов\'язково. Значення: Tape & Reel (TR) / Cut Tape (CT) / Bulk / Digi-Reel®. '
                  'Код атрибута DigiKey: "packaging"')
    dk_lifecycle_status = models.CharField(
        'Product Life Cycle Status', max_length=100, blank=True, default='Active',
        help_text='Обов\'язково. Значення: Active / Obsolete / Last Time Buy / Not For New Design. '
                  'Код атрибута DigiKey: "productLifecycleStatus"')

    # ── All DigiKey attributes (raw pull from Products API) ───────────────────
    dk_attributes = models.JSONField(
        'Всі атрибути DigiKey', default=dict, blank=True,
        help_text='additionalFields з DigiKey Products API у форматі {"code": "value", ...}. '
                  'Заповнюється кнопкою «📥 Стягнути поля з DigiKey».'
    )

    # ── Price history ──────────────────────────────────────────────────────────
    price_delta_pct = models.FloatField(
        'Остання зміна цін (%)', null=True, blank=True,
        help_text='Останній застосований масовий відсоток зміни цін (наприклад +10 або -5).'
    )
    dk_price_min = models.FloatField(
        'Ціна (1 шт.)', null=True, blank=True, db_index=True,
        help_text='Автоматично: ціна за 1 шт. з dk_prices (для сортування)'
    )

    # ── Sync status ────────────────────────────────────────────────────────────
    sync_status    = models.CharField('Статус', max_length=20,
                                       choices=SYNC_CHOICES, default='draft')
    last_synced_at = models.DateTimeField('Остання синхронізація', null=True, blank=True)
    last_error     = models.TextField('Остання помилка', blank=True, default='')
    last_error_at  = models.DateTimeField('Час помилки', null=True, blank=True)
    error_log      = models.JSONField(
        'Журнал помилок', default=list, blank=True,
        help_text='Автоматично: хронологія помилок синхронізації (макс. 20)'
    )
    ignored_quality_fields = models.JSONField(
        'Ігноровані поля перевірок', default=list, blank=True,
        help_text='Поля, для яких локальні попередження вимкнено вручну'
    )
    created_at     = models.DateTimeField(auto_now_add=True)
    updated_at     = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name        = 'DigiKey — Лістинг'
        verbose_name_plural = 'DigiKey — Лістинги'
        ordering            = ['product__sku']

    def __str__(self):
        sku = self.product.sku if self.product_id else '—'
        return f"{sku} [{self.get_sync_status_display()}]"

    def get_supplier_sku(self):
        return self.dk_supplier_sku or (self.product.sku if self.product_id else '')

    def get_stock_qty(self):
        """Поточний залишок на складі (з InventoryTransaction)."""
        if not self.product_id:
            return 0
        from django.db.models import Sum
        from inventory.models import InventoryTransaction
        result = InventoryTransaction.objects.filter(
            product_id=self.product_id
        ).aggregate(total=Sum('qty'))
        return max(0, int(result['total'] or 0))

    def get_dk_quantity(self):
        """Кількість для передачі на DigiKey: override якщо задано, інакше склад."""
        if self.dk_quantity_override is not None:
            return self.dk_quantity_override
        return self.get_stock_qty()

    def save(self, *args, **kwargs):
        # Keep dk_price_min synced for fast DB-level sorting.
        # Only recalculate when dk_prices is actually being written.
        update_fields = kwargs.get('update_fields')
        if update_fields is None or 'dk_prices' in update_fields:
            try:
                tiers = [t for t in (self.dk_prices or []) if t.get('price') not in (None, '')]
                if tiers:
                    min_qty_tier = min(tiers, key=lambda t: int(t.get('qty') or 0))
                    self.dk_price_min = float(min_qty_tier['price'])
                else:
                    self.dk_price_min = None
            except Exception:
                self.dk_price_min = None
            # Make sure dk_price_min is included when update_fields is used
            if update_fields is not None and 'dk_price_min' not in update_fields:
                kwargs['update_fields'] = list(update_fields) + ['dk_price_min']
        super().save(*args, **kwargs)

    def get_prices_api(self):
        """Prices list for API: [{"quantityBreak": N, "price": X.XX}, ...]"""
        return [
            {"quantityBreak": int(t["qty"]), "price": float(t["price"])}
            for t in (self.dk_prices or [])
            if t.get("qty") and t.get("price")
        ]

    def get_common_attributes_api(self):
        """Common required attributes: Packaging + Product Life Cycle Status."""
        attrs = []
        if self.dk_packaging:
            attrs.append({"code": "packaging", "type": "String", "value": self.dk_packaging})
        if self.dk_lifecycle_status:
            attrs.append({"code": "productLifecycleStatus", "type": "String", "value": self.dk_lifecycle_status})
        return attrs

    def get_filter_attributes_api(self):
        """additionalFields list for API (filter category only) + common attributes."""
        mapping = [
            ('fa_frequency',      '139'),
            ('fa_bandwidth',      '398'),
            ('fa_filter_type',    '21'),
            ('fa_ripple',         '428'),
            ('fa_insertion_loss', '327'),
            ('fa_mounting_type',  '69'),
            ('fa_package_case',   '16'),
            ('fa_size_dimension', '46'),
            ('fa_height_max',     '966'),
        ]
        attrs = self.get_common_attributes_api()
        for field, code in mapping:
            val = getattr(self, field, '').strip()
            if val:
                attrs.append({"code": code, "type": "String", "value": val})
        return attrs

    def get_all_attributes_api(self):
        """additionalFields for DigiKey upsert — all category types.

        Sends: common (packaging + lifecycle) + category-specific model fields
        (fa_* for filter) + all numeric-coded entries from dk_attributes that
        are not already included. Works for filter, cable, antenna, connector, other.
        """
        if self.category_type == 'filter':
            attrs = self.get_filter_attributes_api()
        else:
            attrs = self.get_common_attributes_api()

        added_codes = {a['code'] for a in attrs}
        for code, value in (self.dk_attributes or {}).items():
            code_str = str(code)
            if (code_str.isdigit()
                    and code_str not in added_codes
                    and value not in (None, '', '-')):
                attrs.append({"code": code_str, "type": "String", "value": str(value)})
                added_codes.add(code_str)
        return attrs


class DigiKeyPriceLog(models.Model):
    """Лог масових змін цін DigiKey лістингу."""
    listing     = models.ForeignKey(
        DigiKeyListing, on_delete=models.CASCADE,
        related_name='price_logs', verbose_name='Лістинг',
    )
    applied_at  = models.DateTimeField('Застосовано', auto_now_add=True, db_index=True)
    delta_pct   = models.FloatField('Зміна (%)')
    old_prices  = models.JSONField('Старі ціни')
    new_prices  = models.JSONField('Нові ціни')
    user        = models.CharField('Користувач', max_length=150, blank=True, default='')

    class Meta:
        verbose_name        = 'Лог зміни цін'
        verbose_name_plural = 'Лог змін цін'
        ordering            = ['-applied_at']

    def __str__(self):
        sign = '+' if self.delta_pct >= 0 else ''
        return f"{self.listing.product.sku} {sign}{self.delta_pct:.1f}% @ {self.applied_at:%Y-%m-%d %H:%M}"


class AIAnalysisLog(models.Model):
    """Зберігає кожен запуск AI-аналізу лістингу: рекомендації, якість, знімок продажів та outcome."""

    listing   = models.ForeignKey(
        DigiKeyListing, on_delete=models.CASCADE,
        related_name='ai_analyses', verbose_name='Лістинг',
    )
    run_at = models.DateTimeField('Проведено', auto_now_add=True, db_index=True)
    run_by = models.CharField('Користувач', max_length=150, blank=True, default='')

    # ── AI output ────────────────────────────────────────────────────────────
    strategy             = models.CharField('Стратегія', max_length=30, blank=True)
    strategy_name        = models.CharField('Назва стратегії', max_length=120, blank=True)
    quality_score        = models.SmallIntegerField('Оцінка якості', null=True, blank=True)
    quality_summary      = models.CharField('Резюме якості', max_length=300, blank=True)
    recommended_prices   = models.JSONField('Рекомендовані ціни', default=list)
    quality_issues       = models.JSONField('Проблеми якості', default=list)
    local_issues         = models.JSONField('Локальні перевірки', default=list)
    expected_impact      = models.CharField('Очікуваний ефект', max_length=300, blank=True)
    post_change_advice   = models.CharField('Порада після зміни', max_length=300, blank=True)
    price_change_summary = models.CharField('Зміна цін (резюме)', max_length=200, blank=True)
    skipped_ai           = models.BooleanField('AI не викликався', default=False)

    # ── Context snapshot at time of analysis ─────────────────────────────────
    prices_before  = models.JSONField('Ціни на момент аналізу', default=list)
    sales_snapshot = models.JSONField('Знімок продажів', default=dict)

    # ── Outcome tracking ─────────────────────────────────────────────────────
    prices_applied  = models.BooleanField('Ціни застосовані', default=False)
    applied_at      = models.DateTimeField('Застосовано', null=True, blank=True)
    applied_by      = models.CharField('Застосував', max_length=150, blank=True, default='')
    outcome_checked = models.BooleanField('Результат перевірено', default=False)
    outcome_notes   = models.TextField('Нотатки результату', blank=True, default='')

    class Meta:
        verbose_name        = 'AI Аналіз'
        verbose_name_plural = 'AI Аналізи'
        ordering            = ['-run_at']

    def __str__(self):
        return f"{self.listing.product.sku} AI [{self.run_at:%Y-%m-%d %H:%M}] Q={self.quality_score}"


class Bot(models.Model):
    """Бот для автоматизації (парсинг, синхронізація, тощо)."""
    
    class BotType(models.TextChoices):
        DIGIKEY = "digikey", "DigiKey Parser"
        MOUSER  = "mouser",  "Mouser Parser"
        CUSTOM  = "custom",  "Custom Script"
    
    class Status(models.TextChoices):
        ACTIVE  = "active",  "Активний"
        PAUSED  = "paused",  "Призупинений"
        ERROR   = "error",   "Помилка"
        RUNNING = "running", "Виконується"
    
    # ── Основна інформація ────────────────────────────────────────────────────
    name        = models.CharField("Назва", max_length=100)
    bot_type    = models.CharField("Тип", max_length=20, choices=BotType.choices, default=BotType.DIGIKEY)
    description = models.TextField("Опис", blank=True, default="")
    is_active   = models.BooleanField("Увімкнено", default=True)
    status      = models.CharField("Статус", max_length=20, choices=Status.choices, default=Status.PAUSED)
    
    # ── Credentials (зашифровані в production) ────────────────────────────────
    login       = models.CharField("Логін", max_length=200, blank=True, default="")
    password    = models.CharField("Пароль", max_length=200, blank=True, default="",
                                   help_text="⚠️ Буде зашифровано при збереженні")
    api_key     = models.CharField("API ключ", max_length=500, blank=True, default="")
    
    # ── Розклад виконання ─────────────────────────────────────────────────────
    schedule_enabled = models.BooleanField("Авто-запуск", default=False)
    schedule_cron    = models.CharField(
        "Розклад (cron)",
        max_length=100,
        blank=True,
        default="0 */6 * * *",  # Кожні 6 годин
        help_text="Формат: хвилина година день місяць день_тижня. Приклад: 0 */6 * * * (кожні 6 год)",
        validators=[
            RegexValidator(
                regex=r'^[\d\*\/\-\,\s]+$',
                message='Неправильний формат cron'
            )
        ]
    )
    schedule_interval_minutes = models.IntegerField(
        "Інтервал (хвилин)",
        null=True,
        blank=True,
        help_text="Альтернатива cron: запуск кожні N хвилин"
    )
    
    # ── Статистика виконання ──────────────────────────────────────────────────
    last_run_at     = models.DateTimeField("Останній запуск", null=True, blank=True)
    last_run_status = models.CharField("Статус останнього запуску", max_length=20, blank=True, default="")
    last_run_duration = models.IntegerField("Тривалість (сек)", null=True, blank=True)
    next_run_at     = models.DateTimeField("Наступний запуск", null=True, blank=True)
    total_runs      = models.IntegerField("Всього запусків", default=0)
    success_runs    = models.IntegerField("Успішних запусків", default=0)
    error_runs      = models.IntegerField("Помилок", default=0)
    
    # ── Метадані ──────────────────────────────────────────────────────────────
    created_at  = models.DateTimeField("Створено", auto_now_add=True)
    updated_at  = models.DateTimeField("Оновлено", auto_now=True)
    created_by  = models.ForeignKey("auth.User", on_delete=models.SET_NULL, 
                                    null=True, blank=True, verbose_name="Автор")
    
    class Meta:
        verbose_name = "Бот"
        verbose_name_plural = "Боти"
        ordering = ["-created_at"]
    
    def __str__(self):
        return f"{self.name} ({self.get_bot_type_display()})"
    
    def save(self, *args, **kwargs):
        """Шифрування паролю при збереженні (TODO в production)."""
        # TODO: Додати шифрування через cryptography.fernet
        super().save(*args, **kwargs)
    
    def can_run(self):
        """Чи можна запустити бота зараз."""
        return self.is_active and self.status != self.Status.RUNNING
    
    def calculate_next_run(self):
        """Розрахунок наступного запуску по cron/interval."""
        if not self.schedule_enabled:
            return None
        
        if self.schedule_interval_minutes:
            # Простий інтервал
            from datetime import timedelta
            base = self.last_run_at or timezone.now()
            return base + timedelta(minutes=self.schedule_interval_minutes)
        
        # TODO: Парсинг cron expression через croniter
        return None


class BotLog(models.Model):
    """Лог виконання бота."""
    
    class LogLevel(models.TextChoices):
        INFO    = "info",    "Інформація"
        SUCCESS = "success", "Успіх"
        WARNING = "warning", "Попередження"
        ERROR   = "error",   "Помилка"
    
    bot         = models.ForeignKey(Bot, on_delete=models.CASCADE, related_name="logs", verbose_name="Бот")
    started_at  = models.DateTimeField("Початок", auto_now_add=True)
    finished_at = models.DateTimeField("Кінець", null=True, blank=True)
    duration    = models.IntegerField("Тривалість (сек)", null=True, blank=True)
    level       = models.CharField("Рівень", max_length=20, choices=LogLevel.choices, default=LogLevel.INFO)
    message     = models.TextField("Повідомлення", blank=True, default="")
    details     = models.JSONField("Деталі", null=True, blank=True)
    
    # Результати виконання
    items_processed = models.IntegerField("Оброблено записів", default=0)
    items_created   = models.IntegerField("Створено", default=0)
    items_updated   = models.IntegerField("Оновлено", default=0)
    items_failed    = models.IntegerField("Помилок", default=0)
    
    class Meta:
        verbose_name = "Лог бота"
        verbose_name_plural = "Логи ботів"
        ordering = ["-started_at"]
    
    def __str__(self):
        return f"{self.bot.name} — {self.started_at.strftime('%d.%m.%Y %H:%M')}"
