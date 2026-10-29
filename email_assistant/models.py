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
    knowledge_excluded = models.BooleanField(
        default=False, verbose_name='Не індексувати в базі знань',
        help_text='Приватна переписка: не передавати в RAG (rag_assistant/knowledge.py)')
    knowledge_forced = models.BooleanField(
        default=False, verbose_name='Примусово в базу знань',
        help_text='«🧠 Передати в базу знань»: гілка йде в RAG, навіть якщо категорія чи дата листів поза правилами '
                  '(скринька має бути вибрана для бази знань)')
    crm_customer  = models.ForeignKey(
        'crm.Customer', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='email_threads',
        verbose_name='CRM клієнт')
    work_category = models.ForeignKey(
        'MailCategory', null=True, blank=True, on_delete=models.SET_NULL, related_name='threads',
        verbose_name='Своя категорія гілки',
        help_text='Вибрано вручну для всієї гілки — має пріоритет над правилами категорій')

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
    CATEGORY_CHOICES = [
        ('customer',   '👤 Клієнти'),
        ('supplier',   '🏭 Постачальники'),
        ('service',    '📦 Сервіси та доставка'),
        ('newsletter', '📰 Розсилки'),
        ('internal',   '🏢 Внутрішні'),
        ('other',      '❓ Інше'),
    ]
    category = models.CharField(
        max_length=20, blank=True, default='', db_index=True, choices=CATEGORY_CHOICES,
        verbose_name='Категорія',
        help_text='Визначається автоматично (email_assistant/categorize.py); порожньо — ще не визначено')
    work_category = models.ForeignKey(
        'MailCategory', null=True, blank=True, on_delete=models.SET_NULL, related_name='messages',
        verbose_name='Своя категорія',
        help_text='DigiKey, Інтернет-магазин… — за правилами категорії (email_assistant/work_categories.py)')
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

    CATEGORY_SHORT = {
        'customer':   '👤 Клієнт',
        'supplier':   '🏭 Постачальник',
        'service':    '📦 Сервіс',
        'newsletter': '📰 Розсилка',
        'internal':   '🏢 Внутрішній',
        'other':      '❓ Інше',
    }

    @property
    def has_attachments(self):
        return bool(self.attachments)

    @property
    def category_short(self):
        """Коротка мітка категорії для рядка в списку листів."""
        return self.CATEGORY_SHORT.get(self.category, '')

    def fix_html_body(self, save=True) -> bool:
        """Листи лише з text/html раніше зберігались як текст — перенести розмітку в body_html."""
        from email_assistant.imap_client import html_to_text, looks_like_html
        if self.body_html or not looks_like_html(self.body_text):
            return False
        self.body_html = self.body_text
        self.body_text = html_to_text(self.body_html)
        if save and self.pk:
            self.save(update_fields=['body_html', 'body_text'])
        return True


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
    AI_MODEL_AUTO = 'auto'
    AI_MODEL_CHOICES = [
        (AI_MODEL_AUTO,       'Автоматично (як Minerva AI: Haiku 4.5, для аналітики — Sonnet 4.6)'),
        ('claude-opus-5-5',   'Claude Opus 5.5 — найкраща якість · $4 / $20 за 1 млн токенів'),
        ('claude-sonnet-5-5', 'Claude Sonnet 5.5 — швидко й якісно · $2 / $10'),
        ('claude-haiku-5-5',  'Claude Haiku 5.5 — найдешевша й найшвидша · $0.10 / $0.50'),
        ('claude-fable-5-1',  'Claude Fable 5.1 — найпотужніша, найдорожча · $10 / $50'),
    ]
    ai_model = models.CharField(
        max_length=40, default=AI_MODEL_AUTO, choices=AI_MODEL_CHOICES,
        verbose_name='AI модель пошти',
        help_text='Для відповідей, перекладу, історії, граматики й нових листів. '
                  'Фонове визначення дедлайнів у кожному листі завжди «Автоматично» (дешево).')
    RAG_LOCAL, RAG_AUTO, RAG_EXTENDED = 'local', 'auto', 'extended'
    RAG_MODE_CHOICES = [
        (RAG_LOCAL,    'Лише Minerva — без звернень до RAG'),
        (RAG_AUTO,     'Автоматично — RAG, коли потрібна історія інших листувань'),
        (RAG_EXTENDED, 'Розширений — завжди шукати в базі знань RAG'),
    ]
    rag_mode = models.CharField(
        max_length=10, default=RAG_AUTO, choices=RAG_MODE_CHOICES,
        verbose_name='Пошук у базі знань (RAG)',
        help_text='Для «📜 Історії» й AI-відповіді. Переклад і граматика RAG ніколи не використовують.')
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
    sender_logos = models.BooleanField(
        default=True, verbose_name='Логотипи відправників',
        help_text='Значок сайту домену відправника замість букви в кружку (email_assistant/logos.py)')
    nav_icons = models.BooleanField(
        default=True, verbose_name='Значки категорій і проєктів',
        help_text='Емодзі перед назвою своїх категорій і проєктів у лівій колонці й мітках листів')
    nav_project_codes = models.BooleanField(
        default=True, verbose_name='Номер проєкту перед назвою',
        help_text='Ліва колонка скриньки «📁 Проєкти»: «P-2026-002 · Назва» або лише назва')
    hidden_work_categories = models.JSONField(
        default=list, blank=True, verbose_name='Сховані свої категорії',
        help_text='pk своїх категорій, яких не показувати в лівій колонці скриньки (правий клік → «Сховати»)')
    # ⭐ обране й порядок лівої колонки (email_assistant/nav_prefs.py)
    nav_pinned = models.JSONField(
        default=list, blank=True, verbose_name='⭐ Обране в лівій колонці',
        help_text='Ключі (wc:<pk>, kind:<тип>, quote, nocat, sort) у порядку закріплення — завжди зверху')
    nav_usage = models.JSONField(default=dict, blank=True, verbose_name='Вживання пунктів лівої колонки',
                                 help_text='{ключ: [бал, дата]} — кліки, «старіють» вдвічі за 30 днів')
    nav_sort = models.CharField(
        max_length=8, default='usage', verbose_name='Порядок лівої колонки',
        choices=[('usage', '🔥 За вживанням — часті вгорі'), ('count', '🔢 За кількістю листів'),
                 ('name', '🔤 За назвою'), ('order', '⚙ Як у налаштуваннях категорій')])
    ctx_menu = models.JSONField(
        default=dict, blank=True, verbose_name='Меню правого кліку листа',
        help_text='{"list": [...], "letter": [...]} — пункти, сховані в «⋯ Ще» (у списку й у відкритому листі); '
                  'порожньо — як за замовчуванням')

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
                from calendar_app.blocks import is_blocked
                if is_blocked(msg.account.user, msg):
                    return False
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


