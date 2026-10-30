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
    notes       = models.TextField("Примітки", blank=True, default="")
    is_favorite = models.BooleanField("⭐ Обраний", default=False, db_index=True)
    created_at  = models.DateTimeField("Створено", auto_now_add=True)
    updated_at  = models.DateTimeField("Оновлено", auto_now=True)

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
    # Лише продажі (crm/utils.py: sales.revenue.sale_orders — без запитів пропозицій, скасованих і тестових),
    # суми — у валюті Minerva. Сторінка списку рахує їх одним махом (crm_utils.prefetch_stats → _order_stats).

    def order_stats(self):
        """OrderStats клієнта (crm/utils.py), кешується на об'єкті."""
        cached = getattr(self, "_order_stats", None)
        if cached is None:
            from crm import utils as crm_utils
            cached = crm_utils.order_stats([self]).get(self.pk)
            if cached is None:
                from sales.revenue import home_currency
                cached = crm_utils.OrderStats(home_currency())
            self._order_stats = cached
        return cached

    def total_orders(self) -> int:
        return self.order_stats().count

    def total_revenue(self) -> Decimal:
        """Сума продажів у валюті Minerva (валюти без курсу не додано)."""
        return self.order_stats().revenue.total

    def avg_order_value(self) -> Decimal:
        return self.order_stats().avg

    def _sale_lines(self):
        from sales.models import SalesOrderLine
        from sales.revenue import sale_lines
        return sale_lines(SalesOrderLine.objects.filter(order__customer_key=self.external_key))

    def top_products(self, limit: int = 5) -> list:
        """Топ N товарів за виручкою (у валюті Minerva): [{sku_raw, total_qty, total_revenue}, ...]"""
        from django.db.models import Sum
        from sales.revenue import line_revenue
        lines = self._sale_lines()
        money = line_revenue(lines, by=["sku_raw"])
        rows = list(lines.order_by().values("sku_raw").annotate(total_qty=Sum("qty")))
        for r in rows:
            m = money.get((r["sku_raw"],))
            r["total_revenue"] = m.total if m else Decimal("0")
        rows.sort(key=lambda r: -r["total_revenue"])
        return rows[:limit]

    def card_stats(self) -> dict:
        """Дані для картки CRM: count, rev (сума у валюті Minerva + без курсу), avg, last, days, recency_cls, sym."""
        from sales.revenue import fmt, symbol

        stats = self.order_stats()
        count = stats.count
        last = stats.last
        days = None
        if last:
            ld = last.date() if hasattr(last, "date") and callable(last.date) else last
            days = (timezone.localdate() - ld).days

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

        money = stats.revenue
        return {
            "count":       count,
            "rev":         money.full_text if money else "—",
            "avg":         fmt(stats.avg, money.home) if stats.avg else "—",
            "last":        last,
            "days":        days,
            "recency_cls": recency_cls,
            "sym":         symbol(money.home),   # символ валюти для топ-товарів
        }

    def active_strategy_card(self) -> dict | None:
        """Compact strategy data for the CRM card. 1 DB query."""
        try:
            from strategy.models import CustomerStrategy
            strategy = (
                CustomerStrategy.objects
                .filter(customer=self, status__in=["active", "paused"])
                .select_related("template", "current_step")
                .prefetch_related("steps")
                .order_by("-started_at")
                .first()
            )
            if not strategy:
                return None
            steps = sorted(strategy.steps.all(), key=lambda s: s.pk)
            total = len(steps)
            done  = sum(1 for s in steps if s.outcome != "pending")
            pct   = int(done / total * 100) if total else 0
            return {
                "pk":         strategy.pk,
                "name":       (strategy.name or strategy.template.name)[:40],
                "status":     strategy.status,
                "current_pk": strategy.current_step_id,
                "steps":      steps[:8],
                "total":      total,
                "done":       done,
                "pct":        pct,
                "extra":      max(0, total - 8),
            }
        except Exception:
            return None

    def last_order_date(self):
        return self.order_stats().last

    def first_order_date(self):
        from sales.revenue import sale_orders
        result = sale_orders().filter(customer_key=self.external_key).aggregate(first=Min("order_date"))
        return result["first"]

    def days_since_last_order(self) -> int | None:
        last = self.last_order_date()
        if not last:
            return None
        if hasattr(last, "date") and callable(last.date):
            last = last.date()
        return (timezone.localdate() - last).days

    def is_repeat_customer(self) -> bool:
        from crm import utils as crm_utils
        return crm_utils.is_repeat(self.total_orders())

    def rfm_score(self) -> dict:
        """RFM аналіз — логіка лише в crm/utils.py."""
        from crm import utils as crm_utils
        return crm_utils.rfm_score(self)

    def _rfm_segment(self, r, f, m):
        from crm import utils as crm_utils
        return crm_utils.rfm_segment(r, f, m)


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


