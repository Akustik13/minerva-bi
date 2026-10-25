"""
Інтернет-магазини: кілька магазинів (сайт, маркетплейс …), у кожного свій асортимент і ціни.

  Shop         — магазин; slug = джерело замовлень (SalesOrder.source); ключ API прив'язується до магазину
  ShopListing  — товар у конкретному магазині: видимість + ціна (порожньо = «Ціна продажу» товару)
  ShopPriceTier— ступінь ціни позиції за кількістю (як на DigiKey: від 10 / 100 … шт.)
  ShopSettings — шаблон ступенів, націнка, округлення (спільні для всіх магазинів)
  ShopCustomer — акаунт покупця (особистий кабінет на сайті) + токени листів, сесії
  ShopOrder    — замовлення з сайту: акаунт покупця, ставка ПДВ, платіжна адреса
  ShopPayment  — онлайн-оплата замовлення (Stripe Checkout): сесія, PaymentIntent, статус
  ProductMedia — фото / даташит товару для сайту: звідки взято (URL → файл Minerva), перевірена копія
  ShopMediaSite— що з цих файлів уже зберіг у себе сайт магазину (звіт сайту)
"""
from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models

from inventory.models import Product

DEFAULT_PRICE_BREAKS = [
    {"min_qty": 10, "discount": 5},
    {"min_qty": 25, "discount": 8},
    {"min_qty": 100, "discount": 12},
    {"min_qty": 250, "discount": 15},
    {"min_qty": 500, "discount": 18},
    {"min_qty": 1000, "discount": 22},
]


class ShopSettings(models.Model):
    """Налаштування цін — синглтон (pk=1)."""

    class Rounding(models.TextChoices):
        NONE   = "none", "Без округлення"
        CENT   = "0.01", "До 0,01"
        FIVE   = "0.05", "До 0,05"
        TEN    = "0.10", "До 0,10"
        HALF   = "0.50", "До 0,50"
        WHOLE  = "1.00", "До 1,00"
        NINETY = "x.90", "Закінчення ,90"
        NINE9  = "x.99", "Закінчення ,99"

    price_breaks = models.JSONField(
        "Шаблон ступенів цін", default=list, blank=True,
        help_text='Знижка від базової ціни (1 шт.) за кількістю, напр. '
                  '[{"min_qty": 10, "discount": 5}, {"min_qty": 100, "discount": 12}]',
    )
    default_markup = models.DecimalField(
        "Націнка за замовчуванням, %", max_digits=7, decimal_places=2, default=Decimal("100"),
        help_text="Для дії «Ціна = закупівля + націнка». 100 % = закупівельна ціна × 2.",
    )
    rounding = models.CharField("Округлення цін", max_length=8, choices=Rounding.choices,
                                default=Rounding.CENT)
    fx_rates = models.JSONField(
        "Курси валют", default=dict, blank=True,
        help_text='Для базових цін в іншій валюті: скільки одиниць валюти магазину за 1 одиницю, '
                  'напр. {"USD": 0.86} — 1 USD = 0,86 EUR.',
    )

    class Meta:
        verbose_name = "Налаштування цін магазину"
        verbose_name_plural = "⚙️ Налаштування цін"

    def __str__(self):
        return "Налаштування цін магазину"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        obj, created = cls.objects.get_or_create(pk=1)
        if created and not obj.price_breaks:
            obj.price_breaks = DEFAULT_PRICE_BREAKS
            obj.save(update_fields=["price_breaks"])
        return obj