class EmailCategoryRule(models.Model):
    """
    Запам'ятована категорія контакту: правий клік по листу → «Віднести до категорії».
    key — адреса співрозмовника (a@b.com) або весь домен (@b.com); має пріоритет над автовизначенням.
    """
    account    = models.ForeignKey(EmailAccount, on_delete=models.CASCADE,
                                   related_name='category_rules', verbose_name='Акаунт')
    key        = models.CharField(max_length=254, verbose_name='Адреса або @домен')
    category   = models.CharField(max_length=20, choices=EmailMessage.CATEGORY_CHOICES,
                                  verbose_name='Категорія')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name        = 'Категорія контакту'
        verbose_name_plural = 'Категорії контактів'
        ordering            = ['key']
        constraints         = [
            models.UniqueConstraint(fields=['account', 'key'], name='unique_account_category_key'),
        ]

    def __str__(self):
        return f'{self.key} → {self.get_category_display()}'


class MailCategory(models.Model):
    """
    Своя категорія листів (DigiKey, Інтернет-магазин…) — друга ознака поруч із типом співрозмовника
    (клієнт / постачальник / сервіс). Спільна для всіх скриньок; лист потрапляє в першу за порядком,
    правила якої підходять. Логіка — email_assistant/work_categories.py; час на неї в askDANTE —
    worktime (MailCategoryTime).
    """
    name   = models.CharField('Назва', max_length=80)
    icon   = models.CharField('Значок', max_length=8, default='🏷', blank=True)
    color  = models.CharField('Колір', max_length=9, default='#1a73e8',
                              help_text='HEX, напр. #e53935')
    order  = models.PositiveSmallIntegerField('Порядок', default=0,
                                              help_text='Лист, що підходить до кількох, іде в першу.')
    is_active = models.BooleanField('Увімкнено', default=True)
    senders = models.TextField(
        'Відправники', blank=True, default='',
        help_text='По одному в рядку: адреса (sales@digikey.com), @домен (@digikey.com — разом із '
                  'піддоменами) або частина домену без @ (digikey → digikey.com, digikey.de…). '
                  'Діє і на наші відповіді цим адресам.')
    subject_words = models.TextField(
        'Слова в темі', blank=True, default='',
        help_text='По одному в рядку, без різниці великі чи малі літери (Marketplace, Bestellung).')
    sources = models.ManyToManyField(
        'sales.SalesSource', blank=True, related_name='+', verbose_name='Замовлення з джерел',
        help_text='Лист, у якому є номер або трек-номер замовлення з цих джерел.')
    by_customer_orders = models.BooleanField(
        'І листи покупців, які замовляли через ці джерела', default=False,
        help_text='Навіть без номера замовлення в листі. Не вмикайте для джерела, через яке замовляє майже кожен.')
    accounts = models.ManyToManyField(
        EmailAccount, blank=True, related_name='+', verbose_name='Усі листи скриньок',
        help_text='Напр. скринька магазину shop@… — усе, що в неї приходить і з неї йде.')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name        = 'Своя категорія листів'
        verbose_name_plural = '🏷 Свої категорії листів'
        ordering            = ['order', 'name']

    def __str__(self):
        return f'{self.icon} {self.name}'.strip()

    @property
    def label(self):
        return str(self)

    def lines(self, field: str) -> list:
        return [ln.strip().lower() for ln in (getattr(self, field) or '').splitlines() if ln.strip()]


