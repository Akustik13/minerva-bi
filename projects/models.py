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
    ai_mail_since = models.DateField("Листи для AI — з", null=True, blank=True,
                                     help_text="«🧠 AI-помічник» (моменти й ризики, план з листування, забуті листи) "
                                               "читає листи проєкту лише з цієї дати; порожньо — усі")
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


LINK_TARGETS = ("thread", "message", "order", "purchase_order", "jlc_order", "shipment", "event")


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
    event = models.ForeignKey("calendar_app.CalendarEvent", null=True, blank=True, on_delete=models.CASCADE,
                              related_name="project_links", verbose_name="Подія календаря")
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
            models.UniqueConstraint(fields=["project", "event"], condition=Q(event__isnull=False),
                                    name="unique_project_event"),
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
        if self.event_id:
            return f"📅 {self.event.title}"
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
    Ставить людина (з пульта, шкали чи прев'ю листа); source ai — прийнята пропозиція AI-помічника
    (ProjectSuggestion, projects/ai_scan.py)."""
    DECISION, APPROVAL, CHANGE, PROBLEM = "decision", "approval", "change", "problem"
    DEADLINE, DELIVERY, PAYMENT, OTHER = "deadline", "delivery", "payment", "other"
    CALL, MEETING = "call", "meeting"
    KIND_CHOICES = [
        (DECISION, "🧭 Рішення"), (APPROVAL, "✅ Погодження"), (CHANGE, "✏️ Зміна вимог"), (PROBLEM, "⚠️ Проблема"),
        (DEADLINE, "⏰ Строк"), (DELIVERY, "📦 Поставка"), (PAYMENT, "💶 Оплата"),
        (CALL, "📞 Дзвінок"), (MEETING, "🤝 Зустріч"), (OTHER, "📌 Інше"),
    ]
    MANUAL, AI, RULE, MEETING = "manual", "ai", "rule", "meeting"
    SOURCE_CHOICES = [(MANUAL, "вручну"), (AI, "AI"), (RULE, "правило"), (MEETING, "🎙 нарада")]
    STATUS_CHOICES = ProjectLink.STATUS_CHOICES

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="highlights")
    date = models.DateField("Дата", db_index=True)
    kind = models.CharField("Вид", max_length=10, choices=KIND_CHOICES, default=OTHER)
    title = models.CharField("Що сталося", max_length=255)
    quote = models.TextField("Цитата / примітка", blank=True, default="")
    message = models.ForeignKey("email_assistant.EmailMessage", null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="project_highlights", verbose_name="Лист")
    # 🤝 / 📞 «про що йшла мова»: зустріч або дзвінок з календаря — сюди ж модуль запису нарад додасть транскрипцію
    event = models.ForeignKey("calendar_app.CalendarEvent", null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="project_highlights", verbose_name="Подія календаря")
    source = models.CharField("Джерело", max_length=10, choices=SOURCE_CHOICES, default=MANUAL)
    meeting = models.ForeignKey("meetings.Meeting", null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="project_highlights", verbose_name="Нарада")
    meeting_second = models.PositiveIntegerField("Секунда наради", null=True, blank=True)
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
    MANUAL, TEMPLATE, AI, MEETING = "manual", "template", "ai", "meeting"
    SOURCE_CHOICES = [(MANUAL, "вручну"), (TEMPLATE, "шаблон"), (AI, "AI"), (MEETING, "🎙 нарада")]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="steps")
    phase = models.ForeignKey(ProjectPhase, null=True, blank=True, on_delete=models.SET_NULL, related_name="steps",
                              verbose_name="Фаза")
    order = models.PositiveIntegerField("Порядок", default=0)
    title = models.CharField("Крок", max_length=200)
    kind = models.CharField("Вид", max_length=10, choices=KIND_CHOICES, default=TASK)
    side = models.CharField("Хто", max_length=10, choices=SIDE_CHOICES, default=WE)
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="project_steps", verbose_name="Виконавець")
    party = models.ForeignKey("ProjectParty", null=True, blank=True, on_delete=models.SET_NULL, related_name="steps",
                              verbose_name="Сторона")
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
    agent_tasks = models.ManyToManyField("agent.AgentTask", blank=True, related_name="project_steps",
                                         verbose_name="Завдання агенту")
    notes = models.TextField("Примітки", blank=True, default="")
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default=MANUAL)
    meeting = models.ForeignKey("meetings.Meeting", null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="project_steps", verbose_name="Нарада")
    meeting_second = models.PositiveIntegerField("Секунда наради", null=True, blank=True)
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


# ── Ризики й тижневий статус (projects/risk_rules.py, health.py, weekly.py) ──────────────────────────────────────

class ProjectRisk(models.Model):
    """Ризик проєкту: ймовірність × вплив (1–3) → бал 1–9 (до 2 — 🟢, 3–4 — 🟡, від 6 — 🔴), власник, заходи, дата
    перегляду. source rule — відкриває й закриває risk_rules.py сам (rule_key — що саме перевіряло правило; умова
    зникла — closed_auto, повернулась — відкривається знову); закрила людина — правило його вже не відкриває.
    edited — людина змінила ймовірність / вплив: правило їх не перезаписує."""
    OPEN, WATCH, CLOSED = "open", "watch", "closed"
    STATUS_CHOICES = [(OPEN, "⚠️ Відкритий"), (WATCH, "👀 Стежимо"), (CLOSED, "✓ Закрито")]
    RULE, MANUAL, AI, MEETING = "rule", "manual", "ai", "meeting"
    SOURCE_CHOICES = [(RULE, "⚙️ Правило"), (MANUAL, "✍️ Вручну"), (AI, "🧠 AI"), (MEETING, "🎙 Нарада")]
    SCHEDULE, SUPPLY, CUSTOMER, QUALITY, COST, OTHER = "schedule", "supply", "customer", "quality", "cost", "other"
    CATEGORY_CHOICES = [(SCHEDULE, "⏰ Строки"), (SUPPLY, "🏭 Постачання й склад"), (CUSTOMER, "👤 Клієнт"),
                        (QUALITY, "🔧 Технічне й якість"), (COST, "💶 Гроші"), (OTHER, "❓ Інше")]
    PROBABILITY_CHOICES = [(1, "1 — малоймовірно"), (2, "2 — можливо"), (3, "3 — ймовірно")]
    IMPACT_CHOICES = [(1, "1 — незначний"), (2, "2 — помітний"), (3, "3 — серйозний")]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="risks")
    title = models.CharField("Ризик", max_length=200)
    description = models.TextField("Опис", blank=True, default="")
    category = models.CharField("Категорія", max_length=10, choices=CATEGORY_CHOICES, default=SCHEDULE)
    probability = models.PositiveSmallIntegerField("Ймовірність", choices=PROBABILITY_CHOICES, default=2)
    impact = models.PositiveSmallIntegerField("Вплив на строк і гроші", choices=IMPACT_CHOICES, default=2)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="project_risks", verbose_name="Власник")
    mitigation = models.TextField("Заходи", blank=True, default="",
                                  help_text="Що робимо, щоб ризик не стався або зашкодив менше")
    status = models.CharField("Стан", max_length=8, choices=STATUS_CHOICES, default=OPEN, db_index=True)
    review_on = models.DateField("Переглянути", null=True, blank=True)
    source = models.CharField("Звідки", max_length=8, choices=SOURCE_CHOICES, default=MANUAL)
    rule_key = models.CharField("Ключ правила", max_length=80, blank=True, default="")
    evidence = models.TextField("Підстава", blank=True, default="")
    message = models.ForeignKey("email_assistant.EmailMessage", null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="+", verbose_name="Лист")
    meeting = models.ForeignKey("meetings.Meeting", null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="project_risks", verbose_name="Нарада")
    meeting_second = models.PositiveIntegerField("Секунда наради", null=True, blank=True)
    steps = models.ManyToManyField(ProjectStep, blank=True, related_name="risks", verbose_name="Кроки під впливом")
    edited = models.BooleanField(default=False)
    closed_auto = models.BooleanField(default=False)
    opened_at = models.DateTimeField("Відкрито", default=timezone.now)
    closed_at = models.DateTimeField("Закрито", null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Ризик проєкту"
        verbose_name_plural = "⚠️ Ризики проєктів"
        ordering = ["project", "-probability", "-impact", "pk"]
        constraints = [models.UniqueConstraint(fields=["project", "rule_key"], condition=~Q(rule_key=""),
                                               name="unique_project_risk_rule")]

    def __str__(self):
        return f"{self.project.code} · {self.title}"

    @property
    def score(self) -> int:
        return (self.probability or 0) * (self.impact or 0)

    @property
    def level(self) -> str:
        return "red" if self.score >= 6 else ("yellow" if self.score >= 3 else "green")

    @property
    def is_open(self) -> bool:
        return self.status != self.CLOSED


class ProjectUpdate(models.Model):
    """Тижневий статус: колір, короткий текст, хто й коли, знімок (фаза, прогноз, строк, ризики, кроки). auto — знімок,
    який записав тижневий запуск сам (людина за тиждень статусу не написала): історія здоров'я без пропусків."""
    ON_TRACK, AT_RISK, OFF_TRACK = "on_track", "at_risk", "off_track"
    HEALTH_CHOICES = [(ON_TRACK, "🟢 За планом"), (AT_RISK, "🟡 Під загрозою"), (OFF_TRACK, "🔴 Не встигаємо")]
    MANUAL, AUTO = "manual", "auto"
    SOURCE_CHOICES = [(MANUAL, "людина"), (AUTO, "автоматично")]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="updates")
    health = models.CharField("Колір", max_length=10, choices=HEALTH_CHOICES, default=ON_TRACK)
    text = models.TextField("Статус", blank=True, default="")
    ai_draft = models.BooleanField("З AI-чернетки", default=False)
    source = models.CharField(max_length=8, choices=SOURCE_CHOICES, default=MANUAL)
    snapshot = models.JSONField(default=dict, blank=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="+", verbose_name="Хто")
    created_at = models.DateTimeField("Коли", default=timezone.now, db_index=True)

    class Meta:
        verbose_name = "Тижневий статус"
        verbose_name_plural = "Тижневі статуси"
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"{self.project.code} · {self.get_health_display()} · {self.created_at:%d.%m.%Y}"