class Shop(models.Model):
    name = models.CharField("Назва", max_length=100)
    public_name = models.CharField(
        "Назва для покупців", max_length=100, blank=True, default="",
        help_text="Як магазин називається в листах покупцям і на сторінці оплати Stripe, напр. «Sevskiy GmbH». "
                  "Порожньо — «Ім'я відправника», інакше назва вище.",
    )
    slug = models.CharField(
        "Код (джерело замовлень)", max_length=32, unique=True,
        validators=[RegexValidator(r"^[a-z0-9][a-z0-9_-]*$", "Лише латиниця в нижньому регістрі, цифри, - і _")],
        help_text="Записується в замовлення як «Джерело» (напр. webshop) — за ним фільтруються продажі.",
    )
    currency = models.CharField("Валюта", max_length=3, default="EUR")
    is_active = models.BooleanField("Активний", default=True)
    is_default = models.BooleanField(
        "За замовчуванням", default=False,
        help_text="Для ключів API, не прив'язаних до магазину.",
    )
    url = models.URLField("Адреса сайту", blank=True, default="")
    notes = models.TextField("Нотатки", blank=True, default="")
    free_shipping_enabled = models.BooleanField(
        "Безкоштовна доставка", default=False,
        help_text="Доставка безкоштовна, коли сума товарів (нетто) досягає порогу. Діє в регіонах, де це дозволено.",
    )
    free_shipping_threshold = models.DecimalField(
        "Безкоштовно від (нетто)", max_digits=12, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )
    # ── Особистий кабінет покупців (shop/accounts.py) ──
    accounts_enabled = models.BooleanField(
        "Особистий кабінет покупців", default=False,
        help_text="Реєстрація, вхід, статус замовлень і кошик у кабінеті на сайті. "
                  "Ключу сайту потрібні права «Акаунти покупців — читання / запис».",
    )
    mail_sender = models.ForeignKey(
        "email_assistant.EmailAccount", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Відправник листів покупцям",
        help_text="Підтвердження реєстрації, скидання пароля. Порожньо — SMTP сповіщень Minerva "
                  "(Налаштування → Сповіщення). Копія в «Надісланих» не зберігається: у листах одноразові посилання.",
    )
    mail_from_name = models.CharField("Ім'я відправника", max_length=80, blank=True, default="",
                                      help_text="Напр. «Sevskiy Shop». Порожньо — назва магазину.")
    mail_footer = models.TextField("Підпис листів", blank=True, default="",
                                   help_text="Внизу кожного листа: компанія, адреса, контакти.")
    team_lang = models.CharField("Мова сповіщень команді", max_length=2, default="de",
                                 choices=[("de", "Deutsch"), ("en", "English")],
                                 help_text="«Оплату отримано» тощо — email і Telegram з «Налаштування → Сповіщення».")
    account_links = models.JSONField(
        "Сторінки кабінету на сайті", default=dict, blank=True,
        help_text="Заповнюється автоматично з запитів сайту: {мова: адреса}. Потрібні для листів з адмінки.",
    )
    account_link_error = models.CharField(
        "Відхилена адреса кабінету", max_length=400, blank=True, default="",
        help_text="Остання адреса сторінки кабінету від сайту, яку Minerva не прийняла (інший домен, ніж «Адреса сайту»).",
    )
    # ── Способи оплати на сайті ──
    payment_methods = models.JSONField(
        "Способи оплати на сайті", default=list, blank=True,
        help_text="Що бачить покупець у касі, у цьому порядку (перший — вибраний за замовчуванням): "
                  "prepayment, invoice, stripe. Порожньо — як у налаштуваннях сайту (shop-config.php).",
    )
    # ── Онлайн-оплата Stripe (shop/payments.py) ──
    stripe_enabled = models.BooleanField(
        "Stripe у касі", default=False,
        help_text="Заповнюється автоматично: «stripe» у способах оплати.",
    )
    stripe_secret_key = models.CharField(
        "Stripe: секретний ключ", max_length=255, blank=True, default="",
        help_text="sk_test_… (тестовий режим) або sk_live_… / rk_live_… (обмежений ключ: права Checkout Sessions — запис, "
                  "Charges/PaymentIntents — читання). Зберігається лише в Minerva.",
    )
    stripe_webhook_secret = models.CharField(
        "Stripe: секрет вебхука", max_length=255, blank=True, default="",
        help_text="whsec_… — Stripe → Developers → Webhooks → ваш endpoint → Signing secret.",
    )
    stripe_poll_minutes = models.PositiveSmallIntegerField(
        "Автоперевірка оплат, хв", default=15,
        help_text="Поки вебхук Stripe не працює, Minerva раз на стільки хвилин сама питає Stripe про незавершені "
                  "оплати (за 14 днів). 0 — вимкнено. Щойно приходять вебхуки, автоперевірка зупиняється — "
                  "лишаються кнопки «🔄 Перевірити оплати».",
    )
    stripe_poll_last_at = models.DateTimeField("Остання автоперевірка", null=True, blank=True)
    stripe_webhook_last_at = models.DateTimeField("Останній вебхук Stripe", null=True, blank=True)
    # ── Помічник Minerva на сайті (чат RAG; shop/assistant.py, сайт читає GET /api/v1/shop/assistant/) ──
    ASSISTANT_SITE, ASSISTANT_ON, ASSISTANT_OFF = "site", "on", "off"
    assistant_mode = models.CharField(
        "Помічник на сайті", max_length=4, default=ASSISTANT_SITE,
        choices=[(ASSISTANT_SITE, "Як налаштовано на сайті (private/minerva-config.php)"),
                 (ASSISTANT_ON, "Увімкнено — кнопка «Minerva» на сайті"),
                 (ASSISTANT_OFF, "Вимкнено — кнопки на сайті немає")],
        help_text="Сайт перевіряє налаштування раз на хвилину.",
    )
    ASSISTANT_BRIDGE, ASSISTANT_RAG_API = "bridge", "rag_api"
    assistant_backend = models.CharField(
        "Звідки відповіді", max_length=8, default=ASSISTANT_BRIDGE,
        choices=[(ASSISTANT_RAG_API, "RAG API — як «Помічник по продукції» Minerva (адреса й ключ з «⚙️ Налаштування "
                                     "помічника»; працює з будь-якого хостингу)"),
                 (ASSISTANT_BRIDGE, "Веб-міст RAG (адреса й ключ нижче або з private/minerva-config.php; "
                                    "лише з локальної мережі)")],
        help_text="RAG API — той самий публічний сервіс і ключ, що в Minerva; розмови зберігає сайт.",
    )
    assistant_url = models.CharField(
        "Адреса помічника (RAG)", max_length=255, blank=True, default="",
        help_text="Звідки сервер сайту бере відповіді чату: https://… або http:// лише до IP локальної мережі "
                  "(напр. http://192.168.2.68:8790). Порожньо — адреса з private/minerva-config.php сайту.",
    )
    assistant_key = models.CharField(
        "Ключ помічника", max_length=255, blank=True, default="",
        help_text="Ключ сайту для RAG (заголовок X-Minerva-Bridge). Порожньо — ключ з private/minerva-config.php сайту.",
    )
    assistant_sites = models.JSONField(
        "Звіти сайтів про помічника", default=dict, blank=True,
        help_text="{домен сайту: {at, file, file_enabled, file_url, file_key}} — сайт повідомляє, що в його "
                  "private/minerva-config.php (без ключа), разом із запитом налаштувань.",
    )
    assistant_remove_file = models.BooleanField(
        "Видалити private/minerva-config.php на сайтах", default=False,
        help_text="Сайт видаляє файл при наступному запиті налаштувань; знімається, коли сайти повідомили, що файлу немає.",
    )
    # ── Копія магазину (shop/copying.py) ──
    copied_from = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="copies",
        verbose_name="Скопійовано з",
    )
    review_fields = models.JSONField(
        "Змінити після копіювання", default=dict, blank=True,
        help_text="{поле: значення в момент копіювання}; поле підсвічене червоним, доки його не змінили.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Магазин"
        verbose_name_plural = "🏬 Магазини"
        ordering = ["-is_default", "name"]

    def __str__(self):
        return self.name

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.free_shipping_enabled and self.free_shipping_threshold is None:
            raise ValidationError({"free_shipping_threshold": "Вкажіть поріг безкоштовної доставки."})

    PAYMENT_CHOICES = [("prepayment", "Передоплата (Vorkasse, переказ)"), ("invoice", "Рахунок (Kauf auf Rechnung)"),
                       ("stripe", "Онлайн через Stripe (картки, Apple / Google Pay, SEPA …)")]

    def save(self, *args, **kwargs):
        if self.payment_methods:  # «Stripe у касі» = Stripe серед способів оплати (одне місце для налаштування)
            self.stripe_enabled = "stripe" in self.payment_methods
        super().save(*args, **kwargs)
        if self.is_default:
            Shop.objects.exclude(pk=self.pk).filter(is_default=True).update(is_default=False)
        # Код магазину — джерело замовлень: має бути в довіднику джерел (фільтри й назви в «Продажах»)
        from sales.models import SalesSource
        SalesSource.objects.get_or_create(slug=self.slug, defaults={"name": self.name})


class ShopListing(models.Model):
    """Товар у конкретному магазині."""
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="listings", verbose_name="Магазин")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="shop_listings", verbose_name="Товар")
    is_visible = models.BooleanField("Показувати", default=True)
    price = models.DecimalField(
        "Ціна (нетто, 1 шт.)", max_digits=18, decimal_places=4, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Порожньо — «Ціна продажу» товару; без ціни на сайті «Ціна за запитом».",
    )
    PRICE_MANUAL, PRICE_BASE = "manual", "base"
    price_source = models.CharField(
        "Джерело ціни", max_length=10, default=PRICE_MANUAL,
        choices=[(PRICE_MANUAL, "Вручну"), (PRICE_BASE, "Базові ціни товару (автоматично)")],
        help_text="Базові ціни — ціна і ступені беруться з базових цін товару (склад) і оновлюються разом з ними.",
    )
    price_factor = models.DecimalField(
        "% від базової ціни", max_digits=7, decimal_places=2, default=Decimal("100"),
        validators=[MinValueValidator(Decimal("1"))],
        help_text="100 = як базова ціна, 95 = на 5 % дешевше.",
    )
    discount_percent = models.DecimalField(
        "Акція: знижка, %", max_digits=5, decimal_places=2, default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("90"))],
        help_text="Sonderangebot: знижка на ціну і всі ступені. 0 — без акції.",
    )
    discount_until = models.DateField(
        "Акція до (включно)", null=True, blank=True,
        help_text="Після цієї дати акція сама припиняється. Порожньо — без кінцевої дати.",
    )
    new_until = models.DateField(
        "Новинка до (включно)", null=True, blank=True,
        help_text="До цієї дати на сайті бейдж «Neu», товар показується вгорі каталогу.",
    )
    lifecycle_status = models.CharField(
        "Життєвий цикл у цьому магазині", max_length=16, blank=True, default="",
        choices=[("", "Як на складі"), ("active", "Активний"),
                 ("nrnd", "Не рекомендовано для нових розробок (NRND)"),
                 ("discontinued", "Знято з виробництва (EOL)")],
        help_text="«Як на складі» — статус товару (діє в усіх магазинах). Інше значення — лише для цього магазину.",
    )
    successor = models.ForeignKey(
        Product, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        verbose_name="Заміна в цьому магазині",
        help_text="Порожньо — рекомендована заміна з картки товару.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Позиція магазину"
        verbose_name_plural = "💶 Асортимент і ціни"
        ordering = ["shop", "product__sku"]
        constraints = [models.UniqueConstraint(fields=["shop", "product"], name="uniq_shop_listing")]

    def __str__(self):
        return f"{self.shop.slug}: {self.product.sku}"

    @property
    def effective_price(self):
        return self.price if self.price is not None else self.product.sale_price

    @property
    def effective_lifecycle(self) -> str:
        """Статус для магазину: власний (якщо задано) або статус товару на складі."""
        return self.lifecycle_status or self.product.lifecycle_status

    @property
    def effective_successor(self):
        return self.successor if self.successor_id else self.product.successor


class ShopPriceTier(models.Model):
    """Ціна за штуку від певної кількості (1 шт. = ціна позиції)."""
    listing    = models.ForeignKey(ShopListing, on_delete=models.CASCADE, related_name="tiers",
                                   verbose_name="Позиція")
    min_qty    = models.PositiveIntegerField("Від кількості, шт.", validators=[MinValueValidator(2)])
    unit_price = models.DecimalField("Ціна за шт. (нетто)", max_digits=18, decimal_places=4,
                                     validators=[MinValueValidator(Decimal("0"))])

    class Meta:
        verbose_name = "Ступінь ціни"
        verbose_name_plural = "Ступені цін"
        ordering = ["listing", "min_qty"]
        constraints = [models.UniqueConstraint(fields=["listing", "min_qty"], name="uniq_listing_tier_qty")]

    def __str__(self):
        return f"{self.listing}: від {self.min_qty} шт. — {self.unit_price}"


# Країни ЄС (ISO 3166-1 alpha-2) — розгортання токена «EU» у регіонах доставки
EU_COUNTRIES = ["AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU", "IE",
                "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK"]
REST_OF_WORLD = "*"


class ShippingZone(models.Model):
    """Регіон доставки магазину: країни, ціна доставки (нетто), участь у безкоштовній доставці."""
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="shipping_zones", verbose_name="Магазин")
    name = models.CharField("Назва зони", max_length=80, help_text="Напр. «Europa (Standard)» — видно покупцям у касі.")
    countries = models.JSONField(
        "Країни", default=list,
        help_text="Коди ISO через кому: DE, AT, CH. «EU» — усі країни ЄС, «*» — решта світу.",
    )
    price = models.DecimalField("Доставка (нетто)", max_digits=10, decimal_places=2, default=Decimal("0"),
                                validators=[MinValueValidator(Decimal("0"))])
    free_shipping = models.BooleanField("Діє безкоштовна доставка", default=True,
                                        help_text="Якщо в магазині увімкнено безкоштовну доставку від порогу.")
    delivery_days_min = models.PositiveSmallIntegerField(
        "Доставка від, роб. днів", null=True, blank=True, validators=[MaxValueValidator(365)],
        help_text="Термін доставки після відправлення (робочі дні) — покупець бачить його в касі.")
    delivery_days_max = models.PositiveSmallIntegerField(
        "Доставка до, роб. днів", null=True, blank=True, validators=[MaxValueValidator(365)])
    overrides = models.JSONField(
        "Винятки по країнах", default=dict, blank=True,
        help_text='Своя ціна / термін для окремих країн зони: {"CH": {"price": "25.00", "days_min": 4, "days_max": 7}}.')
    is_active = models.BooleanField("Активний", default=True)
    sort_order = models.PositiveSmallIntegerField("Порядок", default=0)
    dk_code = models.CharField("Код DigiKey", max_length=40, blank=True, default="",
                               help_text="Заповнюється при імпорті регіонів з DigiKey.")

    class Meta:
        verbose_name = "Зона доставки"
        verbose_name_plural = "🚚 Зони доставки"
        ordering = ["shop", "sort_order", "name"]

    def __str__(self):
        return f"{self.shop.slug}: {self.name}"

    @property
    def is_rest_of_world(self) -> bool:
        return REST_OF_WORLD in (self.countries or [])

    def clean(self):
        # Дублікати країн між регіонами перевіряє формсет в адмінці (ShippingZoneFormSet) —
        # так можна перенести країну з одного регіону в інший одним збереженням.
        from django.core.exceptions import ValidationError
        if not self.countries:
            raise ValidationError({"countries": "Вкажіть хоча б одну країну."})


