"""Проєкти: кілька гілок листування, листи, замовлення й учасники навколо однієї справи клієнта.

Одна гілка ≠ один проєкт: проєкт збирає кілька гілок, а гілка чи лист можуть належати кільком проєктам.
Автоматичні зв'язки зберігають підставу (reason) і статус підтвердження; людина може їх виправити.
"""
from django.conf import settings
from django.db import IntegrityError, models, transaction
from django.db.models import Q
from django.utils import timezone


class Project(models.Model):
    LEAD, ACTIVE, ON_HOLD, DONE, CANCELLED = "lead", "active", "on_hold", "done", "cancelled"
    STATUS_CHOICES = [
        (LEAD, "🟡 Запит / потенційний"),
        (ACTIVE, "🟢 Активний"),
        (ON_HOLD, "⏸ Призупинено"),
        (DONE, "✅ Завершено"),
        (CANCELLED, "✖ Скасовано"),
    ]
    OPEN_STATUSES = (LEAD, ACTIVE, ON_HOLD)

    code = models.CharField("Код", max_length=20, unique=True, editable=False,
                            help_text="P-РРРР-NNN; якщо згадати код у темі листа, лист сам прив'яжеться до проєкту")
    name = models.CharField("Назва", max_length=200)
    icon = models.CharField("Значок", max_length=8, blank=True, default="",
                            help_text="Емодзі в пошті замість 📁 (правий клік на проєкті → «🎨 Значок…»)")
    description = models.TextField("Опис / вимоги", blank=True, default="")
    customer = models.ForeignKey("crm.Customer", null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="projects", verbose_name="Клієнт")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="owned_projects", verbose_name="Відповідальний")
    status = models.CharField("Статус", max_length=10, choices=STATUS_CHOICES, default=ACTIVE, db_index=True)
    due_date = models.DateField("Строк", null=True, blank=True)
    next_action = models.CharField("Наступна дія", max_length=255, blank=True, default="")
    source_thread = models.ForeignKey("email_assistant.EmailThread", null=True, blank=True,
                                      on_delete=models.SET_NULL, related_name="+",
                                      verbose_name="Створено з листування")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+", verbose_name="Створив")
    created_at = models.DateTimeField("Створено", auto_now_add=True)
    updated_at = models.DateTimeField("Оновлено", auto_now=True)

    class Meta:
        verbose_name = "Проєкт"
        verbose_name_plural = "📁 Проєкти"
        ordering = ["-updated_at"]

    def __str__(self):
        return f"{self.code} · {self.name}"

    @property
    def is_open(self) -> bool:
        return self.status in self.OPEN_STATUSES

    def save(self, *args, **kwargs):
        if self.code:
            return super().save(*args, **kwargs)
        # Код P-РРРР-NNN: наступний номер року; якщо два збереження збіглися — повтор з новим номером
        for _ in range(5):
            self.code = self._next_code()
            try:
                with transaction.atomic():
                    return super().save(*args, **kwargs)
            except IntegrityError:
                self.code = ""
        raise IntegrityError("Не вдалося видати унікальний код проєкту")

    @classmethod
    def _next_code(cls) -> str:
        prefix = f"P-{timezone.localdate().year}-"
        numbers = [int(c[len(prefix):]) for c in cls.objects.filter(code__startswith=prefix)
                   .values_list("code", flat=True) if c[len(prefix):].isdigit()]
        return f"{prefix}{(max(numbers) if numbers else 0) + 1:03d}"


class ProjectParticipant(models.Model):
    CUSTOMER, COLLEAGUE, SUPPLIER, OTHER = "customer", "colleague", "supplier", "other"
    ROLE_CHOICES = [(CUSTOMER, "👤 Клієнт"), (COLLEAGUE, "🏢 Колега"),
                    (SUPPLIER, "🏭 Постачальник"), (OTHER, "❓ Інше")]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="participants")
    email = models.CharField("Email", max_length=254)
    name = models.CharField("Ім'я", max_length=200, blank=True, default="")
    role = models.CharField("Роль", max_length=10, choices=ROLE_CHOICES, default=OTHER)
    customer = models.ForeignKey("crm.Customer", null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="+", verbose_name="Клієнт CRM")

    class Meta:
        verbose_name = "Учасник"
        verbose_name_plural = "Учасники"
        constraints = [models.UniqueConstraint(fields=["project", "email"], name="unique_project_participant")]
        ordering = ["role", "email"]

    def __str__(self):
        return f"{self.name or self.email} ({self.get_role_display()})"

    def save(self, *args, **kwargs):
        self.email = (self.email or "").strip().lower()
        super().save(*args, **kwargs)


