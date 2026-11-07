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
    start_date = models.DateField("Старт", null=True, blank=True,
                                  help_text="Коли почали роботу; порожньо — шкала починається з першої події проєкту")
    due_date = models.DateField("Строк клієнту", null=True, blank=True,
                                help_text="Що обіцяли клієнту: на шкалі — червона лінія; минув — «Потребує уваги»")
    next_action = models.CharField("Наступна дія", max_length=255, blank=True, default="")
    products = models.ManyToManyField("inventory.Product", blank=True, related_name="projects",
                                      verbose_name="Товари проєкту",
                                      help_text="Що розробляємо чи постачаємо: за ними Minerva пропонує закупівлі "
                                                "й JLC-замовлення проєкту")
    source_thread = models.ForeignKey("email_assistant.EmailThread", null=True, blank=True,
                                      on_delete=models.SET_NULL, related_name="+",
                                      verbose_name="Створено з листування")
    baseline_at = models.DateTimeField("Базовий план зафіксовано", null=True, blank=True)
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


LINK_TARGETS = ("thread", "message", "order", "purchase_order", "jlc_order", "shipment")


def _one_target():
    """Рівно одна ціль прив'язки (CheckConstraint)."""
    condition = None
    for target in LINK_TARGETS:
        part = Q(**{f"{t}__isnull": t != target for t in LINK_TARGETS})
        condition = part if condition is None else condition | part
    return condition


class ProjectLink(models.Model):
    """Прив'язка до проєкту гілки, окремого листа, замовлення, закупівлі, JLC-замовлення чи відправлення —
    з підставою й статусом. Що досяжне через уже прив'язане (відправлення замовлення, JLC і посилки закупівлі),
    окремого рядка не потребує (projects/facts.py)."""
    TARGETS = LINK_TARGETS
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
    purchase_order = models.ForeignKey("inventory.PurchaseOrder", null=True, blank=True, on_delete=models.CASCADE,
                                       related_name="project_links", verbose_name="Закупівля (PO)")
    jlc_order = models.ForeignKey("jlcpcb.JLCOrder", null=True, blank=True, on_delete=models.CASCADE,
                                  related_name="project_links", verbose_name="JLC-замовлення")
    shipment = models.ForeignKey("shipping.Shipment", null=True, blank=True, on_delete=models.CASCADE,
                                 related_name="project_links", verbose_name="Відправлення / посилка")
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
            models.UniqueConstraint(fields=["project", "purchase_order"], condition=Q(purchase_order__isnull=False),
                                    name="unique_project_purchase_order"),
            models.UniqueConstraint(fields=["project", "jlc_order"], condition=Q(jlc_order__isnull=False),
                                    name="unique_project_jlc_order"),
            models.UniqueConstraint(fields=["project", "shipment"], condition=Q(shipment__isnull=False),
                                    name="unique_project_shipment"),
            models.CheckConstraint(condition=_one_target(), name="project_link_one_target"),
        ]

    def __str__(self):
        return f"{self.project.code} ← {self.target_label}"

    @property
    def target(self):
        for name in self.TARGETS:
            if getattr(self, f"{name}_id"):
                return getattr(self, name)
        return None

    @property
    def target_label(self) -> str:
        if self.thread_id:
            return f"✉️ {self.thread.subject or '(без теми)'}"
        if self.message_id:
            return f"📨 {self.message.subject or '(без теми)'}"
        if self.order_id:
            return f"🛒 {self.order.source}:{self.order.order_number}"
        if self.purchase_order_id:
            po = self.purchase_order
            return f"🏭 {po}" + (f" · {po.supplier.name}" if po.supplier_id else "")
        if self.jlc_order_id:
            return f"🔧 JLC {self.jlc_order.jlc_order_number or self.jlc_order.jlc_order_id}"
        if self.shipment_id:
            s = self.shipment
            return f"{'📥' if s.direction == s.IN else '🚚'} {s.tracking_number or f'відправлення #{s.pk}'}"
        return "—"


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


