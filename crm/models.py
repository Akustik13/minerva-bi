from __future__ import annotations
from decimal import Decimal
from django.db import models
from django.utils import timezone
from django.db.models import Sum, Max, Min
import hashlib


class Customer(models.Model):
    """Клієнт — центральна сутність CRM."""

    class Segment(models.TextChoices):
        B2B         = "b2b",         "B2B (компанія)"
        B2C         = "b2c",         "B2C"
        DISTRIBUTOR = "distributor", "Дистриб'ютор"
        RESELLER    = "reseller",    "Реселер"
        OTHER       = "other",       "Інше"

    class Status(models.TextChoices):
        ACTIVE   = "active",   "Активний"
        INACTIVE = "inactive", "Неактивний"
        VIP      = "vip",      "VIP"
        BLOCKED  = "blocked",  "Заблокований"

    # --- УНІКАЛЬНИЙ КЛЮЧ для надійного зв'язку ---
    external_key = models.CharField(
        "Ключ клієнта", 
        max_length=64, 
        unique=True, 
        db_index=True,
        help_text="Унікальний ідентифікатор для зв'язку з замовленнями"
    )
    
    # --- Контактні дані ---
    name    = models.CharField("Контактна особа", max_length=255)
    email   = models.EmailField("Email", blank=True, default="", db_index=True)
    phone   = models.CharField("Телефон", max_length=50, blank=True, default="")
    company = models.CharField("Компанія", max_length=255, blank=True, default="")
    country = models.CharField("Країна (ISO 2)", max_length=2, blank=True, default="",
                               db_index=True, help_text="Двобуквений код: DE, UA, PL, US...")

    # --- Структурована адреса ---
    addr_street  = models.CharField("Вулиця, будинок", max_length=300, blank=True, default="")
    addr_city    = models.CharField("Місто",           max_length=100, blank=True, default="")
    addr_zip     = models.CharField("Поштовий індекс", max_length=20,  blank=True, default="")
    addr_state   = models.CharField("Штат / провінція (ISO 2)", max_length=2, blank=True, default="",
                                    help_text="Тільки для США/Канади: CA, NY, TX, FL...")

    # --- Legacy адреса (зберігається для сумісності) ---
    shipping_address = models.TextField("Адреса (raw, legacy)", blank=True, default="")

    # --- Сегментація ---
    segment = models.CharField("Сегмент", max_length=20,
                               choices=Segment.choices, default=Segment.B2C)
    status  = models.CharField("Статус", max_length=20,
                               choices=Status.choices, default=Status.ACTIVE)

    # --- Метадані ---
    source     = models.CharField("Джерело", max_length=64, blank=True, default="",
                                  help_text="digikey / webshop / manual / etc")
    notes      = models.TextField("Примітки", blank=True, default="")
    created_at = models.DateTimeField("Створено", auto_now_add=True)
    updated_at = models.DateTimeField("Оновлено", auto_now=True)

    class Meta:
        verbose_name = "Клієнт"
        verbose_name_plural = "Клієнти"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        label = self.company or self.name
        return f"{label} ({self.external_key[:8]})"
    
    @staticmethod
    def generate_key(email: str, name: str) -> str:
        """Генерує унікальний ключ з email + name."""
        source = f"{email.lower().strip()}:{name.lower().strip()}"
        return hashlib.sha256(source.encode()).hexdigest()[:32]

    def save(self, *args, **kwargs):
        """Автоматично генерує external_key якщо немає."""
        if not self.external_key:
            self.external_key = self.generate_key(self.email or self.name, self.name)
        super().save(*args, **kwargs)

    # ---- Аналітичні властивості ----

    def total_orders(self) -> int:
        from sales.models import SalesOrder
        return SalesOrder.objects.filter(customer_key=self.external_key).count()

    def total_revenue(self) -> Decimal:
        """Сума всіх продажів (з SalesOrderLine)."""
        from sales.models import SalesOrderLine
        result = (
            SalesOrderLine.objects
            .filter(order__customer_key=self.external_key)
            .aggregate(rev=Sum("total_price"))
        )
        return result["rev"] or Decimal("0")

    def avg_order_value(self) -> Decimal:
        total = self.total_orders()
        if total == 0:
            return Decimal("0")
        return (self.total_revenue() / total).quantize(Decimal("0.01"))

    def top_products(self, limit: int = 5) -> list:
        """Топ N товарів за виручкою: [{sku_raw, total_qty, total_revenue}, ...]"""
        from sales.models import SalesOrderLine
        from django.db.models import Sum
        return list(
            SalesOrderLine.objects
            .filter(order__customer_key=self.external_key)
            .values("sku_raw")
            .annotate(total_qty=Sum("qty"), total_revenue=Sum("total_price"))
            .order_by("-total_revenue")[:limit]
        )

    def card_stats(self) -> dict:
        """Всі дані для картки CRM — 2 запити замість 5+.
        Повертає: count, rev (рядок з валютою), avg, last, days, recency_cls
        """
        from sales.models import SalesOrder, SalesOrderLine
        from django.db.models import Sum, Max, Count
        from django.utils import timezone

        SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "CHF": "Fr"}

        # Query 1: кількість замовлень + дата останнього
        agg = (
            SalesOrder.objects
            .filter(customer_key=self.external_key)
            .aggregate(count=Count("id"), last=Max("order_date"))
        )
        count = agg["count"] or 0
        last  = agg["last"]

        days = None
        if last:
            ld   = last.date() if hasattr(last, "date") else last
            days = (timezone.now().date() - ld).days

        # Recency CSS class: green/amber/orange/red
        if days is None:
            recency_cls = "crm-rec-none"
        elif days <= 30:
            recency_cls = "crm-rec-fresh"
        elif days <= 90:
            recency_cls = "crm-rec-ok"
        elif days <= 365:
            recency_cls = "crm-rec-warn"
        else:
            recency_cls = "crm-rec-old"

        # Query 2: виручка по валютах
        rev_rows = list(
            SalesOrderLine.objects
            .filter(order__customer_key=self.external_key)
            .values("currency")
            .annotate(total=Sum("total_price"))
            .order_by("-total")
        )

        def _fmt(total, currency):
            sym = SYMBOLS.get(currency or "EUR", (currency or "") + "\u00a0")
            return f"{sym}{float(total or 0):,.0f}"

        if rev_rows:
            main       = rev_rows[0]
            main_total = float(main["total"] or 0)
            main_curr  = main["currency"] or "EUR"
            main_sym   = SYMBOLS.get(main_curr, main_curr + "\u00a0")
            rev_str    = " + ".join(_fmt(p["total"], p["currency"]) for p in rev_rows[:2])
            avg_str    = _fmt(main_total / count, main_curr) if count else "—"
        else:
            main_sym = "€"
            rev_str = avg_str = "—"

        return {
            "count":       count,
            "rev":         rev_str,
            "avg":         avg_str,
            "last":        last,
            "days":        days,
            "recency_cls": recency_cls,
            "sym":         main_sym,   # символ валюти для топ-товарів
        }

    def last_order_date(self):
        from sales.models import SalesOrder
        result = SalesOrder.objects.filter(
            customer_key=self.external_key
        ).aggregate(last=Max("order_date"))
        return result["last"]

    def first_order_date(self):
        from sales.models import SalesOrder
        result = SalesOrder.objects.filter(
            customer_key=self.external_key
        ).aggregate(first=Min("order_date"))
        return result["first"]

    def days_since_last_order(self) -> int | None:
        last = self.last_order_date()
        if not last:
            return None
        if hasattr(last, "date"):
            last = last.date()
        return (timezone.now().date() - last).days

    def is_repeat_customer(self) -> bool:
        return self.total_orders() > 1

    def rfm_score(self) -> dict:
        """RFM аналіз."""
        days = self.days_since_last_order()
        
        if days is None:
            r = 1
        elif days <= 30:
            r = 5
        elif days <= 60:
            r = 4
        elif days <= 120:
            r = 3
        elif days <= 365:
            r = 2
        else:
            r = 1

        freq = self.total_orders()
        if freq >= 10:
            f = 5
        elif freq >= 5:
            f = 4
        elif freq >= 3:
            f = 3
        elif freq >= 2:
            f = 2
        else:
            f = 1

        rev = float(self.total_revenue())
        if rev >= 5000:
            m = 5
        elif rev >= 1000:
            m = 4
        elif rev >= 500:
            m = 3
        elif rev >= 100:
            m = 2
        else:
            m = 1

        score = r + f + m
        
        return {
            "R": r, "F": f, "M": m, "score": score,
            "segment": self._rfm_segment(r, f, m)
        }

    def _rfm_segment(self, r, f, m):
        if r >= 4 and f >= 4 and m >= 4:
            return "🏆 Champions"
        elif r >= 3 and f >= 3:
            return "💎 Loyal"
        elif r >= 4:
            return "⭐ Potential"
        elif f >= 4:
            return "🔄 Regular"
        elif r <= 2 and f >= 2:
            return "😴 At Risk"
        elif r <= 2 and f <= 2:
            return "💤 Hibernating"
        else:
            return "🆕 New"


class CustomerNote(models.Model):
    """Нотатка / взаємодія з клієнтом."""

    class NoteType(models.TextChoices):
        CALL     = "call",     "Дзвінок"
        EMAIL    = "email",    "Email"
        MEETING  = "meeting",  "Зустріч"
        NOTE     = "note",     "Нотатка"
        REMINDER = "reminder", "⏰ Нагадування"
        OTHER    = "other",    "Інше"

    customer   = models.ForeignKey(Customer, on_delete=models.CASCADE,
                                   related_name="notes_crm", verbose_name="Клієнт")
    note_type  = models.CharField("Тип", max_length=20,
                                  choices=NoteType.choices, default=NoteType.NOTE)
    subject    = models.CharField("Тема", max_length=255)
    body       = models.TextField("Текст", blank=True, default="")
    due_date   = models.DateField("Дедлайн нагадування", null=True, blank=True,
                                  help_text="Заповни для типу ⏰ Нагадування → авто-Task")
    created_at = models.DateTimeField("Дата", auto_now_add=True)
    created_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL,
                                   null=True, blank=True, verbose_name="Автор")

    class Meta:
        verbose_name = "Нотатка"
        verbose_name_plural = "Нотатки"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.customer.name} — {self.subject}"
