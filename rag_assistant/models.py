"""Помічник по продукції і складу: підключення до зовнішньої RAG-системи (даташити + каталог Minerva).

Окремо від «Minerva AI» (ai_assistant): власні налаштування, розмови й історія.
Ключ RAG API зберігається лише на сервері Minerva; браузер його не отримує.
"""
import uuid

from django.conf import settings
from django.db import models


class RagSettings(models.Model):
    """Підключення до RAG API — синглтон (pk=1)."""
    LANGUAGES = [("uk", "Українська"), ("de", "Deutsch"), ("en", "English")]

    enabled = models.BooleanField("Увімкнено", default=False,
                                  help_text="Показувати кнопку помічника в Minerva.")
    name = models.CharField("Назва помічника", max_length=40, default="Minerva")
    base_url = models.URLField("Адреса RAG API", default="https://app.sevskiy.com",
                               help_text="Без /v1 у кінці, напр. https://app.sevskiy.com або http://192.168.2.68:8787")
    api_key = models.CharField("Ключ RAG API", max_length=200, blank=True, default="",
                               help_text="Bearer-ключ RAG («Копіювати ключ API» у RAG Documents).")
    verify_tls = models.BooleanField("Перевіряти сертифікат HTTPS", default=True)
    timeout = models.PositiveSmallIntegerField("Тайм-аут запиту, с", default=20)
    language = models.CharField("Мова відповідей", max_length=2, choices=LANGUAGES, default="uk")
    latest_only = models.BooleanField("Лише останні ревізії даташитів", default=True)
    last_check_at = models.DateTimeField("Остання перевірка", null=True, blank=True)
    last_check_result = models.CharField("Результат перевірки", max_length=255, blank=True, default="")

    class Meta:
        verbose_name = "Налаштування помічника"
        verbose_name_plural = "⚙️ Налаштування помічника"

    def __str__(self):
        return "Налаштування помічника по продукції"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        return cls.objects.get_or_create(pk=1)[0]

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)


