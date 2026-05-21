"""email_assistant/models.py — власний email клієнт Minerva."""
from django.db import models
from django.contrib.auth.models import User


class EmailAccount(models.Model):
    user = models.ForeignKey(
        User, on_delete=models.CASCADE,
        related_name='email_accounts',
        verbose_name='Юзер')
    display_name = models.CharField(
        max_length=200, blank=True,
        verbose_name="Ім'я відправника",
        help_text='Наприклад: Slavik Pryimak або Minerva Support')
    email_address = models.EmailField(verbose_name='Email адреса')
    is_primary = models.BooleanField(default=False, verbose_name='Основний акаунт')
    is_active  = models.BooleanField(default=True,  verbose_name='Активний')

    # IMAP
    imap_host        = models.CharField(max_length=200, default='imap.ionos.de', verbose_name='IMAP хост')
    imap_port        = models.PositiveIntegerField(default=993, verbose_name='IMAP порт')
    imap_use_ssl     = models.BooleanField(default=True, verbose_name='SSL')
    imap_username    = models.CharField(max_length=200, verbose_name='IMAP логін')
    imap_password    = models.CharField(max_length=500, verbose_name='IMAP пароль')
    imap_folder_inbox = models.CharField(max_length=200, default='INBOX', verbose_name='Папка вхідних')
    imap_folder_sent  = models.CharField(
        max_length=200, default='Gesendete Objekte',
        verbose_name='Папка надісланих',
        help_text='IONOS: Gesendete Objekte | Gmail: [Gmail]/Sent Mail')

    # SMTP
    smtp_host    = models.CharField(max_length=200, default='smtp.ionos.de', verbose_name='SMTP хост')
    smtp_port    = models.PositiveIntegerField(default=587, verbose_name='SMTP порт')
    smtp_use_tls = models.BooleanField(default=True,  verbose_name='TLS')
    smtp_use_ssl = models.BooleanField(default=False, verbose_name='SSL')
    smtp_username = models.CharField(max_length=200, verbose_name='SMTP логін')
    smtp_password = models.CharField(max_length=500, verbose_name='SMTP пароль')

    # Per-account signature (overrides UserProfile.smtp_signature if set)
    signature = models.TextField(
        blank=True, default='',
        verbose_name='Підпис листа',
        help_text="Підпис для цього акаунту. {name} = ваше ім'я. Якщо порожньо — береться з профілю.")
    signature_position = models.CharField(
        max_length=20, default='after_reply',
        choices=[
            ('after_reply', 'Після мого тексту (перед цитатою)'),
            ('end',         'В кінці (після цитати)'),
        ],
        verbose_name='Позиція підпису')

    # Sync state
    last_sync_at  = models.DateTimeField(null=True, blank=True, verbose_name='Остання синхронізація')
    sync_days_back = models.PositiveIntegerField(default=30, verbose_name='Синхронізувати за N днів')
    sync_limit = models.PositiveIntegerField(
        default=200, verbose_name='Ліміт листів на папку',
        help_text='Скільки листів максимум завантажувати на одну папку за раз.')
    sync_no_limit = models.BooleanField(
        default=False, verbose_name='Без ліміту',
        help_text='Якщо увімкнено — завантажує всі листи (ігнорує ліміт). Може бути повільно для великих папок.')
    sync_interval_minutes = models.PositiveIntegerField(
        default=2,
        verbose_name='Автооновлення кожні N хвилин',
        help_text='Як часто браузер перевіряє нові листи (мін. 1).')
    last_seen_uid  = models.PositiveIntegerField(default=0, verbose_name='Останній UID')
    created_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Email акаунт'
        verbose_name_plural = 'Email акаунти'
        ordering = ['-is_primary', 'email_address']

    def __str__(self):
        return f'{self.display_name or self.email_address} ({self.user.username})'

    @property
    def from_header(self):
        if self.display_name:
            return f'{self.display_name} <{self.email_address}>'
        return self.email_address

    def save(self, *args, **kwargs):
        if not self.pk:
            if not EmailAccount.objects.filter(user=self.user, is_primary=True).exists():
                self.is_primary = True
        super().save(*args, **kwargs)


