"""
Інтернет-магазини: кілька магазинів (сайт, маркетплейс …), у кожного свій асортимент і ціни.

  Shop         — магазин; slug = джерело замовлень (SalesOrder.source); ключ API прив'язується до магазину
  ShopListing  — товар у конкретному магазині: видимість + ціна (порожньо = «Ціна продажу» товару)
  ShopPriceTier— ступінь ціни позиції за кількістю (як на DigiKey: від 10 / 100 … шт.)
  ShopSettings — шаблон ступенів, націнка, округлення (спільні для всіх магазинів)
  ShopCustomer — акаунт покупця (особистий кабінет на сайті) + токени листів, сесії
  ShopOrder    — замовлення з сайту: акаунт покупця, ставка ПДВ, платіжна адреса
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
    account_links = models.JSONField(
        "Сторінки кабінету на сайті", default=dict, blank=True,
        help_text="Заповнюється автоматично з запитів сайту: {мова: адреса}. Потрібні для листів з адмінки.",
    )
    account_link_error = models.CharField(
        "Відхилена адреса кабінету", max_length=400, blank=True, default="",
        help_text="Остання адреса сторінки кабінету від сайту, яку Minerva не прийняла (інший домен, ніж «Адреса сайту»).",
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

    def save(self, *args, **kwargs):
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
    name = models.CharField("Регіон", max_length=80)
    countries = models.JSONField(
        "Країни", default=list,
        help_text="Коди ISO через кому: DE, AT, CH. «EU» — усі країни ЄС, «*» — решта світу.",
    )
    price = models.DecimalField("Доставка (нетто)", max_digits=10, decimal_places=2, default=Decimal("0"),
                                validators=[MinValueValidator(Decimal("0"))])
    free_shipping = models.BooleanField("Діє безкоштовна доставка", default=True,
                                        help_text="Якщо в магазині увімкнено безкоштовну доставку від порогу.")
    is_active = models.BooleanField("Активний", default=True)
    sort_order = models.PositiveSmallIntegerField("Порядок", default=0)
    dk_code = models.CharField("Код DigiKey", max_length=40, blank=True, default="",
                               help_text="Заповнюється при імпорті регіонів з DigiKey.")

    class Meta:
        verbose_name = "Регіон доставки"
        verbose_name_plural = "🚚 Регіони доставки"
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