class ProjectSettings(models.Model):
    """Налаштування проєктів (один запис): тижневий звіт відповідальним — день, година, лист / Telegram;
    знімок кольору в історію, якщо статус не написали; через скільки днів нагадувати про статус."""
    WEEKDAYS = [(0, "Понеділок"), (1, "Вівторок"), (2, "Середа"), (3, "Четвер"), (4, "П’ятниця"), (5, "Субота"),
                (6, "Неділя")]
    weekly_enabled = models.BooleanField("Щотижневий звіт відповідальним", default=False,
                                         help_text="Раз на тиждень кожному відповідальному — його відкриті проєкти: "
                                                   "колір, прогноз, нові й закриті ризики, кроки тижня")
    weekday = models.PositiveSmallIntegerField("День", choices=WEEKDAYS, default=0)
    hour = models.PositiveSmallIntegerField("Година", default=8, help_text="0–23, час Minerva")
    send_email = models.BooleanField("Листом", default=True)
    send_telegram = models.BooleanField("У Telegram", default=True,
                                        help_text="Якщо в профілі людини є Telegram ID і бот у «Налаштування → "
                                                  "Сповіщення»")
    auto_snapshot = models.BooleanField("Записувати колір в історію, якщо статус не написали", default=True)
    stale_days = models.PositiveSmallIntegerField("Нагадувати оновити статус через, днів", default=7)
    last_run_at = models.DateTimeField("Останній звіт", null=True, blank=True)
    last_result = models.CharField("Результат", max_length=255, blank=True, default="")

    class Meta:
        verbose_name = "Налаштування проєктів"
        verbose_name_plural = "⚙️ Налаштування проєктів"

    def __str__(self):
        return "Налаштування проєктів"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