class ShopProduct(Product):
    """Проксі товару: каталог для додавання товарів у магазини."""

    class Meta:
        proxy = True
        verbose_name = "Товар каталогу"
        verbose_name_plural = "📦 Каталог → додати в магазин"


# ── Особистий кабінет покупців ───────────────────────────────────────────────
#
# Акаунти живуть лише в Minerva; сайт зберігає тільки випадковий ключ сесії в cookie (HttpOnly).
# Логіка — shop/accounts.py, API — api/accounts.py (/api/v1/shop/account/…).

class ShopCustomer(models.Model):
    """Акаунт покупця на сайті магазину. Пароль — лише хеш (як у користувачів Django)."""
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="customers", verbose_name="Магазин")
    email = models.EmailField("Email (логін)", max_length=254)
    password = models.CharField("Пароль (хеш)", max_length=128)
    is_active = models.BooleanField("Активний", default=True,
                                    help_text="Вимкнений акаунт не може увійти; замовлення залишаються.")
    email_verified_at = models.DateTimeField("Email підтверджено", null=True, blank=True)
    lang = models.CharField("Мова листів", max_length=2, default="de", choices=[("de", "Deutsch"), ("en", "English")])

    company = models.CharField("Компанія", max_length=160, blank=True, default="")
    name = models.CharField("Контактна особа", max_length=120, blank=True, default="")
    phone = models.CharField("Телефон", max_length=60, blank=True, default="")
    vat_id = models.CharField("VAT ID", max_length=30, blank=True, default="")
    street = models.CharField("Вулиця, будинок", max_length=200, blank=True, default="")
    zip = models.CharField("Індекс", max_length=20, blank=True, default="")
    city = models.CharField("Місто", max_length=100, blank=True, default="")
    country = models.CharField("Країна (ISO 2)", max_length=2, blank=True, default="")
    ship_separate = models.BooleanField("Інша адреса доставки", default=False)
    ship_company = models.CharField("Доставка: компанія", max_length=160, blank=True, default="")
    ship_name = models.CharField("Доставка: отримувач", max_length=120, blank=True, default="")
    ship_street = models.CharField("Доставка: вулиця", max_length=200, blank=True, default="")
    ship_zip = models.CharField("Доставка: індекс", max_length=20, blank=True, default="")
    ship_city = models.CharField("Доставка: місто", max_length=100, blank=True, default="")
    ship_country = models.CharField("Доставка: країна", max_length=2, blank=True, default="")
    ship_phone = models.CharField("Доставка: телефон", max_length=60, blank=True, default="")

    crm_customer = models.ForeignKey("crm.Customer", on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="shop_accounts", verbose_name="Клієнт CRM")
    cart = models.JSONField("Кошик", default=dict, blank=True, help_text="{артикул: кількість}")
    cart_updated_at = models.DateTimeField("Кошик змінено", null=True, blank=True)

    failed_logins = models.PositiveSmallIntegerField("Невдалих входів поспіль", default=0)
    locked_until = models.DateTimeField("Вхід заблоковано до", null=True, blank=True)
    last_login_at = models.DateTimeField("Останній вхід", null=True, blank=True)
    last_mail_at = models.DateTimeField("Останній лист", null=True, blank=True)
    last_mail_error = models.CharField("Помилка останнього листа", max_length=255, blank=True, default="")
    created_at = models.DateTimeField("Зареєстровано", auto_now_add=True)
    updated_at = models.DateTimeField("Змінено", auto_now=True)

    class Meta:
        verbose_name = "Акаунт покупця"
        verbose_name_plural = "👤 Акаунти покупців"
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["shop", "email"], name="shop_customer_unique_email")]

    def __str__(self):
        return f"{self.email} ({self.company or self.name or self.shop.slug})"

    @property
    def is_verified(self) -> bool:
        return self.email_verified_at is not None


