from django.contrib.auth.models import User
from django.db import models


class CalendarCategory(models.Model):
    user  = models.ForeignKey(User, on_delete=models.CASCADE, related_name='cal_categories')
    name  = models.CharField(max_length=100, verbose_name='Назва')
    color = models.CharField(max_length=7, default='#607d8b', verbose_name='Колір (hex)')
    emoji = models.CharField(max_length=10, blank=True, default='📌', verbose_name='Іконка')

    class Meta:
        unique_together     = [('user', 'name')]
        verbose_name        = 'Категорія календаря'
        verbose_name_plural = 'Категорії календаря'
        ordering            = ['name']

    def __str__(self):
        return f'{self.emoji} {self.name}'


class CalendarEvent(models.Model):
    TYPE_DEADLINE = 'deadline'
    TYPE_MEETING  = 'meeting'
    TYPE_REMINDER = 'reminder'
    TYPE_EMAIL    = 'email_follow_up'
    TYPE_OTHER    = 'other'

    TYPE_CHOICES = [
        (TYPE_DEADLINE, '⏰ Дедлайн'),
        (TYPE_MEETING,  '🤝 Зустріч'),
        (TYPE_REMINDER, '🔔 Нагадування'),
        (TYPE_EMAIL,    '📧 Email follow-up'),
        (TYPE_OTHER,    '📌 Інше'),
    ]

    user        = models.ForeignKey(User, on_delete=models.CASCADE,
                                    related_name='calendar_events')
    title       = models.CharField(max_length=300, verbose_name='Назва')
    description = models.TextField(blank=True, verbose_name='Опис')
    event_type  = models.CharField(max_length=30, choices=TYPE_CHOICES,
                                   default=TYPE_OTHER, verbose_name='Тип')
    custom_category = models.ForeignKey(
        'CalendarCategory', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='events',
        verbose_name='Власна категорія')

    start_at = models.DateTimeField(verbose_name='Початок', db_index=True)
    end_at   = models.DateTimeField(null=True, blank=True, verbose_name='Кінець')
    all_day  = models.BooleanField(default=False, verbose_name='Весь день')

    crm_customer  = models.ForeignKey(
        'crm.Customer', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='calendar_events',
        verbose_name='CRM клієнт')
    email_message = models.ForeignKey(
        'email_assistant.EmailMessage', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='calendar_events',
        verbose_name='Лист',
        help_text='Лист з якого витягнуто дедлайн')
    sales_order   = models.ForeignKey(
        'sales.SalesOrder', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='calendar_events',
        verbose_name='Замовлення')

    remind_minutes_before = models.PositiveIntegerField(
        default=60, verbose_name='Нагадати за N хвилин')
    remind_sent = models.BooleanField(default=False)
    push_sent   = models.BooleanField(default=False)

    is_done    = models.BooleanField(default=False, verbose_name='Виконано', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name        = 'Подія календаря'
        verbose_name_plural = 'Календар'
        ordering            = ['start_at']

    def __str__(self):
        return f'{self.title} ({self.start_at:%d.%m.Y %H:%M})'


class CalendarSettings(models.Model):
    """Per-user notification preferences for calendar reminders."""
    user = models.OneToOneField(User, on_delete=models.CASCADE,
                                related_name='calendar_settings')

    notify_telegram = models.BooleanField(default=False, verbose_name='Telegram')
    notify_email    = models.BooleanField(default=True,  verbose_name='Email')
    notify_push     = models.BooleanField(default=True,  verbose_name='Push у браузері')

    default_remind_minutes = models.PositiveIntegerField(
        default=60, verbose_name='Нагадувати за N хвилин (за замовчуванням)')

    # Optional overrides — leave blank to use system NotificationSettings values
    email_to         = models.EmailField(blank=True, verbose_name='Email для сповіщень',
                                         help_text='Порожньо → використовується системний email')
    telegram_chat_id = models.CharField(max_length=50, blank=True,
                                        verbose_name='Telegram Chat ID',
                                        help_text='Порожньо → UserProfile.telegram_id або системний')

    invites_auto = models.BooleanField(
        default=False, verbose_name='Заносити запрошення з пошти без підтвердження',
        help_text='Вимкнено — запрошення (і зміни, скасування) чекають ✓ угорі календаря.')
    invites_since = models.DateField(null=True, blank=True, verbose_name='Шукати запрошення в листах від',
                                     help_text='Для пошуку в уже отриманій пошті.')
    invites_scan_status = models.CharField(max_length=255, blank=True, default='',
                                           verbose_name='Пошук запрошень — стан')
    deadlines_auto = models.BooleanField(
        default=False, verbose_name='Заносити знайдені терміни без підтвердження')
    deadline_mask = models.JSONField(default=dict, blank=True, verbose_name='Остання маска пошуку термінів')
    deadline_scan_status = models.CharField(max_length=255, blank=True, default='',
                                            verbose_name='Пошук термінів — стан')
    feed_token = models.CharField(max_length=64, blank=True, default='', editable=False,
                                  help_text='Секрет адреси ICS календаря Minerva; порожньо — адресу вимкнено.')
    feed_worktime = models.BooleanField(default=True, verbose_name='У підписці — відпустки й лікарняні askDANTE')
    feed_busy_only = models.BooleanField(default=False, verbose_name='У підписці лише «Зайнято» (без назв і описів)')

    class Meta:
        verbose_name        = 'Налаштування сповіщень календаря'
        verbose_name_plural = 'Налаштування сповіщень календаря'

    def __str__(self):
        return f'Налаштування — {self.user}'

    @classmethod
    def for_user(cls, user):
        obj, _ = cls.objects.get_or_create(user=user)
        return obj


class MeetingInvite(models.Model):
    """Запрошення на зустріч з пошти (iCalendar: Outlook / Teams / Google / Zoom). Один рядок на зустріч (UID):
    зміни й скасування оновлюють його. У календар — лише після ✓ (або «без підтвердження» в налаштуваннях);
    відхилене людиною не повертається. Логіка — calendar_app/invites.py."""
    NEW, UPDATE, CANCEL = 'new', 'update', 'cancel'          # чекають рішення
    ADDED, DISMISSED, CANCELLED = 'added', 'dismissed', 'cancelled'
    STATUS_CHOICES = [
        (NEW, '📨 Нове'), (UPDATE, '♻ Змінено'), (CANCEL, '✖ Скасовано організатором'),
        (ADDED, '✅ У календарі'), (DISMISSED, '— Не заносити'), (CANCELLED, '✖ Знято з календаря'),
    ]
    PENDING = (NEW, UPDATE, CANCEL)

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='meeting_invites')
    uid = models.CharField(max_length=500, verbose_name='UID зустрічі')
    sequence = models.IntegerField(default=0)
    method = models.CharField(max_length=20, blank=True, default='')
    title = models.CharField(max_length=300, verbose_name='Назва')
    start_at = models.DateTimeField(verbose_name='Початок', db_index=True)
    end_at = models.DateTimeField(null=True, blank=True, verbose_name='Кінець')
    all_day = models.BooleanField(default=False)
    location = models.CharField(max_length=500, blank=True, default='', verbose_name='Місце')
    organizer = models.CharField(max_length=300, blank=True, default='', verbose_name='Організатор')
    join_url = models.CharField(max_length=1000, blank=True, default='', verbose_name='Посилання (Teams / Zoom)')
    description = models.TextField(blank=True, default='')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=NEW, db_index=True,
                              verbose_name='Стан')
    email_message = models.ForeignKey('email_assistant.EmailMessage', null=True, blank=True,
                                      on_delete=models.SET_NULL, related_name='meeting_invites', verbose_name='Лист')
    crm_customer = models.ForeignKey('crm.Customer', null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name='meeting_invites', verbose_name='CRM клієнт')
    event = models.ForeignKey(CalendarEvent, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name='invites', verbose_name='Подія календаря')
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Запрошення з пошти'
        verbose_name_plural = '📨 Запрошення з пошти'
        ordering = ['start_at']
        unique_together = [('user', 'uid')]

    def __str__(self):
        return f'{self.title} ({self.start_at:%d.%m.%Y %H:%M})'