# ── 🧠 AI-помічник проєкту (projects/ai_scan.py, ai_plan.py, forgotten.py, delegate.py) ─────────────────────────

class ProjectSuggestion(models.Model):
    """🧠 Пропозиція AI з листа: важливий момент або ризик з дослівною цитатою, яку Minerva знайшла в тексті листа.
    Бачить і вирішує лише той, хто запустив пошук (листи — з його скриньок): ✓ — момент / ризик проєкту
    (source AI), ✗ — та сама цитата більше не пропонується."""
    MOMENT, RISK = "moment", "risk"
    KIND_CHOICES = [(MOMENT, "📌 Важливий момент"), (RISK, "⚠️ Ризик")]
    SUGGESTED, ACCEPTED, REJECTED = "suggested", "accepted", "rejected"
    STATUS_CHOICES = [(SUGGESTED, "❔ Чекає рішення"), (ACCEPTED, "✅ Прийнято"), (REJECTED, "✖ Відхилено")]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="suggestions")
    kind = models.CharField("Що", max_length=8, choices=KIND_CHOICES)
    title = models.CharField("Назва", max_length=255)
    quote = models.TextField("Цитата з листа")
    message = models.ForeignKey("email_assistant.EmailMessage", null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="+", verbose_name="Лист")
    date = models.DateField("Дата", null=True, blank=True)
    moment_kind = models.CharField("Вид моменту", max_length=10, blank=True, default="",
                                   choices=ProjectHighlight.KIND_CHOICES)
    category = models.CharField("Категорія ризику", max_length=10, blank=True, default="",
                                choices=ProjectRisk.CATEGORY_CHOICES)
    probability = models.PositiveSmallIntegerField("Ймовірність", default=2)
    impact = models.PositiveSmallIntegerField("Вплив", default=2)
    mitigation = models.TextField("Заходи", blank=True, default="")
    key = models.CharField(max_length=40, help_text="Відбиток: вид + лист + цитата — відхилене не пропонується знову")
    status = models.CharField("Стан", max_length=10, choices=STATUS_CHOICES, default=SUGGESTED, db_index=True)
    model = models.CharField("Модель AI", max_length=60, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+", verbose_name="Чиї листи")
    created_at = models.DateTimeField("Знайдено", auto_now_add=True)
    decided_at = models.DateTimeField("Вирішено", null=True, blank=True)
    highlight = models.ForeignKey(ProjectHighlight, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="+")
    risk = models.ForeignKey(ProjectRisk, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        verbose_name = "Пропозиція AI"
        verbose_name_plural = "Пропозиції AI"
        ordering = ["-created_at", "-pk"]
        constraints = [models.UniqueConstraint(fields=["project", "key"], name="unique_project_suggestion")]

    def __str__(self):
        return f"{self.project.code} · {self.get_kind_display()} · {self.title}"

    @property
    def score(self) -> int:
        return (self.probability or 0) * (self.impact or 0)

    @property
    def level(self) -> str:
        return "red" if self.score >= 6 else ("yellow" if self.score >= 3 else "green")


class ProjectMailScan(models.Model):
    """Лист проєкту, який AI-помічник уже прочитав (пошук моментів і ризиків): удруге не читається."""
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="+")
    message = models.ForeignKey("email_assistant.EmailMessage", on_delete=models.CASCADE, related_name="+")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                             related_name="+")
    found = models.PositiveSmallIntegerField(default=0)
    scanned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Прочитаний AI лист"
        verbose_name_plural = "Прочитані AI листи"
        constraints = [models.UniqueConstraint(fields=["project", "message"], name="unique_project_mail_scan")]


