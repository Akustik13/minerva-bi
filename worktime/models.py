"""Робочий час в askDANTE: головний облік — askDANTE, Minerva — пульт (календар, заповнення пропусків, проєкти).

Записи часу через API askDANTE не виправляються й не видаляються, тож Minerva перед кожним записом
перечитує день в askDANTE і пише лише туди, де порожньо. Логіка — services.py, клієнт — client.py.
"""
import datetime

from django.conf import settings
from django.db import models


class DanteSettings(models.Model):
    """Підключення до askDANTE і правила заповнення — синглтон (pk=1)."""
    MODE_OFF, MODE_FIXED, MODE_ACTIVITY = "off", "fixed", "activity"
    MODES = [
        (MODE_OFF, "Вимкнено"),
        (MODE_FIXED, "Стандартний робочий день"),
        (MODE_ACTIVITY, "За активністю в Minerva (перша й остання дія дня)"),
    ]

    enabled = models.BooleanField("Увімкнено", default=False)
    base_url = models.URLField("Адреса askDANTE", default="https://my.askdante.com",
                               help_text="Без /rest/api у кінці.")
    api_key = models.CharField("API-Key askDANTE", max_length=200, blank=True, default="",
                               help_text="askDANTE → Einstellungen → Schnittstellen → Rest API.")
    key_in_query = models.BooleanField(default=False, editable=False,
                                       help_text="askDANTE прийняв ключ лише параметром адреси (не заголовком).")
    timeout = models.PositiveSmallIntegerField("Тайм-аут запиту, с", default=20)

    default_start = models.TimeField("Прихід за замовчуванням", default=datetime.time(8, 0))
    default_stop = models.TimeField("Відхід за замовчуванням", default=datetime.time(16, 30))
    workdays = models.CharField("Робочі дні", max_length=7, default="12345",
                                help_text="Номери днів тижня: 1 — понеділок … 7 — неділя. "
                                          "Свята й відпустки askDANTE враховуються окремо.")

    autofill_mode = models.CharField("Автозаповнення", max_length=10, choices=MODES, default=MODE_OFF,
                                     help_text="Щодня заповнює порожні робочі дні працівників, яким увімкнено "
                                               "автозаповнення. День, де вже є записи, не чіпається.")
    autofill_since = models.DateField("Не раніше дати", null=True, blank=True,
                                      help_text="Автозаповнення не йде далі в минуле, ніж ця дата.")
    autofill_lookback_days = models.PositiveSmallIntegerField("Перевіряти днів назад", default=14)
    autofill_hour = models.PositiveSmallIntegerField("Запуск о годині", default=7,
                                                     help_text="Раз на добу, після цієї години (0–23).")
    activity_fallback = models.BooleanField(
        "Без активності — стандартний день", default=False,
        help_text="У режимі «за активністю»: якщо в Minerva за день немає дій — ставити стандартний час. "
                  "Вимкнено — такий день пропускається.")
    round_minutes = models.PositiveSmallIntegerField("Округлення активності, хв", default=5)
    min_activity_minutes = models.PositiveSmallIntegerField(
        "Мінімум активності, хв", default=30,
        help_text="Коротша активність за день не вважається робочим днем.")

    last_check_at = models.DateTimeField("Остання перевірка", null=True, blank=True)
    last_check_result = models.CharField("Результат перевірки", max_length=255, blank=True, default="")
    last_sync_at = models.DateTimeField("Довідники оновлено", null=True, blank=True)
    last_autofill_at = models.DateTimeField("Останнє автозаповнення", null=True, blank=True)
    last_autofill_result = models.CharField("Результат автозаповнення", max_length=255, blank=True, default="")
    last_autofill_report = models.JSONField("Звіт автозаповнення", default=dict, blank=True, editable=False,
                                            help_text="Останній запуск (щоденний або «Заповнити зараз») по днях.")
    default_activity = models.ForeignKey(
        "DanteActivity", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        verbose_name="Вид роботи за замовчуванням",
        help_text="Для часу на проєкти, якщо в askDANTE увімкнено види робіт (Tätigkeiten).")
    records_days = models.PositiveSmallIntegerField(
        "Години на проєктах — днів назад", default=90,
        help_text="Скільки днів записів часу askDANTE Minerva тримає в себе (оновлення — щоночі й кнопкою).")
    records_synced_at = models.DateTimeField("Записи часу оновлено", null=True, blank=True)

    mail_time = models.BooleanField(
        "Рахувати час на пошту", default=False,
        help_text="Листи своїх категорій (DigiKey, магазин…) і проєктів Minerva → оцінка часу по днях → "
                  "запис у їхній проєкт askDANTE (після ✓ або автоматично, як задано в категорії).")
    mail_projects = models.BooleanField(
        "Листи проєктів Minerva — на їхній проєкт askDANTE", default=True,
        help_text="Гілка в проєкті Minerva, який зв'язано з askDANTE, іде туди раніше за свою категорію.")
    mail_projects_auto = models.BooleanField("Проєкти — записувати автоматично", default=False)
    mail_reply_minutes = models.PositiveSmallIntegerField("Хв на нашу відповідь (проєкти)", default=10)
    mail_read_minutes = models.PositiveSmallIntegerField("Хв на отриманий лист (проєкти)", default=3)
    mail_step = models.PositiveSmallIntegerField(
        "Округлювати вгору до, хв", default=15,
        help_text="Оцінка за день по проєкту округлюється вгору (15 — чверть години).")
    mail_time_last_day = models.DateField("Час на пошту записано по", null=True, blank=True, editable=False)
    mail_time_result = models.CharField("Час на пошту — останній запуск", max_length=255, blank=True, default="",
                                        editable=False)

    class Meta:
        verbose_name = "Налаштування askDANTE"
        verbose_name_plural = "⚙️ Налаштування askDANTE"

    def __str__(self):
        return "Налаштування askDANTE"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        return cls.objects.get_or_create(pk=1)[0]

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def is_workday(self, day: datetime.date) -> bool:
        return str(day.isoweekday()) in (self.workdays or "")