class ShopCustomerToken(models.Model):
    """Одноразове посилання з листа: підтвердження email або скидання пароля. Зберігається лише хеш."""
    VERIFY, RESET = "verify", "reset"
    customer = models.ForeignKey(ShopCustomer, on_delete=models.CASCADE, related_name="tokens")
    kind = models.CharField(max_length=10, choices=[(VERIFY, "Підтвердження email"), (RESET, "Скидання пароля")])
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Посилання з листа"
        verbose_name_plural = "Посилання з листів"
        indexes = [models.Index(fields=["customer", "kind", "created_at"])]


class ShopCustomerSession(models.Model):
    """Вхід покупця на сайті. Сайт тримає ключ у cookie, Minerva — лише хеш."""
    customer = models.ForeignKey(ShopCustomer, on_delete=models.CASCADE, related_name="sessions")
    token_hash = models.CharField(max_length=64, unique=True)
    remember = models.BooleanField("Запам'ятати", default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField()
    expires_at = models.DateTimeField()

    class Meta:
        verbose_name = "Сесія покупця"
        verbose_name_plural = "Сесії покупців"


class ShopOrder(models.Model):
    """Замовлення або запит пропозиції, оформлені на сайті: акаунт покупця і дані для кабінету."""
    order = models.OneToOneField("sales.SalesOrder", on_delete=models.CASCADE, related_name="shop_info",
                                 verbose_name="Замовлення")
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="shop_orders", verbose_name="Магазин")
    customer = models.ForeignKey(ShopCustomer, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="orders", verbose_name="Акаунт покупця")
    vat_rate = models.DecimalField("ПДВ, %", max_digits=5, decimal_places=2, null=True, blank=True,
                                   help_text="Ставка, яку порахував сайт (0 — reverse charge / експорт).")
    billing = models.JSONField("Платіжна адреса", default=dict, blank=True)
    requested = models.JSONField("Запитані позиції поза каталогом", default=list, blank=True,
                                 help_text="Запит пропозиції: [{sku, qty}] артикулів, яких немає в Minerva.")
    lang = models.CharField("Мова", max_length=2, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Замовлення з сайту"
        verbose_name_plural = "Замовлення з сайту"

    def __str__(self):
        return str(self.order)


class ShopPayment(models.Model):
    """Онлайн-оплата замовлення через Stripe Checkout (shop/payments.py). Одне замовлення — кілька спроб."""
    OPEN, PROCESSING, PAID, FAILED, EXPIRED, REFUNDED = "open", "processing", "paid", "failed", "expired", "refunded"
    STATUSES = [(OPEN, "Очікує оплати"), (PROCESSING, "Обробляється (SEPA тощо)"), (PAID, "Оплачено"),
                (FAILED, "Помилка оплати"), (EXPIRED, "Сесія закінчилась"), (REFUNDED, "Повернено")]

    order = models.ForeignKey("sales.SalesOrder", on_delete=models.CASCADE, related_name="shop_payments",
                              verbose_name="Замовлення")
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="payments", verbose_name="Магазин")
    provider = models.CharField("Платіжна система", max_length=20, default="stripe")
    session_id = models.CharField("Checkout Session", max_length=255, unique=True)
    payment_intent = models.CharField("PaymentIntent", max_length=255, blank=True, default="", db_index=True)
    url = models.URLField("Сторінка оплати", max_length=2000, blank=True, default="")
    amount = models.DecimalField("Сума", max_digits=12, decimal_places=2)
    currency = models.CharField("Валюта", max_length=3, default="EUR")
    status = models.CharField("Статус", max_length=12, choices=STATUSES, default=OPEN, db_index=True)
    livemode = models.BooleanField("Бойовий режим", default=False)
    last_event = models.CharField("Остання подія Stripe", max_length=80, blank=True, default="")
    expires_at = models.DateTimeField("Сесія дійсна до", null=True, blank=True)
    paid_at = models.DateTimeField("Оплачено", null=True, blank=True)
    notified_at = models.DateTimeField("Лист «оплату отримано»", null=True, blank=True,
                                       help_text="Покупцю надіслано підтвердження оплати (один раз).")
    created_at = models.DateTimeField("Створено", auto_now_add=True)
    updated_at = models.DateTimeField("Змінено", auto_now=True)

    class Meta:
        verbose_name = "Онлайн-оплата"
        verbose_name_plural = "💳 Онлайн-оплати"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.order} · {self.amount} {self.currency} · {self.get_status_display()}"