class MailHandled(models.Model):
    """Лист, на який відповіли (або від якого отримали відповідь) поза поштою — телефоном, на зустрічі, іншим шляхом,
    або відповідь не потрібна. Позначає людина — власник скриньки — з «Що зараз» пульта проєкту.

    «Чекає нашої відповіді» / «Наш лист без відповіді» про цей лист більше не показується (overview.collect);
    новий лист у гілці — знову за звичайними правилами. Одна позначка на лист — для всіх проєктів з цією гілкою."""
    PHONE, MEETING, OTHER, NOT_NEEDED = "phone", "meeting", "other", "not_needed"
    HOW_CHOICES = [
        (PHONE, "📞 Телефоном"),
        (MEETING, "🤝 На зустрічі"),
        (OTHER, "✉️ Іншим шляхом"),
        (NOT_NEEDED, "🙅 Відповідь не потрібна"),
    ]
    message = models.OneToOneField("email_assistant.EmailMessage", on_delete=models.CASCADE, related_name="+",
                                   verbose_name="Лист")
    how = models.CharField("Як", max_length=12, choices=HOW_CHOICES)
    note = models.CharField("Примітка", max_length=255, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                             related_name="+", verbose_name="Хто позначив")
    created_at = models.DateTimeField("Позначено", auto_now_add=True)
    # «Про що йшла мова» — 📌 момент проєкту, записаний разом з позначкою (↶ прибирає і його)
    highlight = models.ForeignKey("ProjectHighlight", null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="+", verbose_name="Момент")

    class Meta:
        verbose_name = "Відповідь поза поштою"
        verbose_name_plural = "Відповіді поза поштою"

    def __str__(self):
        return f"{self.get_how_display()} · {self.message_id}"


