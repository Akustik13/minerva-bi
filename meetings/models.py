"""🎙 Наради: запис на RAG-ПК → розшифровка й ключові моменти → сторінка наради в Minerva (лише для того, хто записав).

Логіка — meetings/services.py (підготовка, квиток, push від RAG-ПК, ✓ / ✕, видалення, прибирання), база знань —
meetings/knowledge.py, повідомлення — meetings/notify.py. Розшифровка лежить окремо (MeetingTranscript), щоб списки,
опитування статусу й пульт проєкту її не завантажували. __str__ наради нейтральний — назва й текст ніколи не
потрапляють у журнали (AuditLog / LogEntry).
"""
import uuid

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

TEXT_ONLY, AUDIO_TEXT = "text_only", "audio_text"
RETENTION_CHOICES = [
    (TEXT_ONLY, "лише текст — аудіо видаляється з RAG-ПК, щойно розшифровку готово (рекомендовано)"),
    (AUDIO_TEXT, "аудіо + текст — аудіо лишається на RAG-ПК до 30 днів (щоб розшифрувати ще раз)"),
]
LANG_CHOICES = [("", "Автоматично"), ("uk", "Українська"), ("de", "Deutsch"), ("en", "English"), ("ru", "Русский")]
CONSENT_LANG_CHOICES = [("de", "Deutsch"), ("en", "English"), ("uk", "Українська")]


class MeetingSettings(models.Model):
    """Налаштування нарад (один запис, pk=1): вмикання після перевірки текстів згоди, вікно запису (RAG-ПК),
    строки зберігання, повідомлення, тексти для читання вголос; стан RAG-ПК (bench_*) пише лише синхронізація."""
    enabled = models.BooleanField(
        "Записування нарад увімкнено", default=False,
        help_text="Вмикайте після перевірки текстів згоди юристом / DSB і посилання на інформацію за ст. 13 DSGVO. "
                  "Поки вимкнено, кнопок «🎙 Записати нараду» ніде немає.")
    privacy_url = models.URLField("Інформація за ст. 13 DSGVO (посилання)", max_length=500, blank=True, default="",
                                  help_text="Сторінка з інформацією для учасників — показується в підготовці наради "
                                            "й у примітці для запрошення")
    texts_reviewed_at = models.DateTimeField("Тексти згоди перевірено", null=True, blank=True)
    texts_reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                          related_name="+", verbose_name="Хто відмітив перевірку текстів")
    works_council_informed = models.BooleanField("Betriebsrat поінформовано / угоду укладено", default=False)
    bench_url = models.URLField("Адреса вікна запису (RAG-ПК)", max_length=300, default="https://192.168.2.68:8791")
    ticket_ttl_seconds = models.PositiveSmallIntegerField(
        "Посилання на вікно запису діє, секунд", default=120,
        validators=[MinValueValidator(30), MaxValueValidator(1800)])
    default_retention = models.CharField("Режим за замовчуванням", max_length=10, choices=RETENTION_CHOICES,
                                         default=TEXT_ONLY)
    transcript_retention_days = models.PositiveIntegerField(
        "Розшифровку зберігати, днів", default=365, validators=[MinValueValidator(30), MaxValueValidator(3650)],
        help_text="Щонайменше 30. Діє менше з двох: строк, з яким нараду записали, і цей")
    audio_retention_days = models.PositiveSmallIntegerField(
        "Аудіо «аудіо + текст» на RAG-ПК, днів", default=30, validators=[MinValueValidator(1), MaxValueValidator(30)])
    protocol_retention_days = models.PositiveIntegerField(
        "Протокол згоди зберігати, днів", default=1095, validators=[MinValueValidator(30), MaxValueValidator(3650)],
        help_text="Після цього нарада видаляється повністю (пункти, уже додані в проєкт, лишаються)")
    inactive_owner_days = models.PositiveSmallIntegerField(
        "Вимкнений користувач — прибрати розшифровки через, днів", default=30,
        validators=[MinValueValidator(1), MaxValueValidator(365)])
    notify_email = models.BooleanField("Повідомляти листом", default=True)
    notify_telegram = models.BooleanField("Повідомляти в Telegram", default=True)
    consent_text_de = models.TextField("Текст для читання вголос — Deutsch", blank=True, default="",
                                       help_text="Порожньо — вбудований текст")
    consent_text_en = models.TextField("Текст для читання вголос — English", blank=True, default="",
                                       help_text="Порожньо — вбудований текст")
    consent_text_uk = models.TextField("Текст для читання вголос — українська", blank=True, default="",
                                       help_text="Порожньо — вбудований текст")
    bench_seen_at = models.DateTimeField("RAG-ПК востаннє на зв'язку", null=True, blank=True)
    bench_info = models.JSONField(default=dict, blank=True)
    cleanup_at = models.DateTimeField("Останнє прибирання", null=True, blank=True)

    class Meta:
        verbose_name = "Налаштування нарад"
        verbose_name_plural = "⚙️ Налаштування нарад"

    def __str__(self):
        return "Налаштування нарад"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    get = load