class ReplyBrief(models.Model):
    """
    «📋 Підготувати відповідь»: бриф (з чого писати) і лист за ним — email_assistant/reply_brief.py.
    Готується у фоні: status / stages читає браузер; «Переписати» — лише крок листа з виправленим брифом.
    Останній бриф користувача для гілки показується під чернеткою; застарів — якщо в гілці з'явились нові листи.
    Ті самі зібрані дані — й для «❓ Запитати про лист» (answer: відповідь у полі brief) і «🧭 Що обіцяли / зробили»
    (review: перевірений JSON у полі brief).
    """
    RUNNING, DONE, ERROR = 'running', 'done', 'error'
    MODE_DATA, MODE_BRIEF, MODE_LETTER = 'data', 'brief', 'letter'
    MODE_ANSWER, MODE_REVIEW = 'answer', 'review'
    MODE_CHOICES = [(MODE_DATA, 'Лише дані'), (MODE_BRIEF, 'Дані + бриф'), (MODE_LETTER, 'Дані + бриф + лист'),
                    (MODE_ANSWER, 'Запитання про лист'), (MODE_REVIEW, 'Що обіцяли / зробили')]

    user          = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reply_briefs')
    mode          = models.CharField(max_length=10, choices=MODE_CHOICES, default=MODE_LETTER,
                                     help_text='До якого кроку готувати; далі — кнопками «Скласти бриф» / «Написати лист»')
    target        = models.TextField(blank=True, default='', help_text='Лист, на який відповідаємо (мітка [#N] для брифу)')
    message       = models.ForeignKey(EmailMessage, on_delete=models.CASCADE, related_name='+',
                                      verbose_name='Відповідь на лист')
    thread        = models.ForeignKey(EmailThread, null=True, blank=True, on_delete=models.CASCADE, related_name='+')
    wish          = models.TextField('Побажання', blank=True, default='')
    brief         = models.TextField('Бриф', blank=True, default='')
    sources       = models.JSONField(default=list, blank=True,
                                     help_text='[{n, kind, label, url, text}] — зібрані дані; бриф посилається на них як [#N]')
    draft         = models.TextField('Чернетка листа', blank=True, default='')
    recipient     = models.CharField(max_length=300, blank=True, default='', help_text='Кому пишемо (для AI)')
    to_email      = models.CharField(max_length=254, blank=True, default='')
    cc            = models.JSONField(default=list, blank=True)
    used          = models.JSONField(default=list, blank=True, help_text='«Minerva врахувала»')
    dedup         = models.JSONField(default=dict, blank=True,
                                     help_text='Без дублів: {quote_letters, quote_chars, rag_dropped, rag_merged}')
    stages        = models.JSONField(default=list, blank=True, help_text='[{key, label, status, detail}]')
    status        = models.CharField(max_length=10, default=RUNNING)
    error         = models.CharField(max_length=300, blank=True, default='')
    last_message_id = models.PositiveIntegerField(default=0, help_text='Найновіший лист гілки на момент збору')
    brief_model   = models.CharField(max_length=60, blank=True, default='')
    letter_model  = models.CharField(max_length=60, blank=True, default='')
    created_at    = models.DateTimeField(auto_now_add=True)
    updated_at    = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name        = 'Бриф відповіді'
        verbose_name_plural = 'Брифи відповідей'
        ordering            = ['-updated_at']

    def __str__(self):
        return f'Бриф для листа #{self.message_id}'