class ProjectParty(models.Model):
    """🔗 Сторона проєкту: хто бере участь і кому віддає свою частину. Ланцюжок (дерево з гілками):
    постачальник компонентів → виробник → ми → замовник; delivers_to — кому ця сторона постачає / віддає роботу.
    Кроки плану можуть належати стороні (ProjectStep.party) — тоді «Хто» на діаграмі Ганта — її назва.
    Логіка — projects/parties.py."""
    CUSTOMER, WE, CONTRACTOR, MANUFACTURER, SUPPLIER, LOGISTICS, OTHER = (
        "customer", "we", "contractor", "manufacturer", "supplier", "logistics", "other")
    ROLE_CHOICES = [
        (CUSTOMER, "👤 Замовник"), (WE, "🏢 Ми — виконавець"), (CONTRACTOR, "🧑‍🔧 Підрядник"),
        (MANUFACTURER, "🏭 Виробник"), (SUPPLIER, "📦 Постачальник компонентів"), (LOGISTICS, "🚚 Логістика"),
        (OTHER, "❓ Інше"),
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="parties")
    role = models.CharField("Роль", max_length=12, choices=ROLE_CHOICES, default=SUPPLIER)
    name = models.CharField("Назва", max_length=200)
    what = models.CharField("Що постачає / робить", max_length=255, blank=True, default="",
                            help_text="«Друковані плати», «корпуси», «модулі U3, U101», «складання й тест»")
    delivers_to = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="suppliers_of", verbose_name="Кому віддає")
    email = models.CharField("Email контакту", max_length=254, blank=True, default="")
    customer = models.ForeignKey("crm.Customer", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
                                 verbose_name="Клієнт CRM")
    supplier = models.ForeignKey("inventory.Supplier", null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="+", verbose_name="Постачальник (склад)")
    note = models.CharField("Примітка", max_length=255, blank=True, default="")
    order = models.PositiveIntegerField("Порядок", default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Сторона проєкту"
        verbose_name_plural = "🔗 Сторони проєкту"
        ordering = ["order", "pk"]

    def __str__(self):
        return f"{self.name} ({self.get_role_display()})"