class ProjectLink(models.Model):
    """Прив'язка до проєкту гілки, окремого листа або замовлення — з підставою й статусом."""
    MANUAL, RULE, AUTO = "manual", "rule", "auto"
    SOURCE_CHOICES = [(MANUAL, "вручну"), (RULE, "правило"), (AUTO, "пропозиція системи")]
    SUGGESTED, CONFIRMED, REJECTED = "suggested", "confirmed", "rejected"
    STATUS_CHOICES = [(SUGGESTED, "❔ Запропоновано"), (CONFIRMED, "✅ Підтверджено"), (REJECTED, "✖ Відхилено")]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="links")
    thread = models.ForeignKey("email_assistant.EmailThread", null=True, blank=True, on_delete=models.CASCADE,
                               related_name="project_links", verbose_name="Гілка листування")
    message = models.ForeignKey("email_assistant.EmailMessage", null=True, blank=True, on_delete=models.CASCADE,
                                related_name="project_links", verbose_name="Окремий лист")
    order = models.ForeignKey("sales.SalesOrder", null=True, blank=True, on_delete=models.CASCADE,
                              related_name="project_links", verbose_name="Замовлення")
    source = models.CharField("Джерело", max_length=10, choices=SOURCE_CHOICES, default=MANUAL)
    status = models.CharField("Статус", max_length=10, choices=STATUS_CHOICES, default=CONFIRMED, db_index=True)
    reason = models.CharField("Підстава", max_length=255, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField("Додано", auto_now_add=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Зв'язок проєкту"
        verbose_name_plural = "Зв'язки проєкту"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["project", "thread"], condition=Q(thread__isnull=False),
                                    name="unique_project_thread"),
            models.UniqueConstraint(fields=["project", "message"], condition=Q(message__isnull=False),
                                    name="unique_project_message"),
            models.UniqueConstraint(fields=["project", "order"], condition=Q(order__isnull=False),
                                    name="unique_project_order"),
            models.CheckConstraint(
                condition=(Q(thread__isnull=False, message__isnull=True, order__isnull=True)
                           | Q(thread__isnull=True, message__isnull=False, order__isnull=True)
                           | Q(thread__isnull=True, message__isnull=True, order__isnull=False)),
                name="project_link_one_target"),
        ]

    def __str__(self):
        return f"{self.project.code} ← {self.target_label}"

    @property
    def target_label(self) -> str:
        if self.thread_id:
            return f"✉️ {self.thread.subject or '(без теми)'}"
        if self.message_id:
            return f"📨 {self.message.subject or '(без теми)'}"
        return f"🛒 {self.order.source}:{self.order.order_number}"


class ProjectOverview(models.Model):
    """Останній AI-огляд проєкту (projects/overview.py): обіцянки, виконано, відкриті питання, наступна дія.
    fingerprint — стан листів, Angebote, замовлень і термінів на момент огляду: інший → огляд застарів."""
    project = models.OneToOneField(Project, on_delete=models.CASCADE, related_name="overview")
    data = models.JSONField(default=dict)
    fingerprint = models.CharField(max_length=64, blank=True, default="")
    messages_count = models.PositiveIntegerField(default=0)
    model = models.CharField("Модель AI", max_length=60, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField("Складено", auto_now=True)

    class Meta:
        verbose_name = "Огляд проєкту"
        verbose_name_plural = "Огляди проєктів"

    def __str__(self):
        return f"Огляд {self.project.code}"


class DismissedThread(models.Model):
    """«Розбір пошти»: гілку переглянули — вона не стосується жодного проєкту."""
    thread = models.OneToOneField("email_assistant.EmailThread", on_delete=models.CASCADE,
                                  related_name="project_dismissal")
    dismissed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="+")
    dismissed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Гілка поза проєктами"
        verbose_name_plural = "Гілки поза проєктами"