class EmailThread(models.Model):
    account       = models.ForeignKey(EmailAccount, on_delete=models.CASCADE, related_name='threads')
    thread_id     = models.CharField(max_length=500, blank=True)
    subject       = models.CharField(max_length=500)
    participants  = models.JSONField(default=list)
    message_count = models.PositiveIntegerField(default=0)
    has_unread    = models.BooleanField(default=True)
    last_message_at = models.DateTimeField(null=True, blank=True)
    is_starred    = models.BooleanField(default=False)
    is_archived   = models.BooleanField(default=False)
    crm_customer  = models.ForeignKey(
        'crm.Customer', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='email_threads',
        verbose_name='CRM клієнт')

    class Meta:
        verbose_name = 'Гілка листування'
        verbose_name_plural = 'Гілки листування'
        ordering = ['-last_message_at']

    def __str__(self):
        return f'{self.subject[:50]} ({self.message_count} листів)'


class EmailMessage(models.Model):
    FOLDER_INBOX = 'inbox'
    FOLDER_SENT  = 'sent'
    FOLDER_DRAFT = 'draft'
    FOLDER_TRASH = 'trash'
    FOLDER_SPAM  = 'spam'
    FOLDER_CHOICES = [
        (FOLDER_INBOX, 'Вхідні'),
        (FOLDER_SENT,  'Надіслані'),
        (FOLDER_DRAFT, 'Чернетки'),
        (FOLDER_TRASH, 'Кошик'),
        (FOLDER_SPAM,  'Спам'),
    ]

    account     = models.ForeignKey(EmailAccount, on_delete=models.CASCADE, related_name='messages')
    thread      = models.ForeignKey(EmailThread, null=True, blank=True, on_delete=models.SET_NULL, related_name='messages')
    imap_uid    = models.PositiveIntegerField(default=0)
    message_id  = models.CharField(max_length=500, blank=True)
    in_reply_to = models.CharField(max_length=500, blank=True)
    folder      = models.CharField(max_length=20, choices=FOLDER_CHOICES, default=FOLDER_INBOX)
    subject     = models.CharField(max_length=500, blank=True)
    from_email  = models.CharField(max_length=500)
    from_name   = models.CharField(max_length=200, blank=True)
    to_emails   = models.JSONField(default=list)
    cc_emails   = models.JSONField(default=list)
    bcc_emails  = models.JSONField(default=list)
    body_text   = models.TextField(blank=True)
    body_html   = models.TextField(blank=True)
    imap_folder_name = models.CharField(max_length=200, blank=True, default='', db_index=True,
        verbose_name='Папка IMAP',
        help_text='Оригінальна назва папки на IMAP-сервері (напр. "Meine Order")')
    is_read     = models.BooleanField(default=False)
    is_starred  = models.BooleanField(default=False)
    is_deleted  = models.BooleanField(default=False)
    is_spam     = models.BooleanField(default=False, db_index=True, verbose_name='Спам')
    attachments = models.JSONField(default=list)
    ai_summary      = models.TextField(blank=True)
    ai_reply_draft  = models.TextField(blank=True)
    ai_translated   = models.TextField(blank=True)
    ai_translate_to = models.CharField(max_length=10, blank=True)
    sent_at    = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Email лист'
        verbose_name_plural = 'Email листи'
        ordering = ['-sent_at']
        unique_together = [('account', 'imap_uid', 'imap_folder_name')]

    def __str__(self):
        return f'{self.subject[:60]} ({self.from_email})'

    @property
    def sender_display(self):
        return self.from_name or self.from_email

    @property
    def has_attachments(self):
        return bool(self.attachments)