class CustomerTimeline(models.Model):
    """
    Єдина хронологічна стрічка подій по клієнту.
    Агрегує: замовлення, email листи, AI аналізи, нотатки, нагадування.
    """

    EVENT_TYPES = [
        ('order',       '🛒 Замовлення'),
        ('email_in',    '📨 Отримано лист'),
        ('email_out',   '📤 Відправлено лист'),
        ('ai_analysis', '🏛️ AI аналіз'),
        ('note',        '📝 Нотатка'),
        ('reminder',    '🔔 Нагадування'),
        ('call',        '📞 Дзвінок'),
        ('meeting',     '🤝 Зустріч'),
    ]

    customer   = models.ForeignKey(
                   'crm.Customer',
                   on_delete=models.CASCADE,
                   related_name='timeline_events',
                   verbose_name='Клієнт')
    user       = models.ForeignKey(
                   'auth.User',
                   null=True, blank=True,
                   on_delete=models.SET_NULL,
                   verbose_name='Автор')
    event_type = models.CharField(
                   max_length=20,
                   choices=EVENT_TYPES,
                   default='note',
                   verbose_name='Тип події')
    title      = models.CharField(max_length=300, verbose_name='Заголовок')
    body       = models.TextField(blank=True, verbose_name='Деталі')
    ai_summary = models.TextField(
                   blank=True,
                   verbose_name='AI резюме',
                   help_text='Короткий висновок Мінерви')

    related_order_id = models.PositiveIntegerField(
                         null=True, blank=True,
                         verbose_name='ID замовлення')
    related_email_id = models.PositiveIntegerField(
                         null=True, blank=True,
                         verbose_name='ID email')
    email_message    = models.ForeignKey(
                         'email_assistant.EmailMessage',
                         null=True, blank=True,
                         on_delete=models.SET_NULL,
                         related_name='crm_events',
                         verbose_name='Лист у пошті')
    message_id       = models.CharField(
                         max_length=500, blank=True, default='', db_index=True,
                         verbose_name='Message-ID листа',
                         help_text='Один лист (копія у Вхідних і в Надісланих) — одна подія')

    remind_at   = models.DateTimeField(
                    null=True, blank=True,
                    verbose_name='Нагадати о',
                    help_text='Якщо заповнено — Мінерва надішле алерт')
    remind_sent = models.BooleanField(default=False)

    is_pinned  = models.BooleanField(default=False, verbose_name='Закріплено вгорі')
    created_at = models.DateTimeField(
        default=None, null=True, blank=True,
        help_text='Залиш порожнім — заповниться автоматично (поточний час).',
    )

    class Meta:
        verbose_name        = 'Подія клієнта'
        verbose_name_plural = 'Хронологія клієнта'
        ordering            = ['-created_at']
        indexes             = [
            models.Index(fields=['customer', '-created_at']),
            models.Index(fields=['remind_at', 'remind_sent']),
        ]

    def save(self, *args, **kwargs):
        if self.created_at is None:
            from django.utils import timezone
            self.created_at = timezone.now()
        super().save(*args, **kwargs)

    def __str__(self):
        ts = self.created_at.strftime('%d.%m.%Y') if self.created_at else '?'
        return f'{self.customer} | {self.get_event_type_display()} | {ts}'
