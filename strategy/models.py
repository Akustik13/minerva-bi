from __future__ import annotations
from decimal import Decimal
from django.db import models
from django.utils import timezone


class AISettings(models.Model):
    """
    Singleton — глобальні налаштування AI асистента.
    Керується через /admin/strategy/aisettings/1/change/
    """

    # ── API ──────────────────────────────────────────────────
    anthropic_api_key = models.CharField(
        max_length=200, blank=True,
        verbose_name='Anthropic API Key',
        help_text='sk-ant-... з console.anthropic.com')
    telegram_bot_token = models.CharField(
        max_length=100, blank=True,
        verbose_name='Telegram Bot Token',
        help_text='Отримати у @BotFather')

    # ── Бюджет ───────────────────────────────────────────────
    monthly_budget_usd = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal('10.00'),
        verbose_name='Місячний бюджет ($)')
    alert_threshold_usd = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal('2.00'),
        verbose_name='Поріг сповіщення ($)',
        help_text='Надіслати алерт коли витрачено більше цієї суми')
    budget_alert_telegram_id = models.CharField(
        max_length=50, blank=True,
        verbose_name='Telegram ID для бюджетних алертів',
        help_text='Твій особистий Telegram ID (дізнатись через /myid в боті)')
    per_user_daily_limit_usd = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal('0.50'),
        verbose_name='Денний ліміт на юзера ($)')

    # ── Персонаж ─────────────────────────────────────────────
    persona_name = models.CharField(
        max_length=50, default='Minerva',
        verbose_name="Ім'я персонажа")
    persona_base_prompt = models.TextField(
        verbose_name='Базовий промпт персонажа',
        default=(
            'Ти — Мінерва, богиня мудрості, що живе в серці системи Minerva ERP.\n'
            'Ти говориш від імені жінки — мудрої, трохи величної, але з почуттям гумору.\n'
            'Ти НІКОЛИ не кажеш що ти Claude або AI від Anthropic.\n'
            'Ти НІКОЛИ не вигадуєш дані — завжди використовуєш інструменти.'
        ))

    # ── Глобальні дозволи ────────────────────────────────────
    ai_allow_email_sending = models.BooleanField(
        default=True,
        verbose_name='Дозволити AI відправляти email')
    ai_allow_order_creation = models.BooleanField(
        default=False,
        verbose_name='Дозволити AI створювати замовлення')
    ai_allow_inventory_edit = models.BooleanField(
        default=False,
        verbose_name='Дозволити AI редагувати склад')
    ai_allow_financial_data = models.BooleanField(
        default=True,
        verbose_name='Дозволити AI бачити фінансові дані')

    class Meta:
        verbose_name = 'Налаштування AI'
        verbose_name_plural = 'Налаштування AI'

    def __str__(self):
        return 'Налаштування AI'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def from_email(self):
        try:
            from config.models import SystemSettings
            return SystemSettings.get().from_email
        except Exception:
            return None


class StrategyTemplate(models.Model):
    """Шаблон стратегії — незмінний план дій."""

    class BehaviorType(models.TextChoices):
        REACTIVATION = "reactivation", "Реактивація (At Risk / Lost)"
        NURTURING    = "nurturing",    "Нарощування (Promising)"
        RETENTION    = "retention",    "Утримання VIP (Champion)"
        ONBOARDING   = "onboarding",   "Онбординг (нові клієнти)"

    name          = models.CharField("Назва", max_length=200)
    description   = models.TextField("Опис", blank=True, default="")
    behavior_type = models.CharField(
        "Тип поведінки", max_length=20,
        choices=BehaviorType.choices, default=BehaviorType.NURTURING,
    )
    is_active  = models.BooleanField("Активний", default=True)
    created_at = models.DateTimeField("Створено", auto_now_add=True)

    class Meta:
        verbose_name = "Шаблон стратегії"
        verbose_name_plural = "Шаблони стратегій"
        ordering = ["behavior_type", "name"]

    def __str__(self) -> str:
        return self.name


class TemplateStep(models.Model):
    """Крок шаблону стратегії."""

    class StepType(models.TextChoices):
        EMAIL    = "email",    "📧 Email"
        CALL     = "call",     "📞 Дзвінок"
        PAUSE    = "pause",    "⏸ Пауза"
        DECISION = "decision", "🔀 Рішення"

    template    = models.ForeignKey(
        StrategyTemplate, on_delete=models.CASCADE,
        related_name="steps", verbose_name="Шаблон",
    )
    step_type   = models.CharField("Тип кроку", max_length=20,
                                   choices=StepType.choices, default=StepType.EMAIL)
    title       = models.CharField("Заголовок", max_length=200)
    description = models.TextField("Деталі / скрипт", blank=True, default="")
    delay_days  = models.PositiveSmallIntegerField(
        "Затримка (днів)", default=0,
        help_text="0 = виконати одразу після попереднього кроку",
    )
    order = models.PositiveSmallIntegerField("Порядок", default=0)

    # Decision branches — self-FK
    next_step_yes = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="branch_yes_from", verbose_name="Наступний (Так / позитив)",
    )
    next_step_no = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="branch_no_from", verbose_name="Наступний (Ні / негатив)",
    )

    # Phase 1 canvas coordinates
    canvas_x = models.FloatField("Canvas X", default=0.0)
    canvas_y = models.FloatField("Canvas Y", default=0.0)

    class Meta:
        verbose_name = "Крок шаблону"
        verbose_name_plural = "Кроки шаблону"
        ordering = ["template", "order"]

    def __str__(self) -> str:
        return f"{self.template.name} — {self.order}. {self.title}"