class FoundDeadline(models.Model):
    """Термін, знайдений у листі (пошук в архіві пошти за маскою, calendar_app/deadlines.py). У календар — після ✓
    (або «без підтвердження»); «✕» не повертається. quote — дослівна цитата з листа (перевіряється)."""
    NEW, ADDED, DISMISSED = 'new', 'added', 'dismissed'
    STATUS_CHOICES = [(NEW, '🔎 Знайдено'), (ADDED, '✅ У календарі'), (DISMISSED, '— Не заносити')]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='found_deadlines')
    email_message = models.ForeignKey('email_assistant.EmailMessage', on_delete=models.CASCADE,
                                      related_name='found_deadlines', verbose_name='Лист')
    title = models.CharField(max_length=300, verbose_name='Що')
    due_at = models.DateTimeField(verbose_name='Термін', db_index=True)
    all_day = models.BooleanField(default=True, verbose_name='Без часу')
    quote = models.TextField(blank=True, default='', verbose_name='Цитата з листа')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=NEW, db_index=True)
    event = models.ForeignKey(CalendarEvent, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Термін з пошти'
        verbose_name_plural = '🔎 Терміни з пошти'
        ordering = ['due_at']
        unique_together = [('user', 'email_message', 'due_at', 'title')]

    def __str__(self):
        return f'{self.title} ({self.due_at:%d.%m.%Y})'


class DeadlineScanMark(models.Model):
    """Лист уже перевірено на терміни (щоб повторний пошук не витрачав AI на той самий лист)."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='+')
    email_message = models.ForeignKey('email_assistant.EmailMessage', on_delete=models.CASCADE, related_name='+')
    found = models.PositiveSmallIntegerField(default=0)
    at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('user', 'email_message')]


class CalendarFeed(models.Model):
    """Підписаний календар (секретна адреса iCal Google / опублікований ICS Outlook): лише читання, шар у календарі.
    Логіка — calendar_app/feeds.py; оновлення — cron `calendar_feeds --if-due` і кнопкою."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='calendar_feeds')
    name = models.CharField(max_length=100, verbose_name='Назва')
    url = models.CharField(max_length=1000, verbose_name='Адреса iCal / ICS',
                           help_text='Секретна адреса — як пароль: видно лише початок.')
    color = models.CharField(max_length=7, default='#8e24aa', verbose_name='Колір')
    enabled = models.BooleanField(default=True, verbose_name='Показувати')
    refresh_minutes = models.PositiveSmallIntegerField(default=30, verbose_name='Оновлювати кожні, хв')
    last_fetch = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=255, blank=True, default='')
    events_count = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = 'Підписаний календар'
        verbose_name_plural = 'Підписані календарі'
        ordering = ['name']

    def __str__(self):
        return self.name