class DanteUser(models.Model):
    """Працівник askDANTE (копія довідника) ↔ користувач Minerva."""
    dante_id = models.CharField("ID в askDANTE", max_length=40, unique=True)
    first_name = models.CharField("Ім'я", max_length=100, blank=True, default="")
    last_name = models.CharField("Прізвище", max_length=100, blank=True, default="")
    email = models.CharField("Логін (email)", max_length=254, blank=True, default="", db_index=True)
    personnel_number = models.CharField("Табельний номер", max_length=40, blank=True, default="")
    archived = models.BooleanField("В архіві askDANTE", default=False)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="dante", verbose_name="Користувач Minerva")
    autofill = models.BooleanField("Автозаповнення", default=False,
                                   help_text="Щодня заповнювати порожні робочі дні (режим — у налаштуваннях).")
    start = models.TimeField("Свій прихід", null=True, blank=True, help_text="Порожньо — як у налаштуваннях.")
    stop = models.TimeField("Свій відхід", null=True, blank=True, help_text="Порожньо — як у налаштуваннях.")
    open_since = models.DateTimeField("Прийшов о", null=True, blank=True,
                                      help_text="Відмічено «Прийшов» у Minerva, «Пішов» ще ні.")
    busy_until = models.DateTimeField(null=True, blank=True, editable=False,
                                      help_text="Іде запис в askDANTE (захист від подвійного натискання).")
    synced_at = models.DateTimeField("Оновлено з askDANTE", null=True, blank=True)
    report_email = models.CharField("Email для звіту", max_length=254, blank=True, default="",
                                    help_text="Заповнено — працівник отримує свою частину звіту поштою "
                                              "(якщо в звіті увімкнено «Кожному працівнику»).")

    class Meta:
        verbose_name = "Працівник askDANTE"
        verbose_name_plural = "👥 Працівники askDANTE"
        ordering = ["archived", "last_name", "first_name"]

    def __str__(self):
        return " ".join(p for p in (self.first_name, self.last_name) if p) or self.email or f"#{self.dante_id}"

    def times(self, st: DanteSettings):
        return self.start or st.default_start, self.stop or st.default_stop