class CustomerStrategy(models.Model):
    """Активна стратегія, призначена конкретному клієнту."""

    class Status(models.TextChoices):
        ACTIVE = "active", "⚡ Активна"
        PAUSED = "paused", "⏸ Призупинена"
        DONE   = "done",   "✅ Завершена"
        FAILED = "failed", "❌ Провалена"

    customer = models.ForeignKey(
        "crm.Customer", on_delete=models.CASCADE,
        related_name="strategies", verbose_name="Клієнт",
    )
    template = models.ForeignKey(
        StrategyTemplate, on_delete=models.PROTECT,
        related_name="customer_strategies", verbose_name="Шаблон",
    )
    name   = models.CharField("Назва стратегії", max_length=200, blank=True, default="")
    status = models.CharField(
        "Статус", max_length=10,
        choices=Status.choices, default=Status.ACTIVE,
    )
    current_step   = models.ForeignKey(
        "CustomerStep", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+", verbose_name="Поточний крок",
    )
    started_at     = models.DateTimeField("Розпочато", default=timezone.now)
    next_action_at = models.DateTimeField("Наступна дія", null=True, blank=True)
    notes          = models.TextField("Нотатки", blank=True, default="")

    class Meta:
        verbose_name = "Стратегія клієнта"
        verbose_name_plural = "Стратегії клієнтів"
        ordering = ["-started_at"]

    def __str__(self) -> str:
        label = self.name or str(self.template)
        return f"{self.customer} — {label}"


class CustomerStep(models.Model):
    """Конкретний крок у стратегії клієнта."""

    class Outcome(models.TextChoices):
        PENDING     = "pending",     "⏳ Очікує"
        DONE_POS    = "done_pos",    "✅ Виконано (+)"
        DONE_NEG    = "done_neg",    "⚠️ Виконано (−)"
        SKIPPED     = "skipped",     "⏭ Пропущено"
        NO_RESPONSE = "no_response", "🔇 Немає відповіді"

    strategy      = models.ForeignKey(
        CustomerStrategy, on_delete=models.CASCADE,
        related_name="steps", verbose_name="Стратегія",
    )
    template_step = models.ForeignKey(
        TemplateStep, null=True, blank=True, on_delete=models.SET_NULL,
        verbose_name="Крок шаблону",
    )
    step_type   = models.CharField("Тип кроку", max_length=20,
                                   choices=TemplateStep.StepType.choices,
                                   default=TemplateStep.StepType.EMAIL)
    title       = models.CharField("Заголовок", max_length=200)
    description = models.TextField("Деталі", blank=True, default="")

    scheduled_at  = models.DateTimeField("Заплановано", null=True, blank=True)
    completed_at  = models.DateTimeField("Виконано", null=True, blank=True)
    outcome       = models.CharField(
        "Результат", max_length=20,
        choices=Outcome.choices, default=Outcome.PENDING,
    )
    outcome_notes = models.TextField("Примітки до результату", blank=True, default="")

    class Meta:
        verbose_name = "Крок клієнта"
        verbose_name_plural = "Кроки клієнта"
        ordering = ["strategy", "scheduled_at"]

    def __str__(self) -> str:
        return f"{self.strategy} — {self.title} ({self.get_outcome_display()})"


class StepLog(models.Model):
    """Детальний лог кожної взаємодії по кроку."""

    step      = models.ForeignKey(
        CustomerStep, on_delete=models.CASCADE,
        related_name="logs", verbose_name="Крок",
    )
    logged_by = models.ForeignKey(
        "auth.User", null=True, blank=True, on_delete=models.SET_NULL,
        verbose_name="Менеджер",
    )
    outcome       = models.CharField(
        "Результат", max_length=20,
        choices=CustomerStep.Outcome.choices, default=CustomerStep.Outcome.DONE_POS,
    )
    notes         = models.TextField("Примітки", blank=True, default="")
    logged_at     = models.DateTimeField("Час запису", default=timezone.now)
    ai_suggestion = models.TextField(
        "AI-підказка", blank=True, default="",
        help_text="Заповнюється автоматично у Фазі 2 (AI-радник)",
    )

    class Meta:
        verbose_name = "Лог кроку"
        verbose_name_plural = "Лог кроків"
        ordering = ["-logged_at"]

    def __str__(self) -> str:
        return f"Лог #{self.pk} — {self.step}"