class ShopMailTemplate(models.Model):
    """Власний текст листа магазину (shop/mail.py). Немає активного шаблону — стандартний текст Minerva."""
    KINDS = [
        ("verify", "Підтвердження реєстрації"),
        ("exists", "Реєстрація на вже наявний email"),
        ("reset", "Скидання пароля"),
        ("changed", "Пароль змінено"),
        ("invite", "Запрошення (акаунт створив менеджер)"),
        ("deleted", "Акаунт видалено"),
        ("paid", "Оплату отримано — покупцю"),
        ("team_paid", "Оплату отримано — команді (email / Telegram)"),
        ("test", "Тестовий лист"),
    ]
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="mail_templates", verbose_name="Магазин")
    kind = models.CharField("Лист", max_length=20, choices=KINDS)
    lang = models.CharField("Мова", max_length=2, choices=[("de", "Deutsch"), ("en", "English")])
    subject = models.CharField("Тема", max_length=200)
    greeting = models.CharField("Звертання", max_length=120, blank=True, default="",
                                help_text="Напр. «Guten Tag {name},». Без імені покупця частина « {name}» прибирається.")
    intro = models.TextField("Текст")
    button = models.CharField("Кнопка", max_length=60, blank=True, default="",
                              help_text="Підпис кнопки з посиланням (якщо лист його має).")
    outro = models.TextField("Завершення", blank=True, default="")
    is_active = models.BooleanField("Використовувати", default=True,
                                    help_text="Вимкнено — знову стандартний текст Minerva.")
    updated_at = models.DateTimeField("Змінено", auto_now=True)
    updated_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                                   verbose_name="Хто змінив")

    class Meta:
        verbose_name = "Шаблон листа"
        verbose_name_plural = "✉️ Шаблони листів"
        ordering = ["shop", "kind", "lang"]
        constraints = [models.UniqueConstraint(fields=["shop", "kind", "lang"], name="shop_mail_template_unique")]

    def __str__(self):
        return f"{self.get_kind_display()} · {self.lang.upper()}"