class ExternalEvent(models.Model):
    """Подія підписаного календаря (копія для показу; повторювані — розгорнуті в межах вікна)."""
    feed = models.ForeignKey(CalendarFeed, on_delete=models.CASCADE, related_name='events')
    uid = models.CharField(max_length=500)
    title = models.CharField(max_length=300)
    start_at = models.DateTimeField(db_index=True)
    end_at = models.DateTimeField(null=True, blank=True)
    all_day = models.BooleanField(default=False)
    location = models.CharField(max_length=500, blank=True, default='')

    class Meta:
        ordering = ['start_at']


class CalendarSyncSettings(models.Model):
    """Ключі OAuth-застосунків Google і Microsoft (вводить адміністратор; браузер їх не бачить). Синглтон."""
    google_client_id = models.CharField(max_length=300, blank=True, default='', verbose_name='Google Client ID')
    google_client_secret = models.CharField(max_length=300, blank=True, default='', verbose_name='Google Client Secret')
    ms_client_id = models.CharField(max_length=300, blank=True, default='',
                                    verbose_name='Microsoft Application (client) ID')
    ms_client_secret = models.CharField(max_length=300, blank=True, default='', verbose_name='Microsoft Client Secret')
    ms_tenant = models.CharField(max_length=100, default='common', verbose_name='Microsoft tenant',
                                 help_text='common — і особисті (outlook.com), і робочі акаунти Microsoft 365.')
    interval_minutes = models.PositiveSmallIntegerField(default=10, verbose_name='Синхронізувати кожні, хв')

    class Meta:
        verbose_name = 'Ключі Google / Microsoft'
        verbose_name_plural = 'Ключі Google / Microsoft'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        return cls.objects.get_or_create(pk=1)[0]


