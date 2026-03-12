from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.db import models
from django.db.models import Sum, F


# ── CompanySettings (Singleton) ────────────────────────────────────────────────

class CompanySettings(models.Model):
    """Реквізити компанії — єдиний запис (pk=1)."""

    name           = models.CharField("Назва компанії", max_length=255, default="Моя компанія")
    legal_name     = models.CharField("Юридична назва", max_length=255, blank=True, default="")
    addr_street    = models.CharField("Адреса", max_length=300, blank=True, default="")
    addr_city      = models.CharField("Місто", max_length=100, blank=True, default="")
    addr_zip       = models.CharField("Поштовий індекс", max_length=20, blank=True, default="")
    addr_country   = models.CharField("Країна (ISO 2)", max_length=2, blank=True, default="")
    vat_id         = models.CharField("VAT ID / ІПН", max_length=50, blank=True, default="")
    iban           = models.CharField("IBAN", max_length=34, blank=True, default="")
    bank_name      = models.CharField("Банк", max_length=255, blank=True, default="")
    swift          = models.CharField("SWIFT/BIC", max_length=11, blank=True, default="")
    email          = models.EmailField("Email", blank=True, default="")
    phone          = models.CharField("Телефон", max_length=50, blank=True, default="")
    logo           = models.FileField("Логотип (PNG/JPG)", upload_to="accounting/logos/",
                                      null=True, blank=True)
    invoice_prefix = models.CharField("Префікс рахунку", max_length=10, default="INV")
    next_number    = models.PositiveIntegerField("Наступний номер рахунку", default=1)

    class Meta:
        verbose_name        = "Налаштування компанії"
        verbose_name_plural = "Налаштування компанії"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.pk = 1  # Singleton — завжди тільки один запис
        super().save(*args, **kwargs)

    @classmethod
    def get(cls) -> "CompanySettings":
        obj, _ = cls.objects.get_or_create(pk=1, defaults={"name": "Моя компанія"})
        return obj


# ── ExpenseCategory ────────────────────────────────────────────────────────────

class ExpenseCategory(models.Model):
    name   = models.CharField("Назва", max_length=100, unique=True)
    parent = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        verbose_name="Батьківська категорія", related_name="children"
    )

    class Meta:
        verbose_name        = "Категорія витрат"
        verbose_name_plural = "Категорії витрат"
        ordering            = ["name"]

    def __str__(self):
        if self.parent_id:
            return f"{self.parent.name} → {self.name}"
        return self.name


# ── Invoice ────────────────────────────────────────────────────────────────────

class Invoice(models.Model):

    class Status(models.TextChoices):
        DRAFT     = "draft",     "Чернетка"
        SENT      = "sent",      "Надіслано"
        PAID      = "paid",      "Оплачено"
        OVERDUE   = "overdue",   "Прострочено"
        CANCELLED = "cancelled", "Скасовано"

    number       = models.CharField("Номер", max_length=30, unique=True,
                                    blank=True, editable=False)
    customer     = models.ForeignKey(
        "crm.Customer", on_delete=models.PROTECT,
        null=True, blank=True, verbose_name="Клієнт"
    )
    order        = models.ForeignKey(
        "sales.SalesOrder", on_delete=models.SET_NULL,
        null=True, blank=True, verbose_name="Замовлення"
    )
    status       = models.CharField(
        "Статус", max_length=20, choices=Status.choices,
        default=Status.DRAFT, db_index=True
    )
    currency     = models.CharField("Валюта", max_length=3, default="EUR")
    issue_date   = models.DateField("Дата виставлення", default=date.today)
    service_date = models.DateField("Дата послуги (Leistungsdatum)",
                                    null=True, blank=True)
    due_date     = models.DateField("Термін оплати", null=True, blank=True)
    vat_rate     = models.DecimalField(
        "VAT %", max_digits=5, decimal_places=2, default=Decimal("0"),
        help_text="0, 7, 19, 20 — залежно від країни/типу послуги"
    )
    notes        = models.TextField("Примітки", blank=True, default="")

    # Snapshot клієнта — незмінний після виставлення (юридична вимога)
    client_name  = models.CharField("Клієнт (snapshot)", max_length=255, blank=True, default="")
    client_addr  = models.TextField("Адреса (snapshot)", blank=True, default="")
    client_vat   = models.CharField("VAT клієнта", max_length=50, blank=True, default="")

    created_at   = models.DateTimeField("Створено", auto_now_add=True)

    class Meta:
        verbose_name        = "Рахунок-фактура"
        verbose_name_plural = "Рахунки-фактури"
        ordering            = ["-issue_date", "-id"]
        indexes             = [
            models.Index(fields=["status"]),
            models.Index(fields=["issue_date"]),
            models.Index(fields=["due_date"]),
        ]

    def __str__(self):
        return self.number or f"Invoice #{self.pk}"

    def save(self, *args, **kwargs):
        if not self.number:
            cfg = CompanySettings.get()
            self.number = f"{cfg.invoice_prefix}-{date.today().year}-{cfg.next_number:04d}"
            # Atomic increment — уникаємо race condition
            CompanySettings.objects.filter(pk=1).update(
                next_number=F("next_number") + 1
            )
        super().save(*args, **kwargs)

    # ── Обчислювані поля ──────────────────────────────────────────────────────

    @property
    def subtotal(self) -> Decimal:
        return sum(
            (line.line_total for line in self.lines.all()),
            Decimal("0.00")
        )

    @property
    def vat_amount(self) -> Decimal:
        return (self.subtotal * self.vat_rate / 100).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    @property
    def total(self) -> Decimal:
        return self.subtotal + self.vat_amount

    @property
    def paid_amount(self) -> Decimal:
        agg = self.payments.aggregate(s=Sum("amount"))["s"]
        return agg or Decimal("0.00")

    @property
    def balance_due(self) -> Decimal:
        return self.total - self.paid_amount