class ShopMediaSettings(models.Model):
    """Фото й даташити з Minerva для сайтів — синглтон (pk=1), логіка — shop/media.py.
    Змінюється лише кнопками сторінки «🖼 Фото й даташити» (.update), тож фонова індексація і форма
    не затирають одна одну."""
    enabled = models.BooleanField(
        "Віддавати сайту фото й даташити з Minerva", default=False,
        help_text="Вимкнено — сайт показує лише власні файли; перевірити (проіндексувати) можна й до ввімкнення.")
    recheck_days = models.PositiveSmallIntegerField(
        "Повторно перевіряти посилання, днів", default=7, validators=[MaxValueValidator(365)],
        help_text="Вміст за посиланням може змінитися без зміни адреси. 0 — перевіряти лише нові й змінені.")
    run = models.JSONField("Стан індексації", default=dict, blank=True)
    auto_at = models.DateTimeField("Останній автоматичний прохід", null=True, blank=True)

    class Meta:
        verbose_name = "Налаштування фото й даташитів"
        verbose_name_plural = "Налаштування фото й даташитів"

    def __str__(self):
        return "Фото й даташити для сайту"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)


class ProductMedia(models.Model):
    """Фото або даташит товару для сайту. Порядок джерел: посилання (URL) → файл у картці товару Minerva;
    нічого придатного — сайт показує власний файл. Перевірена копія лежить у MEDIA_ROOT/shop_media/<sha256>.<ext>."""
    IMAGE, DATASHEET = "image", "datasheet"
    KINDS = [(IMAGE, "🖼 Фото"), (DATASHEET, "📄 Даташит")]
    PENDING, OK, FALLBACK, NONE, ERROR = "pending", "ok", "fallback", "none", "error"
    STATUSES = [
        (PENDING, "⏳ Чекає перевірки"),
        (OK, "✅ Готово"),
        (FALLBACK, "⚠ URL недоступний → файл Minerva"),
        (NONE, "— У Minerva немає"),
        (ERROR, "❌ Недоступно"),
    ]
    SOURCES = [("url", "Посилання (URL)"), ("file", "Файл Minerva")]

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="shop_media", verbose_name="Товар")
    kind = models.CharField("Тип", max_length=10, choices=KINDS)
    status = models.CharField("Стан", max_length=10, choices=STATUSES, default=PENDING, db_index=True)
    source = models.CharField("Звідки взято", max_length=4, choices=SOURCES, blank=True, default="")
    stale = models.BooleanField(
        "Остання придатна копія", default=False,
        help_text="Джерело зараз недоступне, але не змінювалось — сайт отримує копію з попередньої перевірки.")
    inputs = models.CharField("Перевірені джерела", max_length=700, blank=True, default="",
                              help_text="Посилання й ім'я файлу на момент перевірки; зміна в картці → перевірити знову.")
    url_error = models.CharField("Помилка посилання", max_length=300, blank=True, default="")
    file_error = models.CharField("Помилка файлу", max_length=300, blank=True, default="")
    sha256 = models.CharField("SHA-256", max_length=64, blank=True, default="", db_index=True)
    size = models.PositiveIntegerField("Розмір, байт", null=True, blank=True)
    content_type = models.CharField("Тип вмісту", max_length=60, blank=True, default="")
    stored = models.CharField("Копія", max_length=200, blank=True, default="",
                              help_text="Шлях у MEDIA_ROOT (shop_media/<sha256>.<ext>).")
    checked_at = models.DateTimeField("Перевірено", null=True, blank=True)
    changed_at = models.DateTimeField("Вміст змінився", null=True, blank=True)

    class Meta:
        verbose_name = "Фото / даташит для сайту"
        verbose_name_plural = "🖼 Фото й даташити"
        ordering = ["product__sku", "kind"]
        constraints = [models.UniqueConstraint(fields=["product", "kind"], name="shop_product_media_unique")]

    def __str__(self):
        return f"{self.product.sku} · {self.get_kind_display()}"

    @property
    def usable(self) -> bool:
        """Є перевірена копія — сайт її отримує."""
        return bool(self.sha256 and self.stored)