class WorkDay(models.Model):
    """День працівника, як його бачить askDANTE (кеш останнього читання): записи часу й відсутності."""
    BUSINESS, NON_BUSINESS, LEAVE, ILLNESS = "BUSINESS_DAY", "NONEBUSINESS_DAY", "LEAVE", "ILLNESS"

    dante_user = models.ForeignKey(DanteUser, on_delete=models.CASCADE, related_name="days")
    date = models.DateField("Дата", db_index=True)
    planned_state = models.CharField("План askDANTE", max_length=20, blank=True, default="",
                                     help_text="BUSINESS_DAY / NONEBUSINESS_DAY / LEAVE / ILLNESS; порожньо — невідомо.")
    absences = models.JSONField("Відсутності", default=list, blank=True)   # [{type, name, ratio, state}]
    times = models.JSONField("Записи", default=list, blank=True)           # [{coming, leaving, source, minutes}]
    minutes = models.PositiveIntegerField("Хвилин", default=0)
    target_minutes = models.PositiveIntegerField("Норма, хв", null=True, blank=True)
    fetched_at = models.DateTimeField("Прочитано з askDANTE", null=True, blank=True)

    class Meta:
        verbose_name = "День"
        verbose_name_plural = "📅 Календар робочого часу"
        unique_together = [("dante_user", "date")]
        ordering = ["-date"]

    def __str__(self):
        return f"{self.dante_user} · {self.date:%d.%m.%Y}"


class DanteProject(models.Model):
    """Проєкт askDANTE (копія довідника). Зв'язок з проєктами Minerva — ProjectMap."""
    dante_id = models.CharField("ID в askDANTE", max_length=40, unique=True)
    name = models.CharField("Назва", max_length=255)
    reference_number = models.CharField("Номер", max_length=100, blank=True, default="")
    customer_name = models.CharField("Клієнт askDANTE", max_length=255, blank=True, default="")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="children",
                               verbose_name="Батьківський проєкт")
    archived = models.BooleanField("В архіві", default=False)
    time_trackable = models.BooleanField("Можна записувати час", default=True)
    gone = models.BooleanField("Немає в askDANTE", default=False,
                               help_text="askDANTE більше не повертає цей проєкт.")
    synced_at = models.DateTimeField("Оновлено з askDANTE", null=True, blank=True)
    crm_customer = models.ForeignKey("crm.Customer", null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="dante_projects", verbose_name="Клієнт CRM",
                                     help_text="Години цього проєкту askDANTE рахуються клієнту в CRM.")
    pinned_by = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="dante_pins",
                                       verbose_name="☆ Мої проєкти",
                                       help_text="Кому показувати в швидкому записі часу й фільтрі «мої».")

    class Meta:
        verbose_name = "Проєкт askDANTE"
        verbose_name_plural = "📁 Проєкти askDANTE ↔ Minerva"
        ordering = ["archived", "name"]

    def __str__(self):
        return f"{self.name} ({self.reference_number})" if self.reference_number else self.name


class DanteActivity(models.Model):
    """Вид роботи askDANTE (Tätigkeit)."""
    dante_id = models.CharField("ID в askDANTE", max_length=40, unique=True)
    name = models.CharField("Назва", max_length=255)
    archived = models.BooleanField("В архіві", default=False)

    class Meta:
        verbose_name = "Вид роботи askDANTE"
        verbose_name_plural = "Види робіт askDANTE"
        ordering = ["archived", "name"]

    def __str__(self):
        return self.name