# ── InvoiceLine ────────────────────────────────────────────────────────────────

class InvoiceLine(models.Model):
    invoice     = models.ForeignKey(Invoice, on_delete=models.CASCADE,
                                    related_name="lines", verbose_name="Рахунок")
    product     = models.ForeignKey(
        "inventory.Product", on_delete=models.SET_NULL,
        null=True, blank=True, verbose_name="Товар"
    )
    description = models.CharField("Опис", max_length=500)
    quantity    = models.DecimalField("Кількість", max_digits=12, decimal_places=3,
                                      default=Decimal("1.000"))
    unit_price  = models.DecimalField("Ціна за одиницю", max_digits=18, decimal_places=4)
    discount    = models.DecimalField("Знижка %", max_digits=5, decimal_places=2,
                                      default=Decimal("0.00"))
    unit        = models.CharField("Од.вим.", max_length=20, blank=True, default="шт")

    class Meta:
        verbose_name        = "Рядок рахунку"
        verbose_name_plural = "Рядки рахунку"

    def __str__(self):
        return f"{self.description[:40]} × {self.quantity}"

    @property
    def line_total(self) -> Decimal:
        gross = self.quantity * self.unit_price
        disc  = gross * self.discount / 100
        return (gross - disc).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ── Payment ────────────────────────────────────────────────────────────────────

class Payment(models.Model):

    class Method(models.TextChoices):
        BANK    = "bank",    "Bank transfer"
        CARD    = "card",    "Card"
        CASH    = "cash",    "Cash"
        STRIPE  = "stripe",  "Stripe"
        PAYPAL  = "paypal",  "PayPal"
        CRYPTO  = "crypto",  "Crypto"

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE,
                                related_name="payments", verbose_name="Рахунок")
    date    = models.DateField("Дата", default=date.today)
    amount  = models.DecimalField("Сума", max_digits=18, decimal_places=2)
    method  = models.CharField("Метод", max_length=20,
                               choices=Method.choices, default=Method.BANK)
    notes   = models.TextField("Примітки", blank=True, default="")

    class Meta:
        verbose_name        = "Платіж"
        verbose_name_plural = "Платежі"
        ordering            = ["-date"]

    def __str__(self):
        return f"{self.invoice.number} — {self.amount} {self.invoice.currency}"


# ── Expense ────────────────────────────────────────────────────────────────────

class Expense(models.Model):
    date              = models.DateField("Дата", default=date.today, db_index=True)
    amount            = models.DecimalField("Сума", max_digits=18, decimal_places=2)
    currency          = models.CharField("Валюта", max_length=3, default="EUR")
    category          = models.ForeignKey(
        ExpenseCategory, on_delete=models.PROTECT,
        null=True, blank=True, verbose_name="Категорія"
    )
    supplier          = models.ForeignKey(
        "inventory.Supplier", on_delete=models.SET_NULL,
        null=True, blank=True, verbose_name="Постачальник"
    )
    description       = models.CharField("Опис", max_length=500)
    receipt           = models.FileField("Чек/документ",
                                         upload_to="accounting/receipts/",
                                         null=True, blank=True)
    is_vat_deductible = models.BooleanField("VAT-deductible", default=False)

    class Meta:
        verbose_name        = "Витрата"
        verbose_name_plural = "Витрати"
        ordering            = ["-date"]

    def __str__(self):
        return f"{self.date} — {self.description[:40]} ({self.amount} {self.currency})"