class ProjectHighlight(models.Model):
    """📌 Важливий момент проєкту на шкалі: рішення, погодження, зміна вимог, проблема… — з листом і цитатою.
    Ставить людина (з пульта, шкали чи прев'ю листа); source / status — для майбутніх пропозицій AI ✓/✗."""
    DECISION, APPROVAL, CHANGE, PROBLEM = "decision", "approval", "change", "problem"
    DEADLINE, DELIVERY, PAYMENT, OTHER = "deadline", "delivery", "payment", "other"
    KIND_CHOICES = [
        (DECISION, "🧭 Рішення"), (APPROVAL, "✅ Погодження"), (CHANGE, "✏️ Зміна вимог"), (PROBLEM, "⚠️ Проблема"),
        (DEADLINE, "⏰ Строк"), (DELIVERY, "📦 Поставка"), (PAYMENT, "💶 Оплата"), (OTHER, "📌 Інше"),
    ]
    MANUAL, AI, RULE = "manual", "ai", "rule"
    SOURCE_CHOICES = [(MANUAL, "вручну"), (AI, "AI"), (RULE, "правило")]
    STATUS_CHOICES = ProjectLink.STATUS_CHOICES

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="highlights")
    date = models.DateField("Дата", db_index=True)
    kind = models.CharField("Вид", max_length=10, choices=KIND_CHOICES, default=OTHER)
    title = models.CharField("Що сталося", max_length=255)
    quote = models.TextField("Цитата / примітка", blank=True, default="")
    message = models.ForeignKey("email_assistant.EmailMessage", null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="project_highlights", verbose_name="Лист")
    source = models.CharField("Джерело", max_length=10, choices=SOURCE_CHOICES, default=MANUAL)
    status = models.CharField("Статус", max_length=10, choices=STATUS_CHOICES, default=ProjectLink.CONFIRMED,
                              db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+", verbose_name="Додав")
    created_at = models.DateTimeField("Додано", auto_now_add=True)

    class Meta:
        verbose_name = "Важливий момент"
        verbose_name_plural = "📌 Важливі моменти"
        ordering = ["-date", "-pk"]

    def __str__(self):
        return f"{self.project.code} · {self.date:%d.%m.%Y} · {self.title}"


class ProjectState(models.Model):
    """Кеш стану проєкту для списку й фільтра «За терміновістю» (projects/state.py): колір, найважливіше
    з «Потребує уваги», коли пораховано. Картка проєкту рахує стан наживо й оновлює цей запис."""
    project = models.OneToOneField(Project, on_delete=models.CASCADE, related_name="state")
    health = models.CharField("Стан", max_length=8, blank=True, default="", db_index=True)
    high = models.PositiveSmallIntegerField(default=0)
    medium = models.PositiveSmallIntegerField(default=0)
    items = models.JSONField(default=list, blank=True)
    computed_at = models.DateTimeField(null=True, blank=True)
    dirty = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Стан проєкту (кеш)"
        verbose_name_plural = "Стани проєктів (кеш)"

    def __str__(self):
        return f"{self.project.code}: {self.health or '—'}"


# ── План: фази, кроки, залежності, шаблони (projects/plan.py, розрахунок — projects/schedule.py) ─────────────────

class ProjectPhase(models.Model):
    """Фаза плану (stage-gate): кроки фази + умова переходу (gate). Дати фази — з її кроків; без кроків — свої."""
    PLANNED, ACTIVE, DONE = "planned", "active", "done"
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="phases")
    order = models.PositiveIntegerField("Порядок", default=0)
    name = models.CharField("Фаза", max_length=120)
    icon = models.CharField("Значок", max_length=8, blank=True, default="")
    gate = models.CharField("Умова переходу", max_length=200, blank=True, default="",
                            help_text="Що має статися, щоб іти далі: «Клієнт погодив зразок»")
    start = models.DateField("Початок", null=True, blank=True)
    end = models.DateField("Кінець", null=True, blank=True)
    # кеш розрахунку (schedule.persist)
    f_start = models.DateField(null=True, blank=True)
    f_end = models.DateField(null=True, blank=True)
    eff_status = models.CharField(max_length=10, blank=True, default="")

    class Meta:
        verbose_name = "Фаза"
        verbose_name_plural = "Фази"
        ordering = ["project", "order", "pk"]

    def __str__(self):
        return f"{self.project.code} · {self.name}"


TRIGGERS = [
    ("", "— вручну"),
    ("offer_sent", "📝 Angebot надіслано"),
    ("offer_accepted", "🤝 Angebot прийнято"),
    ("order_received", "🛒 Отримано замовлення клієнта"),
    ("order_shipped", "🚚 Замовлення відправлено"),
    ("order_delivered", "✅ Замовлення доставлено"),
    ("po_ordered", "🏭 Закупівлю замовлено"),
    ("po_received", "📥 Закупівлю отримано"),
    ("jlc_ordered", "🔧 JLC-замовлення зроблено"),
    ("jlc_delivered", "📦 JLC-замовлення доставлено"),
    ("shipment_sent", "🚚 Відправлення передано перевізнику"),
    ("shipment_delivered", "✅ Відправлення доставлено"),
]