class MailSortRun(models.Model):
    """«🧠 Розбір пошти» за період: групи листів без своєї категорії — за правилами й AI (email_assistant/sorting.py).
    Результат зберігається, щоб не питати AI вдруге при кожному відкритті сторінки."""
    RUNNING, DONE, FAILED = 'running', 'done', 'failed'
    STATUSES = [(RUNNING, '⏳ Йде'), (DONE, '✅ Готово'), (FAILED, '❌ Помилка')]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='mail_sort_runs')
    date_from = models.DateField('З')
    date_to = models.DateField('По')
    use_ai = models.BooleanField('З AI', default=True)
    status = models.CharField('Стан', max_length=8, choices=STATUSES, default=RUNNING)
    progress = models.CharField('Хід', max_length=255, blank=True, default='')
    groups = models.JSONField('Групи', default=list, blank=True)
    mails = models.PositiveIntegerField('Листів розібрано', default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Розбір пошти'
        verbose_name_plural = 'Розбори пошти'
        ordering = ['-created_at']


class MailSortDecision(models.Model):
    """Рішення людини щодо групи «🧠 Розбору пошти»: прийняла (категорія й правила) або «не групувати».
    Відхилене не пропонується знову; прийняте — приклади для навчання сортування."""
    ACCEPTED, REJECTED, IGNORED = 'accepted', 'rejected', 'ignored'
    DECISIONS = [(ACCEPTED, '✓ Прийнято'), (REJECTED, '✕ Не групувати'), (IGNORED, '🙈 Не рахувати')]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='mail_sort_decisions')
    key = models.CharField('Ключ групи', max_length=255, db_index=True)
    decision = models.CharField('Рішення', max_length=10, choices=DECISIONS)
    name = models.CharField('Назва', max_length=120, blank=True, default='')
    category = models.ForeignKey(MailCategory, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    rule = models.JSONField('Правило', default=dict, blank=True)
    message_ids = models.JSONField('Листи', default=list, blank=True)
    source = models.CharField('Звідки', max_length=8, blank=True, default='')   # rules / ai
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Рішення розбору пошти'
        verbose_name_plural = 'Рішення розбору пошти'
        ordering = ['-created_at']