class ShopMediaSite(models.Model):
    """Що з фото й даташитів уже є на сайті магазину — звіт сайту (POST /api/v1/shop/media/status/)."""
    shop = models.OneToOneField(Shop, on_delete=models.CASCADE, related_name="media_site", verbose_name="Магазин")
    reported_at = models.DateTimeField("Звіт сайту", null=True, blank=True)
    have = models.JSONField("Збережено на сайті (SHA-256)", default=list, blank=True)
    failed = models.JSONField("Помилки завантаження {SHA-256: текст}", default=dict, blank=True)
    sync_url = models.URLField("Адреса синхронізації сайту", blank=True, default="",
                               help_text="Сайт повідомляє її сам; лише на домені «Адреса сайту» магазину.")
    ping_at = models.DateTimeField("Minerva викликала синхронізацію", null=True, blank=True)
    ping_error = models.CharField("Помилка виклику", max_length=255, blank=True, default="")

    class Meta:
        verbose_name = "Фото й даташити на сайті"
        verbose_name_plural = "Фото й даташити на сайті"

    def __str__(self):
        return f"{self.shop} · фото й даташити"


# ── Адмінка сайту магазину (/admin/ на сайті; shop/site_admin.py) ─────────────────────────────────────────────

class ShopStaff(models.Model):
    """Хто з команди може входити в адмінку сайту магазину і з якою роллю (вхід — логіном і паролем Minerva)."""
    ROLE_ADMIN, ROLE_MANAGER, ROLE_VIEWER = "admin", "manager", "viewer"
    ROLES = [(ROLE_ADMIN, "Адміністратор сайту — усі налаштування, ключі й паролі"),
             (ROLE_MANAGER, "Менеджер магазину — налаштування без ключів і паролів"),
             (ROLE_VIEWER, "Перегляд — лише бачить налаштування")]
    # Що дозволено на сайті (сайт перевіряє за цим списком)
    PERMISSIONS = {ROLE_ADMIN: ["settings.view", "settings.edit", "secrets.edit", "pair"],
                   ROLE_MANAGER: ["settings.view", "settings.edit"],
                   ROLE_VIEWER: ["settings.view"]}
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="staff", verbose_name="Магазин")
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="shop_roles",
                             verbose_name="Користувач Minerva")
    role = models.CharField("Роль на сайті", max_length=10, choices=ROLES, default=ROLE_MANAGER)
    is_active = models.BooleanField("Доступ дозволено", default=True)
    last_site_login = models.DateTimeField("Останній вхід на сайт", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Доступ до адмінки сайту"
        verbose_name_plural = "👥 Доступ до адмінки сайту"
        constraints = [models.UniqueConstraint(fields=["shop", "user"], name="shop_staff_unique")]

    def __str__(self):
        return f"{self.user} · {self.get_role_display()}"

    @property
    def permissions(self) -> list[str]:
        return list(self.PERMISSIONS.get(self.role, []))


class ShopPairing(models.Model):
    """Одноразовий код підключення сайту: сайт обмінює його на ключ API й секрет вебхука (без перенесення файлів)."""
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="pairings", verbose_name="Магазин")
    code_hash = models.CharField(max_length=64, unique=True)
    created_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField("Дійсний до")
    used_at = models.DateTimeField("Використано", null=True, blank=True)
    site_url = models.URLField("Сайт", blank=True, default="")
    api_key = models.ForeignKey("api.APIKey", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                                verbose_name="Виданий ключ API")

    class Meta:
        verbose_name = "Код підключення сайту"
        verbose_name_plural = "Коди підключення сайту"


class ShopSiteEvent(models.Model):
    """Журнал адмінки сайту: вхід, зміни налаштувань (лише назви полів, без значень), підключення."""
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="site_events", verbose_name="Магазин")
    at = models.DateTimeField("Коли", auto_now_add=True)
    site = models.CharField("Сайт", max_length=120, blank=True, default="")
    user = models.CharField("Хто", max_length=150, blank=True, default="")
    action = models.CharField("Дія", max_length=40)
    details = models.JSONField("Подробиці", default=dict, blank=True)

    class Meta:
        verbose_name = "Подія адмінки сайту"
        verbose_name_plural = "Журнал адмінки сайту"
        ordering = ["-at"]

    def __str__(self):
        return f"{self.at:%d.%m.%Y %H:%M} · {self.site} · {self.user} · {self.action}"
