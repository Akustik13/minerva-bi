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

    class Meta:
        verbose_name = 'Налаштування Email асистента'

    def __str__(self):
        return f'Email налаштування: {self.user.username}'

    @classmethod
    def get_for_user(cls, user):
        obj, _ = cls.objects.get_or_create(user=user)
        return obj