class ProjectMap(models.Model):
    """Проєкт Minerva → проєкт askDANTE. Один askDANTE-проєкт може приймати час кількох проєктів Minerva."""
    project = models.OneToOneField("projects.Project", on_delete=models.CASCADE, related_name="dante_map",
                                   verbose_name="Проєкт Minerva")
    dante_project = models.ForeignKey(DanteProject, on_delete=models.CASCADE, related_name="maps",
                                      verbose_name="Проєкт askDANTE")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Зв'язок проєктів"
        verbose_name_plural = "Зв'язки проєктів"

    def __str__(self):
        return f"{self.project} → {self.dante_project}"


class Booking(models.Model):
    """Журнал: що Minerva записала (або пробувала записати) в askDANTE."""
    WORK, PROJECT = "work", "project"
    KINDS = [(WORK, "Робочий час"), (PROJECT, "Час на проєкт")]
    MANUAL, FILL, AUTO, CLOCK, MEETING, MAIL = "manual", "fill", "auto", "clock", "meeting", "mail"
    SOURCES = [(MANUAL, "Вручну в календарі"), (FILL, "Заповнення пропусків"),
               (AUTO, "Автозаповнення"), (CLOCK, "Прийшов / Пішов"), (MEETING, "Зустріч з календаря"),
               (MAIL, "Час на пошту")]
    SENT, FAILED = "sent", "failed"
    STATUSES = [(SENT, "✅ Записано"), (FAILED, "❌ Помилка")]

    dante_user = models.ForeignKey(DanteUser, on_delete=models.CASCADE, related_name="bookings",
                                   verbose_name="Працівник")
    date = models.DateField("Дата", db_index=True)
    kind = models.CharField("Вид", max_length=10, choices=KINDS, default=WORK)
    source = models.CharField("Звідки", max_length=10, choices=SOURCES, default=MANUAL)
    start = models.TimeField("Прихід", null=True, blank=True)
    stop = models.TimeField("Відхід", null=True, blank=True)
    duration = models.PositiveIntegerField("Хвилин", null=True, blank=True)
    dante_project = models.ForeignKey(DanteProject, null=True, blank=True, on_delete=models.SET_NULL,
                                      verbose_name="Проєкт askDANTE")
    activity = models.ForeignKey(DanteActivity, null=True, blank=True, on_delete=models.SET_NULL,
                                 verbose_name="Вид роботи")
    comment = models.CharField("Коментар", max_length=500, blank=True, default="")
    reason = models.CharField("Підстава", max_length=255, blank=True, default="",
                              help_text="Звідки взято час: стандартний день, активність у Minerva…")
    status = models.CharField("Стан", max_length=10, choices=STATUSES, default=SENT)
    error = models.TextField("Помилка", blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+", verbose_name="Хто")
    created_at = models.DateTimeField("Коли", auto_now_add=True)

    class Meta:
        verbose_name = "Запис у askDANTE"
        verbose_name_plural = "📜 Журнал записів в askDANTE"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.dante_user} · {self.date:%d.%m.%Y} · {self.get_status_display()}"


class TimeRecord(models.Model):
    """Запис часу askDANTE (копія для звітів: години на проєктах і клієнтах). Головне — askDANTE; рядок, якого
    askDANTE більше не повертає за період, видаляється під час оновлення (services.sync_records)."""
    dante_id = models.CharField("ID в askDANTE", max_length=40, unique=True)
    dante_user = models.ForeignKey(DanteUser, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="records", verbose_name="Працівник")
    person_id = models.CharField("Працівник askDANTE", max_length=40, blank=True, default="")
    day = models.DateField("Дата", db_index=True)
    duration = models.PositiveIntegerField("Хвилин", default=0)
    start = models.CharField("Початок", max_length=5, blank=True, default="")
    stop = models.CharField("Кінець", max_length=5, blank=True, default="")
    dante_project = models.ForeignKey(DanteProject, null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name="records", verbose_name="Проєкт askDANTE")
    project_name = models.CharField("Проєкт (як в askDANTE)", max_length=255, blank=True, default="")
    activity_name = models.CharField("Вид роботи", max_length=255, blank=True, default="")
    description = models.TextField("Опис", blank=True, default="")
    hourly_rate = models.DecimalField("Ставка за годину", max_digits=10, decimal_places=2, null=True, blank=True)
    synced_at = models.DateTimeField("Оновлено", auto_now=True)

    class Meta:
        verbose_name = "Запис часу askDANTE"
        verbose_name_plural = "⏱ Записи часу askDANTE"
        ordering = ["-day", "start"]
        indexes = [models.Index(fields=["dante_project", "day"])]

    def __str__(self):
        return f"{self.day:%d.%m.%Y} · {self.project_name or 'без проєкту'} · {self.duration} хв"