class ProjectStep(models.Model):
    """Крок плану: задача, віха (◆) — дати, хто, залежності, стан. trigger + прив'язані об'єкти → стан сам
    (Angebot надіслано, закупівлю отримано…). Поля f_* / eff_status / critical / slip_days — кеш розрахунку."""
    TASK, MILESTONE = "task", "milestone"
    KIND_CHOICES = [(TASK, "Крок"), (MILESTONE, "◆ Віха")]
    WE, CUSTOMER, SUPPLIER, COLLEAGUE = "we", "customer", "supplier", "colleague"
    SIDE_CHOICES = [(WE, "Ми"), (CUSTOMER, "Клієнт"), (SUPPLIER, "Постачальник"), (COLLEAGUE, "Колега")]
    TODO, DOING, WAITING, DONE, SKIPPED = "todo", "doing", "waiting", "done", "skipped"
    STATUS_CHOICES = [(TODO, "○ Заплановано"), (DOING, "● В роботі"), (WAITING, "⏳ Чекаємо"),
                      (DONE, "✓ Виконано"), (SKIPPED, "⤼ Пропущено")]
    MANUAL, TEMPLATE, AI = "manual", "template", "ai"
    SOURCE_CHOICES = [(MANUAL, "вручну"), (TEMPLATE, "шаблон"), (AI, "AI")]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="steps")
    phase = models.ForeignKey(ProjectPhase, null=True, blank=True, on_delete=models.SET_NULL, related_name="steps",
                              verbose_name="Фаза")
    order = models.PositiveIntegerField("Порядок", default=0)
    title = models.CharField("Крок", max_length=200)
    kind = models.CharField("Вид", max_length=10, choices=KIND_CHOICES, default=TASK)
    side = models.CharField("Хто", max_length=10, choices=SIDE_CHOICES, default=WE)
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="project_steps", verbose_name="Виконавець")
    start = models.DateField("Початок (план)", null=True, blank=True)
    end = models.DateField("Кінець (план)", null=True, blank=True)
    duration_days = models.PositiveSmallIntegerField("Тривалість, дн.", default=1)
    baseline_start = models.DateField("Базовий початок", null=True, blank=True)
    baseline_end = models.DateField("Базовий кінець", null=True, blank=True)
    status = models.CharField("Стан", max_length=10, choices=STATUS_CHOICES, default=TODO, db_index=True)
    started_on = models.DateField("Почали", null=True, blank=True)
    done_on = models.DateField("Виконано", null=True, blank=True)
    trigger = models.CharField("Виконано, коли", max_length=20, choices=TRIGGERS, blank=True, default="")
    auto = models.BooleanField("Стан з прив'язаних об'єктів", default=True)
    links = models.ManyToManyField(ProjectLink, blank=True, related_name="steps", verbose_name="Прив'язані об'єкти")
    offers = models.ManyToManyField("offers.Offer", blank=True, related_name="project_steps", verbose_name="Angebote")
    notes = models.TextField("Примітки", blank=True, default="")
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default=MANUAL)
    # кеш розрахунку (schedule.persist)
    f_start = models.DateField(null=True, blank=True)
    f_end = models.DateField(null=True, blank=True)
    eff_status = models.CharField(max_length=10, blank=True, default="")
    critical = models.BooleanField(default=False)
    slip_days = models.SmallIntegerField(null=True, blank=True)
    auto_note = models.CharField(max_length=255, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Крок плану"
        verbose_name_plural = "Кроки плану"
        ordering = ["project", "order", "pk"]

    def __str__(self):
        return f"{self.project.code} · {self.title}"


class StepDependency(models.Model):
    """Наступний крок починається після попереднього (фініш → старт) + lag_days. Попередній може бути
    в іншому проєкті (тоді беруться його збережені дати прогнозу)."""
    predecessor = models.ForeignKey(ProjectStep, on_delete=models.CASCADE, related_name="successor_links")
    successor = models.ForeignKey(ProjectStep, on_delete=models.CASCADE, related_name="predecessor_links")
    lag_days = models.SmallIntegerField("Відступ, дн.", default=0)

    class Meta:
        verbose_name = "Залежність кроків"
        verbose_name_plural = "Залежності кроків"
        constraints = [
            models.UniqueConstraint(fields=["predecessor", "successor"], name="unique_step_dependency"),
            models.CheckConstraint(condition=~Q(predecessor=models.F("successor")), name="step_dependency_not_self"),
        ]


class ProjectTemplate(models.Model):
    """Шаблон плану: фази й кроки з тривалостями (plan — JSON, див. projects/plan.py). Вбудовані з'являються
    самі (plan.ensure_builtin), їх можна сховати, не видалити; свій — «💾 Зберегти як шаблон» з плану проєкту."""
    name = models.CharField("Назва", max_length=120, unique=True)
    description = models.TextField("Опис", blank=True, default="")
    plan = models.JSONField("Фази й кроки", default=list)
    is_active = models.BooleanField("Показувати", default=True)
    builtin = models.BooleanField("Вбудований", default=False)
    order = models.PositiveIntegerField("Порядок", default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Шаблон плану"
        verbose_name_plural = "🧩 Шаблони планів"
        ordering = ["order", "name"]

    def __str__(self):
        return self.name