class EmailDraft(models.Model):
    account    = models.ForeignKey(EmailAccount, on_delete=models.CASCADE, related_name='drafts')
    reply_to   = models.ForeignKey(EmailMessage, null=True, blank=True, on_delete=models.SET_NULL, related_name='replies')
    subject    = models.CharField(max_length=500, blank=True)
    to_emails  = models.JSONField(default=list)
    cc_emails  = models.JSONField(default=list)
    bcc_emails = models.JSONField(default=list)
    body       = models.TextField(blank=True)
    attachments = models.JSONField(default=list)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Чернетка'
        ordering = ['-updated_at']


class EmailSettings(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='email_settings')
    ai_auto_suggest_reply = models.BooleanField(default=True, verbose_name='AI автоматично пропонує відповідь')
    ai_auto_translate     = models.BooleanField(default=False, verbose_name='AI автоматично перекладає листи')
    ai_translate_to       = models.CharField(
        max_length=10, default='uk',
        choices=[('uk', 'Українська'), ('de', 'Deutsch'), ('en', 'English')],
        verbose_name='Мова перекладу')
    deadline_detection = models.BooleanField(default=True, verbose_name='Визначати дедлайни в листах')
    sync_to_crm_timeline = models.BooleanField(
        default=True, verbose_name='Синхронізувати з CRM хронологією',
        help_text='Листи від/до CRM клієнтів додаються в CustomerTimeline')
    signature = models.TextField(blank=True, verbose_name='Підпис (застарілий — використовуй підпис акаунту)')
    auto_signature = models.BooleanField(default=True, verbose_name='Автоматично вставляти підпис')
    signature_position = models.CharField(
        max_length=20, default='after_reply',
        choices=[('after_reply', 'Після мого тексту'), ('end', 'В кінці')],
        verbose_name='Позиція підпису')
    mark_read_on_server = models.BooleanField(
        default=True, verbose_name='Помічати прочитані на IMAP-сервері')
    telegram_notify_new = models.BooleanField(default=True, verbose_name='Telegram: нові листи')
    telegram_quiet_from = models.TimeField(null=True, blank=True, verbose_name='Тихий режим з')
    telegram_quiet_to   = models.TimeField(null=True, blank=True, verbose_name='Тихий режим до')
    spam_folder = models.CharField(
        max_length=200, blank=True, default='Spam',
        verbose_name='Папка спаму на сервері',
        help_text='IONOS: Spam | Gmail: [Gmail]/Spam')
    show_admin_sidebar = models.BooleanField(
        default=True, verbose_name='Показувати панель навігації Minerva',
        help_text='Відображати ліве меню системи на сторінках Email асистента')

    # Auto-reply
    auto_reply_enabled = models.BooleanField(
        default=False, verbose_name='Автовідповідь',
        help_text='Minerva AI автоматично генерує відповідь на вхідні листи')
    auto_reply_mode = models.CharField(
        max_length=10, default='draft',
        choices=[('draft', 'Зберегти як чернетку'), ('send', 'Надіслати одразу')],
        verbose_name='Режим автовідповіді')
    auto_reply_prompt = models.TextField(
        blank=True, verbose_name='Інструкція для AI (автовідповідь)',
        help_text='Порожньо — стандартна інструкція генерації відповіді')

    # Order trigger
    order_trigger_enabled = models.BooleanField(
        default=False, verbose_name='Нове замовлення → чернетка листа',
        help_text='При кожному новому замовленні AI генерує чернетку листа клієнту')

    class Meta:
        verbose_name = 'Налаштування Email асистента'

    def __str__(self):
        return f'Email налаштування: {self.user.username}'

    @classmethod
    def get_for_user(cls, user):
        obj, _ = cls.objects.get_or_create(user=user)
        return obj


