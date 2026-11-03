"""🤖 Агент Minerva — доручення («на такі листи відповідай так») і журнал його роботи.

Логіка — agent/mail.py (листи), рівні ризику дій — agent/risk.py. Доручення належить скриньці: пошта — лише
власника, тож і доручення бачить і змінює лише він (адмінка, сторінки агента).
"""
from django.conf import settings
from django.db import models


class AgentRule(models.Model):
    """Доручення агенту. Вид mail_reply: нові листи скриньки, що підходять під умови, → відповідь за інструкцією
    (чернетка або, якщо дозволено, надіслати самому)."""
    MAIL_REPLY = 'mail_reply'
    KINDS = [(MAIL_REPLY, '✉️ Відповідати на такі листи')]
    DRAFT, SEND = 'draft', 'send'
    MODES = [(DRAFT, '📝 Лише чернетка — ви перевіряєте й надсилаєте'),
             (SEND, '✉️ Надсилати самому — через кілька хвилин, можна скасувати')]

    kind = models.CharField('Що робить', max_length=20, choices=KINDS, default=MAIL_REPLY)
    name = models.CharField('Назва', max_length=200)
    enabled = models.BooleanField('Увімкнено', default=True)
    account = models.ForeignKey('email_assistant.EmailAccount', on_delete=models.CASCADE,
                                related_name='agent_rules', verbose_name='Скринька')
    senders = models.TextField(
        'Від кого', blank=True, default='',
        help_text='По одному в рядку: адреса (anna@firma.de), @домен (з піддоменами) або частина домену. '
                  'Підходить будь-який рядок.')
    subject_words = models.TextField(
        'Тема містить', blank=True, default='',
        help_text='По одному в рядку; підходить будь-який (без різниці великі / малі літери).')
    body_words = models.TextField(
        'Текст листа містить', blank=True, default='',
        help_text='По одному в рядку, лише новий текст листа (без цитати); підходить будь-який.')
    category = models.ForeignKey('email_assistant.MailCategory', null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name='+', verbose_name='Своя категорія листа')
    instruction = models.TextField(
        'Що робити — інструкція агенту',
        help_text='Як відповідати: що написати, чим підтвердити, коли не відповідати. Агент бере факти лише з '
                  'Minerva й листа, а чого немає — пише [уточнити] і лишає чернетку.')
    mode = models.CharField('Режим', max_length=10, choices=MODES, default=DRAFT)
    send_delay_minutes = models.PositiveSmallIntegerField(
        'Надсилати через, хв', default=15,
        help_text='Режим «Надсилати самому»: лист стає в «⏳ Заплановані», до відправки можна скасувати.')
    daily_limit = models.PositiveSmallIntegerField(
        'Найбільше листів на день', default=20,
        help_text='Понад це — лише чернетки (режим «Надсилати самому»).')
    example_message = models.ForeignKey('email_assistant.EmailMessage', null=True, blank=True,
                                        on_delete=models.SET_NULL, related_name='+',
                                        verbose_name='Створено з листа')
    seen_pk = models.BigIntegerField(
        default=0, help_text='Листи скриньки з більшим номером агент ще не перевіряв (раніші — не чіпає)')
    streak = models.PositiveIntegerField(
        'Поспіль без змін', default=0,
        help_text='Скільки чернеток агента поспіль ви надіслали без правок (скидається правкою чи видаленням)')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='+', verbose_name='Створив')
    created_at = models.DateTimeField('Створено', auto_now_add=True)
    updated_at = models.DateTimeField('Змінено', auto_now=True)

    class Meta:
        verbose_name = 'Доручення агента'
        verbose_name_plural = '🤖 Доручення агента'
        ordering = ['-created_at']

    def __str__(self):
        return self.name

    def lines(self, field) -> list:
        return [ln.strip().lower() for ln in (getattr(self, field) or '').splitlines() if ln.strip()]

    @property
    def has_conditions(self) -> bool:
        return bool(self.lines('senders') or self.lines('subject_words') or self.lines('body_words')
                    or self.category_id)


class AgentRun(models.Model):
    """Один лист за одним дорученням: що агент вирішив і зробив, а потім — що зробили ви з його чернеткою."""
    QUEUED, RUNNING, DRAFTED, SCHEDULED, SKIPPED, FAILED = 'queued', 'running', 'drafted', 'scheduled', 'skipped', 'failed'
    STATUSES = [
        (QUEUED, '⏳ У черзі'),
        (RUNNING, '⚙️ Готує'),
        (DRAFTED, '📝 Чернетка готова'),
        (SCHEDULED, '✉️ Заплановано надіслати'),
        (SKIPPED, '↷ Пропущено'),
        (FAILED, '⚠️ Помилка'),
    ]
    SENT_SAME, SENT_EDITED, DISCARDED = 'sent_same', 'sent_edited', 'discarded'
    OUTCOMES = [
        ('', '—'),
        (SENT_SAME, '✓ Надіслано без змін'),
        (SENT_EDITED, '✏️ Надіслано з правками'),
        (DISCARDED, '🗑 Чернетку видалено'),
    ]

    rule = models.ForeignKey(AgentRule, on_delete=models.CASCADE, related_name='runs', verbose_name='Доручення')
    message = models.ForeignKey('email_assistant.EmailMessage', on_delete=models.CASCADE, related_name='agent_runs',
                                verbose_name='Лист')
    status = models.CharField('Стан', max_length=12, choices=STATUSES, default=QUEUED, db_index=True)
    reason = models.CharField('Чому / примітка', max_length=500, blank=True, default='')
    reply = models.TextField('Відповідь агента', blank=True, default='')
    model = models.CharField('Модель', max_length=80, blank=True, default='')
    draft = models.ForeignKey('email_assistant.EmailDraft', null=True, blank=True, on_delete=models.SET_NULL,
                              related_name='+', verbose_name='Чернетка')
    scheduled = models.ForeignKey('email_assistant.ScheduledEmail', null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name='+', verbose_name='Запланований лист')
    outcome = models.CharField('Що зробили ви', max_length=12, choices=OUTCOMES, blank=True, default='')
    created_at = models.DateTimeField('Коли', auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Робота агента'
        verbose_name_plural = '📜 Журнал агента'
        ordering = ['-created_at']
        constraints = [models.UniqueConstraint(fields=['rule', 'message'], name='agent_run_once_per_mail')]

    def __str__(self):
        return f'{self.rule} · {self.message_id}'