class Meeting(models.Model):
    """Нарада: підготовка (назва, проєкт, клієнт, хто присутній, мови, режим) → запис на RAG-ПК (згода підтверджується
    у вікні запису перед ▶) → розшифровка й ключові моменти. Бачить лише owner (і суперкористувач — ні)."""
    READY, RECORDING, PROCESSING, REVIEW, FAILED = "ready", "recording", "processing", "review", "failed"
    STATUS_CHOICES = [(READY, "Готово до запису"), (RECORDING, "Йде запис"), (PROCESSING, "Обробка на RAG-ПК"),
                      (REVIEW, "Розшифровка готова"), (FAILED, "Не вдалося")]
    BOTH, MIC, MEETING_ONLY = "both", "mic", "meeting"
    CAPTURE_CHOICES = [("", "—"), (BOTH, "мікрофон + звук наради"), (MIC, "лише мікрофон (очна зустріч)"),
                       (MEETING_ONLY, "лише звук наради")]

    uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="meetings",
                              verbose_name="Хто записав")
    title = models.CharField("Назва", max_length=200)
    project = models.ForeignKey("projects.Project", null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="meetings", verbose_name="Проєкт")
    customer = models.ForeignKey("crm.Customer", null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="meetings", verbose_name="Клієнт")
    calendar_event = models.ForeignKey("calendar_app.CalendarEvent", null=True, blank=True, on_delete=models.SET_NULL,
                                       related_name="+", verbose_name="Подія календаря")
    continues = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="continuations", verbose_name="Продовження наради")
    status = models.CharField("Стан", max_length=12, choices=STATUS_CHOICES, default=READY, db_index=True)
    retention = models.CharField("Режим", max_length=10, choices=RETENTION_CHOICES, default=TEXT_ONLY)
    retention_days = models.PositiveIntegerField("Розшифровку зберігати, днів", default=365)
    audio_days = models.PositiveSmallIntegerField("Аудіо на RAG-ПК, днів", default=30)
    lang_l = models.CharField("Моя мова (мікрофон)", max_length=5, choices=LANG_CHOICES, blank=True, default="")
    lang_r = models.CharField("Мова співрозмовників", max_length=5, choices=LANG_CHOICES, blank=True, default="")

    # ── протокол згоди (лишається й після видалення розшифровки) ──
    consent_lang = models.CharField("Мова тексту згоди", max_length=2, choices=CONSENT_LANG_CHOICES, blank=True,
                                    default="")
    consent_text = models.TextField("Текст, який читали вголос", blank=True, default="")
    consent_announced = models.BooleanField("Запис оголошено, мету пояснено", default=False)
    consent_all_agreed = models.BooleanField("Усі, кого чути, вголос погодились", default=False)
    consent_at = models.DateTimeField("Згоду підтверджено", null=True, blank=True)
    participants = models.JSONField("Хто на нараді", default=list, blank=True)
    consent_note = models.TextField("Примітка до протоколу", blank=True, default="")
    consent_log = models.JSONField(default=list, blank=True)
    consent_excerpt = models.JSONField(default=list, blank=True)

    # ── зв'язок із вікном запису (RAG-ПК) ──
    ticket_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)
    ticket_expires_at = models.DateTimeField(null=True, blank=True)
    ticket_redeemed_at = models.DateTimeField(null=True, blank=True)
    push_hash = models.CharField(max_length=64, blank=True, default="")
    bench_session = models.CharField(max_length=40, blank=True, default="", db_index=True)
    old_sessions = models.JSONField(default=list, blank=True)
    bench_seq = models.PositiveIntegerField(default=0)
    bench_state = models.CharField(max_length=20, blank=True, default="")
    bench_detail = models.CharField(max_length=300, blank=True, default="")
    bench_progress = models.PositiveSmallIntegerField(null=True, blank=True)
    bench_updated_at = models.DateTimeField(null=True, blank=True)
    retry_requested_at = models.DateTimeField(null=True, blank=True)
    retry_sent_at = models.DateTimeField(null=True, blank=True)
    audio_deleted_at = models.DateTimeField("Аудіо видалено на RAG-ПК", null=True, blank=True)

    # ── запис ──
    capture_mode = models.CharField("Що записували", max_length=8, choices=CAPTURE_CHOICES, blank=True, default="")
    no_headset = models.BooleanField("Без гарнітури", default=False)
    started_at = models.DateTimeField("Початок запису", null=True, blank=True)
    ended_at = models.DateTimeField("Кінець запису", null=True, blank=True)
    duration_s = models.FloatField(null=True, blank=True)
    levels = models.JSONField(default=dict, blank=True)
    silent = models.JSONField(default=list, blank=True)
    marks = models.JSONField(default=list, blank=True)

    # ── результат ──
    transcript_at = models.DateTimeField("Розшифровку отримано", null=True, blank=True)
    transcript_chars = models.PositiveIntegerField(default=0)
    moments_at = models.DateTimeField("Ключові моменти отримано", null=True, blank=True)
    moments_error = models.CharField(max_length=300, blank=True, default="")
    approved_at = models.DateTimeField("Додано в базу знань", null=True, blank=True)
    transcript_deleted_at = models.DateTimeField("Розшифровку видалено", null=True, blank=True)
    notified_at = models.DateTimeField(null=True, blank=True)
    owner_inactive_since = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField("Створено", auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Нарада"
        verbose_name_plural = "🎙 Наради"
        ordering = ["-created_at"]

    def __str__(self):
        return f"🎙 Нарада #{self.pk}"

    @property
    def start(self):
        """Коли нарада почалась: початок запису, інакше коли її підготували."""
        return self.started_at or self.created_at

    @property
    def day(self):
        start = self.start
        return timezone.localtime(start).date() if start else timezone.localdate()

    @property
    def has_transcript(self) -> bool:
        return bool(self.transcript_at) and not self.transcript_deleted_at


class MeetingTranscript(models.Model):
    """Розшифровка наради (окремо від Meeting: великий JSON). segments — [{n, start, end, ch, text}], n — номер рядка
    з 1 (на нього посилаються ключові моменти); incoming — частини, що ще збираються {upload_id, parts, got, meta}."""
    meeting = models.OneToOneField(Meeting, on_delete=models.CASCADE, primary_key=True, related_name="transcript")
    segments = models.JSONField(default=list, blank=True)
    languages = models.JSONField(default=dict, blank=True)
    model = models.CharField(max_length=60, blank=True, default="")
    upload_id = models.CharField(max_length=64, blank=True, default="")
    parts = models.PositiveSmallIntegerField(default=0)
    incoming = models.JSONField(default=dict, blank=True)
    summary = models.TextField(blank=True, default="")
    moments_model = models.CharField(max_length=60, blank=True, default="")
    chars = models.PositiveIntegerField(default=0)
    at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Розшифровка наради"
        verbose_name_plural = "Розшифровки нарад"

    def __str__(self):
        return f"🎙 Розшифровка #{self.meeting_id}"


class MeetingItem(models.Model):
    """Ключовий момент наради з дослівною цитатою (або 📌 позначка під час запису) — чекає ✓ / ✕ того, хто записав.
    ✓ — 📌 момент / ⚠️ ризик / 📅 крок плану проєкту або ⏰ дедлайн у його календарі; ✕ — більше не пропонується
    (key однаковий для того самого виду й цитати, тож повторна передача його не повертає)."""
    DECISION, TASK, DEADLINE, RISK = "decision", "task", "deadline", "risk"
    QUESTION, PROMISE, INFO, MARK = "question", "promise", "info", "mark"
    KIND_CHOICES = [(DECISION, "🧭 Рішення"), (TASK, "✅ Завдання"), (DEADLINE, "⏰ Строк"), (RISK, "⚠️ Ризик"),
                    (QUESTION, "❓ Відкрите питання"), (PROMISE, "🤝 Обіцянка"), (INFO, "ℹ️ Важливий факт"),
                    (MARK, "📌 Позначка під час запису")]
    BENCH_KINDS = (DECISION, TASK, DEADLINE, RISK, QUESTION, PROMISE, INFO)
    PENDING, ACCEPTED, REJECTED = "pending", "accepted", "rejected"
    STATUS_CHOICES = [(PENDING, "Чекає ✓ / ✕"), (ACCEPTED, "Прийнято"), (REJECTED, "Не те")]
    MOMENT, RISK_TARGET, STEP, DEADLINE_TARGET = "moment", "risk", "step", "deadline"
    TARGET_CHOICES = [("", "—"), (MOMENT, "📌 Важливий момент"), (RISK_TARGET, "⚠️ Ризик"), (STEP, "📅 Крок плану"),
                      (DEADLINE_TARGET, "⏰ Дедлайн у календарі")]

    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="items")
    key = models.CharField(max_length=40)
    kind = models.CharField("Вид", max_length=10, choices=KIND_CHOICES, default=INFO)
    title = models.CharField("Що", max_length=300, blank=True, default="")
    quote = models.TextField("Цитата", blank=True, default="")
    start_s = models.FloatField(null=True, blank=True)
    end_s = models.FloatField(null=True, blank=True)
    ch = models.CharField(max_length=1, blank=True, default="")
    who = models.CharField("Хто", max_length=120, blank=True, default="")
    due_text = models.CharField("Строк, як сказали", max_length=120, blank=True, default="")
    segment = models.IntegerField(null=True, blank=True, help_text="Номер рядка розшифровки (n, з 1)")
    status = models.CharField("Стан", max_length=10, choices=STATUS_CHOICES, default=PENDING, db_index=True)
    target = models.CharField("Куди додано", max_length=10, choices=TARGET_CHOICES, blank=True, default="")
    highlight = models.ForeignKey("projects.ProjectHighlight", null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="+")
    risk = models.ForeignKey("projects.ProjectRisk", null=True, blank=True, on_delete=models.SET_NULL,
                             related_name="+")
    step = models.ForeignKey("projects.ProjectStep", null=True, blank=True, on_delete=models.SET_NULL,
                             related_name="+")
    event = models.ForeignKey("calendar_app.CalendarEvent", null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Ключовий момент наради"
        verbose_name_plural = "Ключові моменти нарад"
        ordering = ["meeting", "start_s", "pk"]
        constraints = [models.UniqueConstraint(fields=["meeting", "key"], name="unique_meeting_item_key")]

    def __str__(self):
        return f"🎙 Пункт #{self.pk}"


class MeetingTombstone(models.Model):
    """Слід видаленої наради (без змісту): RAG-ПК, який ще має її запис, отримує «видалити» (410 / sync), а не
    «невідома нарада». Пишеться в post_delete наради, живе щонайменше TOMBSTONE_DAYS (services.cleanup)."""
    meeting_id = models.PositiveBigIntegerField(db_index=True)
    uid = models.UUIDField(db_index=True)
    bench_session = models.CharField(max_length=40, blank=True, default="")
    old_sessions = models.JSONField(default=list, blank=True)
    reason = models.CharField(max_length=20, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        verbose_name = "Слід видаленої наради"
        verbose_name_plural = "Сліди видалених нарад"

    def __str__(self):
        return f"🎙 Нарада #{self.meeting_id} (видалено)"