class ScheduledEmail(models.Model):
    """Email planned for future delivery."""

    STATUS_PENDING   = 'pending'
    STATUS_SENT      = 'sent'
    STATUS_FAILED    = 'failed'
    STATUS_CANCELLED = 'cancelled'

    STATUS_CHOICES = [
        (STATUS_PENDING,   '⏳ Очікує'),
        (STATUS_SENT,      '✓ Надіслано'),
        (STATUS_FAILED,    '✗ Помилка'),
        (STATUS_CANCELLED, '✗ Скасовано'),
    ]

    account      = models.ForeignKey(EmailAccount, on_delete=models.CASCADE,
                                     related_name='scheduled_emails',
                                     verbose_name='Акаунт')
    subject      = models.CharField(max_length=500, verbose_name='Тема')
    to_emails    = models.JSONField(default=list, verbose_name='Отримувачі')
    cc_emails    = models.JSONField(default=list, verbose_name='Копія')
    body         = models.TextField(blank=True, verbose_name='Текст')
    body_html    = models.TextField(blank=True, verbose_name='HTML')
    scheduled_at = models.DateTimeField(verbose_name='Час відправки', db_index=True)
    status       = models.CharField(max_length=20, choices=STATUS_CHOICES,
                                    default=STATUS_PENDING, db_index=True,
                                    verbose_name='Статус')
    sent_at      = models.DateTimeField(null=True, blank=True, verbose_name='Надіслано о')
    error_msg    = models.TextField(blank=True, verbose_name='Помилка')
    trigger      = models.CharField(max_length=50, default='manual', verbose_name='Тригер')
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name        = 'Запланований лист'
        verbose_name_plural = 'Заплановані листи'
        ordering            = ['scheduled_at']

    def __str__(self):
        recipients = ', '.join(self.to_emails[:2])
        return f'{self.subject[:50]} → {recipients} ({self.scheduled_at:%d.%m.Y %H:%M})'