class PeriodBalance(models.Model):
    """Місячний підсумок askDANTE працівника (performancePeriod): норма, факт, переробки, відпустка, лікарняні."""
    dante_user = models.ForeignKey(DanteUser, on_delete=models.CASCADE, related_name="balances")
    period = models.CharField("Місяць", max_length=7)   # 2026-10
    data = models.JSONField(default=dict, blank=True)
    error = models.CharField(max_length=255, blank=True, default="")
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Місячний підсумок askDANTE"
        verbose_name_plural = "Місячні підсумки askDANTE"
        unique_together = [("dante_user", "period")]


class MatchRule(models.Model):
    """Правило зв'язку: проєкт Minerva з таким доменом / словом / клієнтом CRM → цей проєкт askDANTE
    (підказка з балом 90 у worktime/matching.py; зв'язок усе одно ставить людина)."""
    DOMAIN, KEYWORD, CUSTOMER = "domain", "keyword", "customer"
    KINDS = [(DOMAIN, "Домен email учасників"), (KEYWORD, "Слово в назві, описі або темах листів"),
             (CUSTOMER, "Клієнт CRM")]
    kind = models.CharField("Ознака", max_length=10, choices=KINDS, default=KEYWORD)
    value = models.CharField("Значення", max_length=200, blank=True, default="",
                             help_text="Домен (acme.de) або слово; для клієнта CRM — порожньо.")
    customer = models.ForeignKey("crm.Customer", null=True, blank=True, on_delete=models.CASCADE, related_name="+",
                                 verbose_name="Клієнт CRM")
    dante_project = models.ForeignKey(DanteProject, on_delete=models.CASCADE, related_name="rules",
                                      verbose_name="Проєкт askDANTE")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Правило зв'язку проєктів"
        verbose_name_plural = "Правила зв'язку проєктів"
        ordering = ["kind", "value"]

    def __str__(self):
        what = self.customer if self.kind == self.CUSTOMER else self.value
        return f"{self.get_kind_display()} «{what}» → {self.dante_project}"