class CalendarAccount(models.Model):
    """Підключений акаунт Google або Microsoft (OAuth, токени лише на сервері)."""
    GOOGLE, MICROSOFT = 'google', 'microsoft'
    PROVIDERS = [(GOOGLE, 'Google Calendar'), (MICROSOFT, 'Outlook / Microsoft 365')]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='calendar_accounts')
    provider = models.CharField(max_length=10, choices=PROVIDERS)
    email = models.CharField(max_length=254, blank=True, default='')
    access_token = models.TextField(blank=True, default='', editable=False)
    refresh_token = models.TextField(blank=True, default='', editable=False)
    expires_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_error = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Підключений календар'
        verbose_name_plural = 'Підключені календарі'
        unique_together = [('user', 'provider', 'email')]

    def __str__(self):
        return f'{self.get_provider_display()} · {self.email}'


class RemoteCalendar(models.Model):
    account = models.ForeignKey(CalendarAccount, on_delete=models.CASCADE, related_name='calendars')
    remote_id = models.CharField(max_length=500)
    name = models.CharField(max_length=200)
    color = models.CharField(max_length=7, blank=True, default='')
    can_write = models.BooleanField(default=True)
    primary = models.BooleanField(default=False)

    class Meta:
        unique_together = [('account', 'remote_id')]
        ordering = ['-primary', 'name']

    def __str__(self):
        return f'{self.account.get_provider_display()}: {self.name}'


class SyncRule(models.Model):
    """Правило «звідки → куди». Двосторонньо — два правила; копії ніколи не копіюються назад (SyncLink)."""
    ALL, MEETINGS, MAIL = 'all', 'meetings', 'mail'
    WHAT = [(ALL, 'Усі події'), (MEETINGS, 'Лише зустрічі (з учасниками або посиланням Teams / Zoom)'),
            (MAIL, 'Лише з пошти (у Minerva: запрошення й терміни з листів)')]
    FULL, BUSY, PREFIX = 'full', 'busy', 'prefix'
    MODES = [(FULL, 'Повністю (назва, час, місце, опис)'), (BUSY, 'Лише «Зайнято» (без деталей)'),
             (PREFIX, 'Повністю, з позначкою джерела в назві')]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sync_rules')
    enabled = models.BooleanField(default=True, verbose_name='Увімкнено')
    source = models.ForeignKey(RemoteCalendar, null=True, blank=True, on_delete=models.CASCADE, related_name='+',
                               verbose_name='Звідки (порожньо — календар Minerva)')
    target = models.ForeignKey(RemoteCalendar, null=True, blank=True, on_delete=models.CASCADE, related_name='+',
                               verbose_name='Куди (порожньо — календар Minerva)')
    what = models.CharField(max_length=10, choices=WHAT, default=ALL, verbose_name='Що')
    keyword = models.CharField(max_length=100, blank=True, default='', verbose_name='Лише зі словом у назві')
    mode = models.CharField(max_length=10, choices=MODES, default=FULL, verbose_name='Як')
    days_back = models.PositiveSmallIntegerField(default=7, verbose_name='Днів назад')
    days_ahead = models.PositiveSmallIntegerField(default=90, verbose_name='Днів уперед')
    propagate_delete = models.BooleanField(default=True, verbose_name='Видаляти копію, коли оригінал видалено')
    last_run = models.DateTimeField(null=True, blank=True)
    last_result = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        verbose_name = 'Правило синхронізації'
        verbose_name_plural = 'Правила синхронізації'

    def side(self, cal):
        return str(cal) if cal else 'Minerva'

    def __str__(self):
        return f'{self.side(self.source)} → {self.side(self.target)}'


class SyncLink(models.Model):
    """Оригінал → копія (правило). Захист від петель і дублів: копія ніколи не стає джерелом."""
    rule = models.ForeignKey(SyncRule, on_delete=models.CASCADE, related_name='links')
    source_key = models.CharField(max_length=600)
    target_key = models.CharField(max_length=600, db_index=True)
    fingerprint = models.CharField(max_length=64, blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('rule', 'source_key')]