class EmailRule(models.Model):
    """Автоматичне правило обробки вхідних листів."""

    FIELD_FROM_EMAIL = 'from_email'
    FIELD_FROM_NAME  = 'from_name'
    FIELD_SUBJECT    = 'subject'
    FIELD_BODY       = 'body'
    FIELD_TO_EMAIL   = 'to_email'

    FIELD_CHOICES = [
        (FIELD_FROM_EMAIL, 'Відправник (email)'),
        (FIELD_FROM_NAME,  "Відправник (ім'я)"),
        (FIELD_SUBJECT,    'Тема'),
        (FIELD_BODY,       'Текст листа'),
        (FIELD_TO_EMAIL,   'Кому (email)'),
    ]

    OP_CONTAINS     = 'contains'
    OP_EQUALS       = 'equals'
    OP_STARTS_WITH  = 'starts_with'
    OP_ENDS_WITH    = 'ends_with'
    OP_NOT_CONTAINS = 'not_contains'

    OP_CHOICES = [
        (OP_CONTAINS,     'Містить'),
        (OP_EQUALS,       'Рівно'),
        (OP_STARTS_WITH,  'Починається з'),
        (OP_ENDS_WITH,    'Закінчується на'),
        (OP_NOT_CONTAINS, 'Не містить'),
    ]

    ACTION_MARK_READ     = 'mark_read'
    ACTION_MARK_SPAM     = 'mark_spam'
    ACTION_MOVE          = 'move_folder'
    ACTION_STAR          = 'star'
    ACTION_TRASH         = 'trash'
    ACTION_ADD_CALENDAR  = 'add_to_calendar'

    ACTION_CHOICES = [
        (ACTION_MARK_READ,    'Позначити прочитаним'),
        (ACTION_MARK_SPAM,    'Позначити спамом'),
        (ACTION_MOVE,         'Перемістити до папки'),
        (ACTION_STAR,         'Позначити зірочкою'),
        (ACTION_TRASH,        'Видалити'),
        (ACTION_ADD_CALENDAR, 'Додати в календар (email follow-up)'),
    ]

    account         = models.ForeignKey(EmailAccount, on_delete=models.CASCADE, related_name='rules')
    name            = models.CharField(max_length=200, verbose_name='Назва правила')
    condition_field = models.CharField(max_length=20, choices=FIELD_CHOICES, default=FIELD_FROM_EMAIL, verbose_name='Поле')
    condition_op    = models.CharField(max_length=20, choices=OP_CHOICES,    default=OP_CONTAINS,     verbose_name='Умова')
    condition_value = models.CharField(max_length=500, verbose_name='Значення')
    action          = models.CharField(max_length=20, choices=ACTION_CHOICES, default=ACTION_MARK_READ, verbose_name='Дія')
    action_value    = models.CharField(max_length=200, blank=True, verbose_name='Параметр дії',
                                       help_text='Для "move_folder" — назва папки IMAP')
    is_active       = models.BooleanField(default=True, verbose_name='Активне')
    created_at      = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name        = 'Правило пошти'
        verbose_name_plural = 'Правила пошти'
        ordering            = ['name']

    def __str__(self):
        return f'{self.name} ({self.get_action_display()})'

    def matches(self, msg: 'EmailMessage') -> bool:
        field_map = {
            self.FIELD_FROM_EMAIL: msg.from_email or '',
            self.FIELD_FROM_NAME:  msg.from_name  or '',
            self.FIELD_SUBJECT:    msg.subject     or '',
            self.FIELD_BODY:       msg.body_text   or '',
            self.FIELD_TO_EMAIL:   ', '.join(msg.to_emails or []),
        }
        val  = field_map.get(self.condition_field, '').lower()
        cond = self.condition_value.lower()
        if   self.condition_op == self.OP_CONTAINS:     return cond in val
        elif self.condition_op == self.OP_EQUALS:       return val == cond
        elif self.condition_op == self.OP_STARTS_WITH:  return val.startswith(cond)
        elif self.condition_op == self.OP_ENDS_WITH:    return val.endswith(cond)
        elif self.condition_op == self.OP_NOT_CONTAINS: return cond not in val
        return False

    def apply_to(self, msg: 'EmailMessage') -> bool:
        """Apply action to msg. Returns True if changed."""
        changed = []
        if self.action == self.ACTION_MARK_READ:
            if not msg.is_read:
                msg.is_read = True; changed.append('is_read')
        elif self.action == self.ACTION_MARK_SPAM:
            msg.is_spam = True; msg.folder = EmailMessage.FOLDER_SPAM
            changed += ['is_spam', 'folder']
        elif self.action == self.ACTION_MOVE:
            if self.action_value:
                msg.imap_folder_name = self.action_value; changed.append('imap_folder_name')
        elif self.action == self.ACTION_STAR:
            if not msg.is_starred:
                msg.is_starred = True; changed.append('is_starred')
        elif self.action == self.ACTION_TRASH:
            msg.folder = EmailMessage.FOLDER_TRASH; changed.append('folder')
        elif self.action == self.ACTION_ADD_CALENDAR:
            try:
                from calendar_app.models import CalendarEvent
                from django.utils import timezone as _tz
                from datetime import timedelta
                CalendarEvent.objects.get_or_create(
                    email_message=msg,
                    defaults={
                        'user':                  msg.account.user,
                        'title':                 (msg.subject or 'Лист-нагадування')[:300],
                        'event_type':            CalendarEvent.TYPE_EMAIL,
                        'start_at':              _tz.now() + timedelta(days=1),
                        'remind_minutes_before': 60,
                    },
                )
            except Exception:
                pass
        if changed:
            msg.save(update_fields=changed)
        return bool(changed)


class EmailContact(models.Model):
    """Address book: auto-populated from sent/scheduled messages."""
    user       = models.ForeignKey(User, on_delete=models.CASCADE,
                                   related_name='email_contacts')
    email      = models.EmailField()
    name       = models.CharField(max_length=200, blank=True)
    use_count  = models.PositiveIntegerField(default=1)
    last_used_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints         = [
            models.UniqueConstraint(fields=['user', 'email'],
                                    name='unique_user_email_contact'),
        ]
        ordering            = ['-use_count', '-last_used_at']
        verbose_name        = 'Контакт адресної книги'
        verbose_name_plural = 'Адресна книга'

    def __str__(self):
        return f'{self.name} <{self.email}>' if self.name else self.email
