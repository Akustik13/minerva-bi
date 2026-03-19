from __future__ import annotations
from django.db import models
from django.utils import timezone


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
