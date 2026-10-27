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