class RagConversation(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="rag_conversations",
                             verbose_name="Користувач")
    title = models.CharField("Тема", max_length=120, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Розмова"
        verbose_name_plural = "💬 Розмови"
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title or f"Розмова #{self.pk}"


class RagMessage(models.Model):
    ROLE_USER, ROLE_ASSISTANT = "user", "assistant"
    PENDING, DONE, FAILED = "pending", "done", "failed"
    PRODUCTS, GUIDE = "products", "guide"

    conversation = models.ForeignKey(RagConversation, on_delete=models.CASCADE, related_name="messages")
    mode = models.CharField("Хто відповідав", max_length=10, default=PRODUCTS,
                            choices=[(PRODUCTS, "📦 продукція (RAG)"), (GUIDE, "📚 гід по Minerva")])
    role = models.CharField(max_length=10, choices=[(ROLE_USER, "Користувач"), (ROLE_ASSISTANT, "Помічник")])
    content = models.TextField(blank=True, default="")
    status = models.CharField(max_length=10, default=DONE,
                              choices=[(PENDING, "Виконується"), (DONE, "Готово"), (FAILED, "Помилка")])
    rag_job_id = models.CharField(max_length=64, blank=True, default="")
    stage = models.CharField(max_length=200, blank=True, default="")
    sources = models.JSONField(default=list, blank=True)
    products = models.JSONField(default=list, blank=True)
    trace = models.JSONField(default=list, blank=True)
    usage = models.JSONField(default=dict, blank=True)
    error = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Повідомлення"
        verbose_name_plural = "Повідомлення"
        ordering = ["created_at", "pk"]

    def __str__(self):
        return f"{self.get_role_display()}: {self.content[:60]}"


# ── База знань: пошта Minerva → приватний індекс RAG (api/schemas/knowledge, integration/API_CONTRACT.md) ──

KNOWLEDGE_DEFAULT_CATEGORIES = ["customer", "supplier", "internal", "other"]


def _default_categories():
    return list(KNOWLEDGE_DEFAULT_CATEGORIES)


def _default_attachment_types():
    return ["pdf", "docx", "txt", "csv"]


class KnowledgeSettings(models.Model):
    """Що з пошти можна передавати в RAG — синглтон (pk=1). До першого ввімкнення стрічка порожня."""

    enabled = models.BooleanField(
        "Індексувати пошту", default=False,
        help_text="Вимкнення — пауза: нові листи не додаються; видалення, зміни доступу й вмісту вже "
                  "переданих листів надходять далі. Повне очищення індексу — окрема кнопка.")
    accounts = models.ManyToManyField(
        "email_assistant.EmailAccount", blank=True, verbose_name="Скриньки",
        help_text="Порожньо — усі активні скриньки.")
    categories = models.JSONField(
        "Категорії листів", default=_default_categories, blank=True,
        help_text="Розсилки й сервісні сповіщення за замовчуванням не індексуються.")
    index_from = models.DateField(
        "Листи починаючи з", null=True, blank=True,
        help_text="Почніть з короткого періоду (напр. 30 днів) і розширюйте після перевірки.")
    include_attachments = models.BooleanField("Індексувати вкладення", default=True)
    attachment_types = models.JSONField("Типи вкладень", default=_default_attachment_types, blank=True)
    attachment_max_mb = models.PositiveSmallIntegerField("Макс. розмір вкладення, МБ", default=15)
    instance_id = models.CharField(
        "Назва цього сервера для RAG", max_length=64, blank=True, default="",
        help_text="Стала назва середовища, напр. sevskiy-prod (латиниця, цифри, . _ -). Змінна середовища "
                  "MINERVA_INSTANCE_ID має пріоритет. Не копіюйте цю назву разом із базою на інший сервер.")
    private_api_key = models.CharField(
        "Ключ приватного пошуку RAG", max_length=200, blank=True, default="",
        help_text="Окремий Bearer-ключ RAG для /v2/* (не ключ магазину). Лише на сервері Minerva.")
    private_base_url = models.CharField(
        "Адреса приватного API RAG", max_length=200, blank=True, default="",
        help_text="http(s)://адреса:порт без /v2 — складається з полів «Адреса RAG-ПК», «Порт», «HTTPS». "
                  "Порожньо — пошук RAG вимкнено.")
    private_verify_tls = models.BooleanField(
        "Перевіряти HTTPS-сертифікат RAG", default=True,
        help_text="Вимикати лише для власного (self-signed) сертифіката в закритій мережі.")
    rag_failures = models.PositiveSmallIntegerField(default=0, editable=False,
                                                    help_text="Збоїв поспіль (запобіжник).")
    rag_down_until = models.DateTimeField(null=True, blank=True, editable=False,
                                          help_text="До цього часу Minerva не звертається до RAG.")
    last_capabilities = models.JSONField(default=dict, blank=True, editable=False)
    last_capabilities_at = models.DateTimeField(null=True, blank=True, editable=False)
    generation = models.UUIDField(
        "Покоління журналу", default=uuid.uuid4, editable=False,
        help_text="Змінюється дією «Нове покоління» після відновлення БД — RAG тоді робить повну звірку.")
    settings_version = models.PositiveIntegerField(default=1, editable=False)
    pruned_seq = models.BigIntegerField(
        default=0, editable=False, help_text="Журнал до цього seq очищено: старші курсори → 410 cursor_expired.")
    last_refresh_at = models.DateTimeField("Остання звірка стрічки", null=True, blank=True, editable=False)
    last_refresh_result = models.CharField(max_length=255, blank=True, default="", editable=False)
    feed_paused = models.BooleanField(
        "Індексацію призупинено", default=False, editable=False,
        help_text="Кнопка «⏸ Пауза»: стрічка відповідає RAG «зачекайте» (429), звіти RAG приймаються; "
                  "пошук працює з уже проіндексованим.")
    feed_paused_at = models.DateTimeField(null=True, blank=True, editable=False)
    # Етап 3 — що Minerva сама додає в базу знань (документи типу document, KnowledgeItem.auto)
    auto_offers = models.BooleanField(
        "Angebote", default=False,
        help_text="Кожна надіслана версія Angebot (DOCX / PDF) — документ бази знань, прив'язаний до клієнта й проєкту.")
    auto_order_files = models.BooleanField(
        "Документи замовлень", default=False,
        help_text="Файли з папки замовлення (media/orders/<джерело>/<номер>/): рахунки, пакувальні листи, "
                  "Auftragsbestätigung… — прив'язані до замовлення й клієнта.")
    auto_datasheets = models.BooleanField(
        "Даташити товарів", default=False,
        help_text="Файл даташита в картці товару — прив'язаний до товару (посилання на сайт виробника не завантажуються).")

    class Meta:
        verbose_name = "База знань (пошта → RAG)"
        verbose_name_plural = "🧠 База знань (пошта → RAG)"

    def __str__(self):
        return "База знань"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        return cls.objects.get_or_create(pk=1)[0]


class KnowledgeActivity(models.Model):
    """Що RAG робить зараз — за його зверненнями до стрічки (синглтон pk=1, лише для показу на «Базі знань»).
    Окремо від KnowledgeSettings, щоб збереження форми налаштувань не затирало лічильники."""
    IDLE, SNAPSHOT, CHANGES = "idle", "snapshot", "changes"

    phase = models.CharField(max_length=10, default=IDLE)
    phase_started_at = models.DateTimeField(null=True, blank=True)
    snapshot_total = models.PositiveIntegerField(default=0, help_text="Документів у знімку повної звірки")
    snapshot_served = models.PositiveIntegerField(default=0, help_text="Скільки з них RAG уже забрав")
    reported = models.PositiveIntegerField(default=0, help_text="Прийнято записів зі звітів RAG за етап")
    last_call = models.CharField(max_length=200, blank=True, default="")
    last_call_at = models.DateTimeField(null=True, blank=True)
    rag_types = models.JSONField(default=list, blank=True,
                                 help_text="Які типи документів RAG просив востаннє (/changes/?types=); старий RAG — лише email")

    class Meta:
        verbose_name = "Активність RAG"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        return cls.objects.get_or_create(pk=1)[0]


class KnowledgeChange(models.Model):
    """Журнал змін (id = seq). Записувачі серіалізовані (knowledge.journal_writer): порядок seq = порядок commit."""
    UPSERT, DELETE = "upsert", "delete"

    id = models.BigAutoField(primary_key=True)
    doc_type = models.CharField(max_length=20, default="email")
    obj_id = models.PositiveIntegerField()
    action = models.CharField(max_length=10, choices=[(UPSERT, "upsert"), (DELETE, "delete")])
    reason = models.CharField(max_length=20, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Подія бази знань"
        verbose_name_plural = "Журнал бази знань"
        ordering = ["id"]

    def __str__(self):
        return f"#{self.id} {self.action} {self.doc_type}:{self.obj_id}"

    @property
    def document_id(self) -> str:
        return f"{self.doc_type}:{self.obj_id}"


class KnowledgeDoc(models.Model):
    """
    Стан документа для RAG. revision — монотонна (кожна зміна content_version, tombstone, повернення → +1);
    рядок ніколи не видаляється. Статус RAG (state) стосується status_revision і спроби attempt_id.
    """
    PENDING, INDEXED, FAILED, SKIPPED, DELETED = "pending", "indexed", "failed", "skipped", "deleted"
    STATES = [(PENDING, "очікує"), (INDEXED, "у індексі"), (FAILED, "помилка"),
              (SKIPPED, "пропущено"), (DELETED, "видалено")]

    doc_type = models.CharField(max_length=20, default="email")
    obj_id = models.PositiveIntegerField()
    revision = models.PositiveIntegerField(default=0)
    fed = models.BooleanField(default=False, help_text="Має бути в індексі зараз (не tombstone).")
    feed_version = models.CharField(max_length=80, blank=True, default="", help_text="content_version чинної ревізії")
    fed_at = models.DateTimeField(null=True, blank=True)
    state = models.CharField(max_length=10, choices=STATES, default=PENDING, db_index=True)
    status_revision = models.PositiveIntegerField(default=0)
    attempt_id = models.PositiveBigIntegerField(default=0)
    chunks = models.PositiveIntegerField(default=0)
    attachments = models.JSONField(
        null=True, blank=True, default=None,
        help_text="Звіт RAG по вкладеннях для status_revision: [{index, state, reason}]. "
                  "None — RAG про вкладення цієї ревізії не звітував (наприклад, проіндексовано до 28.10.2026).")
    error = models.CharField(max_length=500, blank=True, default="")
    indexed_at = models.DateTimeField(null=True, blank=True)
    last_status_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Документ бази знань"
        verbose_name_plural = "Документи бази знань"
        constraints = [models.UniqueConstraint(fields=["doc_type", "obj_id"], name="unique_knowledge_doc")]
        indexes = [models.Index(fields=["doc_type", "fed"])]

    def __str__(self):
        return f"{self.document_id} r{self.revision} [{self.current_state}]"

    @property
    def document_id(self) -> str:
        return f"{self.doc_type}:{self.obj_id}"

    @property
    def current_state(self) -> str:
        """Стан чинної ревізії: статус старшої ревізії не рахується."""
        return self.state if self.status_revision == self.revision else self.PENDING


class KnowledgeSnapshot(models.Model):
    """Узгоджений знімок набору документів індексу (REPEATABLE READ разом із snapshot_seq). Живе 24 год."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    snapshot_seq = models.BigIntegerField()
    generation = models.UUIDField()
    total = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        verbose_name = "Знімок бази знань"
        verbose_name_plural = "Знімки бази знань"


class KnowledgeSnapshotItem(models.Model):
    snapshot = models.ForeignKey(KnowledgeSnapshot, on_delete=models.CASCADE, related_name="items")
    position = models.PositiveIntegerField()
    doc_type = models.CharField(max_length=20, default="email")
    obj_id = models.PositiveIntegerField()
    revision = models.PositiveIntegerField()
    content_version = models.CharField(max_length=80)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["snapshot", "position"], name="unique_snapshot_position")]
        ordering = ["position"]


class RetrievalJob(models.Model):
    """
    Пошук у приватному індексі RAG (/v2/retrieval/jobs) від імені користувача Minerva.
    Браузер бачить лише id цього запису; job_id RAG і повторні звернення — тільки на сервері.
    hits — уже перевірені Minerva (права, допуск до індексу, ревізія), dropped — скільки відкинуто.
    """
    PENDING, QUEUED, RUNNING, COMPLETED = "pending", "queued", "running", "completed"
    FAILED, CANCELLED, UNAVAILABLE = "failed", "cancelled", "unavailable"
    STATUSES = [(PENDING, "очікує"), (QUEUED, "у черзі RAG"), (RUNNING, "виконується"),
                (COMPLETED, "готово"), (FAILED, "помилка"), (CANCELLED, "скасовано"),
                (UNAVAILABLE, "RAG недоступний")]
    TERMINAL = (COMPLETED, FAILED, CANCELLED, UNAVAILABLE)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="retrieval_jobs")
    purpose = models.CharField(max_length=20, default="history")
    thread = models.ForeignKey("email_assistant.EmailThread", null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="+")
    idempotency_key = models.CharField(max_length=128, unique=True)
    rag_job_id = models.CharField(max_length=32, blank=True, default="")
    resubmit = models.BooleanField(default=False, help_text="Результат POST невідомий — повторити тим самим ключем.")
    request = models.JSONField(default=dict)
    status = models.CharField(max_length=12, choices=STATUSES, default=PENDING, db_index=True)
    hits = models.JSONField(default=list, blank=True)
    dropped = models.PositiveSmallIntegerField(default=0)
    index = models.JSONField(default=dict, blank=True)
    message = models.CharField(max_length=300, blank=True, default="")
    poll_after_ms = models.PositiveIntegerField(default=1000)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Пошук у RAG"
        verbose_name_plural = "Пошуки в RAG"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.purpose} {self.status} ({self.user})"

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL


class EntityHelp(models.Model):
    """
    «🧭 Огляд» і «❓ Запитати» в картці проєкту, клієнта, замовлення чи товару (rag_assistant/entity_help.py):
    дані Minerva + листи + база знань RAG → відповідь AI з посиланнями [#N]. Як ReplyBrief у пошті, але для картки.
    Лише власник бачить свій запис (RAG шукає лише в його скриньках).
    """
    KINDS = [("project", "проєкт"), ("customer", "клієнт"), ("order", "замовлення"), ("product", "товар")]
    MODE_ANSWER, MODE_REVIEW = "answer", "review"
    MODES = [(MODE_ANSWER, "❓ запитання"), (MODE_REVIEW, "🧭 огляд")]
    RUNNING, DONE, ERROR = "running", "done", "error"
    STATUSES = [(RUNNING, "виконується"), (DONE, "готово"), (ERROR, "помилка")]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="entity_helps")
    kind = models.CharField(max_length=10, choices=KINDS)
    obj_id = models.PositiveIntegerField()
    mode = models.CharField(max_length=10, choices=MODES, default=MODE_ANSWER)
    question = models.TextField(blank=True, default="")
    target = models.CharField(max_length=300, blank=True, default="", help_text="Про що питали (назва картки)")
    stages = models.JSONField(default=list, blank=True)
    sources = models.JSONField(default=list, blank=True, help_text="Джерела [#N]: група, підпис, посилання, текст")
    used = models.JSONField(default=list, blank=True)
    result = models.TextField(blank=True, default="", help_text="Відповідь AI або JSON огляду")
    model = models.CharField(max_length=60, blank=True, default="")
    fingerprint = models.CharField(max_length=64, blank=True, default="", help_text="Стан даних на момент збору")
    status = models.CharField(max_length=10, choices=STATUSES, default=RUNNING)
    error = models.CharField(max_length=300, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Огляд / запитання по картці"
        verbose_name_plural = "Огляди й запитання по картках"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "kind", "obj_id", "mode"], name="entityhelp_lookup")]

    def __str__(self):
        return f"{self.get_kind_display()} #{self.obj_id} · {self.mode} · {self.status}"


def _knowledge_upload_to(instance, filename):
    return f"knowledge/{uuid.uuid4().hex[:12]}/{filename}"


class KnowledgeItem(models.Model):
    """
    Документ бази знань поза поштою (тип document у стрічці для RAG, rag_assistant/knowledge_items.py):
    етап 2 — файл або посилання, додані людиною в картці клієнта / проєкту / замовлення / товару;
    етап 3 — автоматично: надіслана версія Angebot, файл із папки замовлення, даташит товару (auto, source_key).
    Видимість: team — усі, хто бачить пов'язану картку; private — лише власник. Текст файлу витягує RAG,
    текст сторінки за посиланням — Minerva (RAG зовнішніх адрес не відкриває).
    """
    UPLOAD, LINK, OFFER, ORDER_FILE, DATASHEET = "upload", "link", "offer", "order_file", "datasheet"
    SOURCE = "source"
    KINDS = [(UPLOAD, "📄 файл"), (LINK, "🔗 посилання"), (OFFER, "📝 Angebot"), (ORDER_FILE, "📦 документ замовлення"),
             (DATASHEET, "📑 даташит"), (SOURCE, "📂 з джерела знань")]
    TEAM, PRIVATE = "team", "private"
    VISIBILITY = [(TEAM, "усі, хто бачить картку"), (PRIVATE, "🔒 лише я")]

    kind = models.CharField("Вид", max_length=12, choices=KINDS, default=UPLOAD)
    title = models.CharField("Назва", max_length=300)
    note = models.TextField("Опис / навіщо", blank=True, default="",
                            help_text="Індексується разом із документом: коротко, що в ньому важливого.")
    file = models.FileField("Файл", upload_to=_knowledge_upload_to, max_length=400, blank=True)
    file_name = models.CharField(max_length=300, blank=True, default="")
    file_size = models.PositiveBigIntegerField(default=0)
    content_type = models.CharField(max_length=200, blank=True, default="")
    file_sha256 = models.CharField(max_length=80, blank=True, default="", help_text="sha256:… байтів файлу")
    url = models.URLField("Посилання", max_length=2000, blank=True, default="")
    text = models.TextField(blank=True, default="", help_text="Текст сторінки за посиланням (Minerva забирає сама)")
    fetched_at = models.DateTimeField(null=True, blank=True)
    fetch_error = models.CharField(max_length=300, blank=True, default="")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="knowledge_items", verbose_name="Додав")
    visibility = models.CharField("Хто бачить", max_length=10, choices=VISIBILITY, default=TEAM)
    customer = models.ForeignKey("crm.Customer", null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="knowledge_items", verbose_name="Клієнт")
    project = models.ForeignKey("projects.Project", null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="knowledge_items", verbose_name="Проєкт")
    order = models.ForeignKey("sales.SalesOrder", null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="knowledge_items", verbose_name="Замовлення")
    product = models.ForeignKey("inventory.Product", null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="knowledge_items", verbose_name="Товар")
    offer = models.ForeignKey("offers.Offer", null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="knowledge_items", verbose_name="Angebot")
    auto = models.BooleanField(default=False, help_text="Додано автоматично (етап 3): Minerva сама оновлює й прибирає")
    source_key = models.CharField(max_length=300, blank=True, default="", db_index=True,
                                  help_text="Для автоматичних: offer:<версія>, order_file:<замовлення>:<файл>, datasheet:<товар>")
    knowledge_source = models.ForeignKey("rag_assistant.KnowledgeSource", null=True, blank=True,
                                         on_delete=models.CASCADE, related_name="items", verbose_name="Джерело знань")
    source_path = models.CharField(max_length=1000, blank=True, default="",
                                   help_text="Папка: шлях файлу відносно папки джерела; сайт — адреса сторінки")
    source_stamp = models.CharField(max_length=80, blank=True, default="",
                                    help_text="Розмір і час зміни файлу / хеш сторінки — щоб перечитувати лише змінене")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Документ / посилання бази знань"
        verbose_name_plural = "📚 Документи й посилання бази знань"
        ordering = ["-created_at"]

    def __str__(self):
        return self.title

    @property
    def document_id(self) -> str:
        return f"document:{self.pk}"


class KnowledgeSource(models.Model):
    """
    «📂 Джерело знань» (rag_assistant/knowledge_sources.py): папка або сайт, які Minerva сама обходить за розкладом
    і передає в базу знань (KnowledgeItem kind=source; тип document для RAG). Папка — лише всередині дозволених коренів
    (knowledge_sources у проєкті, MEDIA/knowledge_sources, MINERVA_KNOWLEDGE_ROOTS); файли не копіюються.
    Папка без шляху — Minerva створює її сама в MEDIA/knowledge_sources (managed), файли туди завантажують у браузері.
    """
    FOLDER, WEBSITE = "folder", "website"
    KINDS = [(FOLDER, "📁 папка"), (WEBSITE, "🌐 сайт")]
    TEAM, PRIVATE = "team", "private"
    VISIBILITY = [(TEAM, "усі, хто бачить пов'язані картки (без прив'язок — усі співробітники)"), (PRIVATE, "🔒 лише я")]

    name = models.CharField("Назва", max_length=200)
    kind = models.CharField("Вид", max_length=10, choices=KINDS, default=FOLDER)
    path = models.CharField("Папка", max_length=500, blank=True, default="",
                            help_text="Шлях усередині Minerva (лише в дозволених коренях); створюючи джерело без шляху, "
                                      "Minerva заводить свою папку в MEDIA/knowledge_sources — файли туди завантажують з ПК")
    url = models.TextField("Посилання", blank=True, default="",
                           help_text="По одному в рядку: sitemap.xml, сторінка сайту або файл (PDF, DOCX)")
    follow_links = models.BooleanField(
        "Брати й інші сторінки сайту", default=True,
        help_text="sitemap.xml — усі його сторінки; сторінка — ще й посилання з неї на той самий сайт. "
                  "Вимкнено — лише ці посилання")
    recursive = models.BooleanField("З підпапками", default=True)
    file_types = models.JSONField("Типи файлів", default=list, blank=True,
                                  help_text="Порожньо — pdf, docx, txt, csv, md, html, htm, xlsx")
    max_files = models.PositiveIntegerField("Макс. файлів / сторінок", default=2000)
    max_file_mb = models.PositiveSmallIntegerField("Макс. розмір файлу, МБ", default=25)
    rescan_hours = models.PositiveSmallIntegerField("Перевіряти кожні, год", default=24)
    enabled = models.BooleanField("Увімкнено", default=True,
                                  help_text="Вимкнено — документи джерела прибираються з RAG (у Minerva лишаються)")
    visibility = models.CharField("Хто бачить", max_length=10, choices=VISIBILITY, default=TEAM)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="knowledge_sources", verbose_name="Додав")
    customer = models.ForeignKey("crm.Customer", null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="+", verbose_name="Клієнт (для всіх документів)")
    projects = models.ManyToManyField(
        "projects.Project", blank=True, related_name="knowledge_sources", verbose_name="Проєкти (для всіх документів)",
        help_text="Можна кілька; порожньо — загальні документи без проєкту. Документи джерела видно в картці "
                  "кожного з цих проєктів, у RAG вони прив'язані до всіх")
    product = models.ForeignKey("inventory.Product", null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="+", verbose_name="Товар (для всіх документів)")
    link_products_by_sku = models.BooleanField(
        "Артикул у назві файлу → товар", default=True,
        help_text="Файл «FT220407_Datenblatt.pdf» прив'яжеться до товару FT220407 (артикул каталогу в назві файлу чи сторінки)")
    last_scan_at = models.DateTimeField("Остання перевірка", null=True, blank=True, editable=False)
    last_scan_result = models.CharField(max_length=300, blank=True, default="", editable=False)
    last_error = models.CharField(max_length=300, blank=True, default="", editable=False)
    scan_started_at = models.DateTimeField(null=True, blank=True, editable=False,
                                           help_text="Йде перевірка (порожньо — ні); старіша за годину вважається зупиненою")
    rescan_requested = models.BooleanField(default=False, editable=False,
                                           help_text="Під час перевірки додали файли — перевірити ще раз одразу після неї")
    managed = models.BooleanField("Папку створила Minerva", default=False, editable=False,
                                  help_text="Файли завантажують у браузері; видалити файл можна там же")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Джерело знань"
        verbose_name_plural = "📂 Джерела знань"
        ordering = ["name"]

    def __str__(self):
        return self.name