class WorkReport(models.Model):
    """Звіт по працівниках askDANTE поштою за розкладом: що заповнено, що ні, час на проєкти.
    Логіка — worktime/reports.py; запуск — cron `worktime_reports --if-due`."""
    WEEK, MONTH, DAYS = "week", "month", "days"
    PERIODS = [(WEEK, "Минулий тиждень (пн–нд)"), (MONTH, "Минулий місяць"), (DAYS, "Останні N днів до вчора")]
    DAILY, WEEKLY, MONTHLY = "daily", "weekly", "monthly"
    FREQS = [(DAILY, "Щодня"), (WEEKLY, "Щотижня"), (MONTHLY, "Щомісяця")]
    WEEKDAYS = [(0, "понеділок"), (1, "вівторок"), (2, "середа"), (3, "четвер"), (4, "п'ятниця"), (5, "субота"),
                (6, "неділя")]

    name = models.CharField("Назва", max_length=120, default="Робочий час команди")
    enabled = models.BooleanField("Надсилати", default=True)
    period = models.CharField("Період звіту", max_length=10, choices=PERIODS, default=WEEK)
    days = models.PositiveSmallIntegerField("N днів", default=7, help_text="Для «Останні N днів».")
    frequency = models.CharField("Як часто", max_length=10, choices=FREQS, default=WEEKLY)
    weekday = models.PositiveSmallIntegerField("День тижня", choices=WEEKDAYS, default=0)
    month_day = models.PositiveSmallIntegerField("День місяця", default=1, help_text="1–28.")
    hour = models.PositiveSmallIntegerField("О годині", default=8, help_text="0–23, після цієї години.")
    people = models.ManyToManyField(DanteUser, blank=True, related_name="reports", verbose_name="Працівники",
                                    help_text="Порожньо — усі, хто не в архіві askDANTE.")
    send_to = models.TextField("Кому (повний звіт)", blank=True, default="",
                               help_text="Адреси через кому або з нового рядка.")
    send_to_employees = models.BooleanField(
        "Кожному працівнику — його частину", default=False,
        help_text="Лише тим, у кого в «👥 Працівники askDANTE» заповнено «Email для звіту».")
    show_absence_types = models.BooleanField(
        "Показувати тип відсутності (лікарняний, відпустка)", default=False,
        help_text="Вимкнено — у повному звіті лише «відсутність» (DSGVO). Працівник свої бачить завжди.")
    sender = models.ForeignKey("email_assistant.EmailAccount", null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="+", verbose_name="З якої скриньки Minerva",
                               help_text="Порожньо — SMTP сповіщень (Налаштування → Сповіщення).")
    last_sent_at = models.DateTimeField("Останнє надсилання", null=True, blank=True)
    last_result = models.CharField("Результат", max_length=255, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")

    class Meta:
        verbose_name = "Звіт поштою"
        verbose_name_plural = "✉️ Звіти поштою"

    def __str__(self):
        return self.name


class MailCategoryTime(models.Model):
    """Своя категорія листів → проєкт askDANTE і норми часу. Оцінка й запис — worktime/mail_time.py."""
    category = models.OneToOneField("email_assistant.MailCategory", on_delete=models.CASCADE,
                                    related_name="dante", verbose_name="Категорія листів")
    dante_project = models.ForeignKey(DanteProject, null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name="mail_categories", verbose_name="Проєкт askDANTE",
                                      help_text="Порожньо — час на цю категорію не рахується.")
    activity = models.ForeignKey(DanteActivity, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
                                 verbose_name="Вид роботи", help_text="Порожньо — як у налаштуваннях askDANTE.")
    reply_minutes = models.PositiveSmallIntegerField("Хв на кожну нашу відповідь", default=10)
    read_minutes = models.PositiveSmallIntegerField("Хв на кожен отриманий і прочитаний лист", default=3)
    order_minutes = models.PositiveSmallIntegerField(
        "Хв на кожне замовлення цих джерел, яке ви змінили в Minerva", default=0,
        help_text="За журналом дій: статус, відправка, документи… 0 — не рахувати.")
    auto = models.BooleanField("Записувати в askDANTE автоматично", default=False,
                               help_text="Щодня за вчора, без підтвердження. Вимкнено — лише після ✓ на сторінці "
                                         "«✉️ Час на пошту».")

    class Meta:
        verbose_name = "Час на категорію листів"
        verbose_name_plural = "Час на категорії листів"

    def __str__(self):
        return f"{self.category} → {self.dante_project or '—'}"


class MailTimeEntry(models.Model):
    """Скільки часу на пошту по проєкту askDANTE за день уже записано (askDANTE не дозволяє виправити запис —
    наступного разу дописується лише різниця)."""
    dante_user = models.ForeignKey(DanteUser, on_delete=models.CASCADE, related_name="mail_time")
    day = models.DateField("День", db_index=True)
    dante_project = models.ForeignKey(DanteProject, on_delete=models.CASCADE, related_name="+")
    minutes_booked = models.PositiveIntegerField("Записано хв", default=0)
    details = models.JSONField(default=dict, blank=True)
    booking = models.ForeignKey(Booking, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Час на пошту за день"
        verbose_name_plural = "Час на пошту за день"
        unique_together = [("dante_user", "day", "dante_project")]
