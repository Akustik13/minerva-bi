"""🤖 Агент Minerva — доручення («на такі листи відповідай так»), завдання («знайди й внеси …») і журнал.

Логіка — agent/mail.py (листи), agent/tasks.py + agent/tools.py (завдання), рівні ризику дій — agent/risk.py.
Доручення належить скриньці (пошта — лише власника), завдання — людині, що його дала: бачить і змінює лише вона.
"""
from django.conf import settings
from django.db import models


TASK_MODELS = [
    ('claude-opus-5-5', 'Claude Opus 5.5 — найточніше · $4 / $20 за 1 млн токенів'),
    ('claude-sonnet-5-5', 'Claude Sonnet 5.5 — швидко й якісно · $2 / $10'),
    ('claude-haiku-5-5', 'Claude Haiku 5.5 — найдешевше · $0.10 / $0.50'),
]


class AgentRule(models.Model):
    """Доручення агенту на нові листи скриньки, що підходять під умови. Вид mail_reply — відповідь за інструкцією
    (чернетка або, якщо дозволено, надіслати самому); mail_task — 🧩 завдання агенту з кожного такого листа
    (трек-номер у замовлення, закупівля з листа постачальника …): пропозиції застосовуєте ви ✓, а з auto_apply
    трек-номер і кур’єра в порожнє поле замовлення та трек-номер посилки закупівлі агент вносить сам."""
    MAIL_REPLY, MAIL_TASK = 'mail_reply', 'mail_task'
    KINDS = [(MAIL_REPLY, '✉️ Відповідати на такі листи'),
             (MAIL_TASK, '🧩 Завдання агенту з кожного такого листа')]
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
        help_text='Відповідь: що написати, чим підтвердити, коли не відповідати (факти — лише з Minerva й листа, '
                  'чого немає — [уточнити] і чернетка). Завдання: що зробити з листом — напр. «знайди номер '
                  'замовлення й трек-номер і внеси трек-номер у замовлення».')
    auto_apply = models.BooleanField(
        '✓ Трек-номер вносити самому', default=False,
        help_text='Лише «🧩 Завдання агенту»: трек-номер і кур’єра — у замовлення, де поле ще порожнє, і трек-номер '
                  'посилки до наявної закупівлі. Решту пропозицій (статус, нова закупівля, прихід…) застосовуєте ви ✓.')
    task_model = models.CharField('Модель AI завдання', max_length=40, choices=TASK_MODELS,
                                  default='claude-sonnet-5-5')
    task_budget_usd = models.DecimalField('Найбільше на одне завдання, $', max_digits=6, decimal_places=2,
                                          default=0.5)
    mode = models.CharField('Режим', max_length=10, choices=MODES, default=DRAFT)
    send_delay_minutes = models.PositiveSmallIntegerField(
        'Надсилати через, хв', default=15,
        help_text='Режим «Надсилати самому»: лист стає в «⏳ Заплановані», до відправки можна скасувати.')
    daily_limit = models.PositiveSmallIntegerField(
        'Найбільше листів на день', default=20,
        help_text='Відповідь: понад це — лише чернетки (режим «Надсилати самому»). Завдання: понад це за день — '
                  'листи пропускаються.')
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
    TASKED = 'tasked'
    STATUSES = [
        (QUEUED, '⏳ У черзі'),
        (RUNNING, '⚙️ Готує'),
        (DRAFTED, '📝 Чернетка готова'),
        (SCHEDULED, '✉️ Заплановано надіслати'),
        (TASKED, '🧩 Завдання агенту'),
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
    task = models.ForeignKey('AgentTask', null=True, blank=True, on_delete=models.SET_NULL,
                             related_name='mail_runs', verbose_name='Завдання агенту')
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


class AgentTask(models.Model):
    """Завдання агенту словами людини: «знайди вагу в даташитах і внеси», «знайди даташити й надішли на …».
    Агент читає товари й даташити (agent/tools.py) і лише ПРОПОНУЄ зміни (AgentProposal) — застосовує людина."""
    QUEUED, RUNNING, REVIEW, DONE, FAILED, CANCELLED = 'queued', 'running', 'review', 'done', 'failed', 'cancelled'
    STATUSES = [
        (QUEUED, '⏳ У черзі'),
        (RUNNING, '⚙️ Агент працює'),
        (REVIEW, '👀 Чекає вашої перевірки'),
        (DONE, '✅ Готово'),
        (FAILED, '⚠️ Помилка'),
        (CANCELLED, '✕ Зупинено'),
    ]
    MODELS = TASK_MODELS

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='agent_tasks',
                              verbose_name='Хто дав завдання')
    title = models.CharField('Завдання', max_length=200)
    instruction = models.TextField('Що зробити')
    products = models.ManyToManyField('inventory.Product', blank=True, related_name='+', verbose_name='Товари')
    message = models.ForeignKey('email_assistant.EmailMessage', null=True, blank=True, on_delete=models.SET_NULL,
                                related_name='agent_tasks', verbose_name='Лист',
                                help_text='Завдання з листа: агент читає цей лист і його вкладення (лише своя скринька)')
    api_key = models.ForeignKey('api.APIKey', null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
                                verbose_name='Ключ API (MCP)',
                                help_text='Пропозиції зовнішнього агента через MCP (agent/mcp.py)')
    mcp_session = models.CharField(max_length=80, blank=True, default='', db_index=True,
                                   help_text='Сесія MCP (Mcp-Session-Id) або «day:<дата>» без сесії')
    each_product = models.BooleanField(
        'Окремо для кожного товару', default=False,
        help_text='Агент проходить вибрані товари по одному (для сотень товарів). Лист у цьому режимі не готує.')
    web = models.BooleanField('Можна шукати в інтернеті', default=False,
                              help_text='Пошук на сайтах виробників, коли в Minerva даних немає (дорожче).')
    model = models.CharField('Модель AI', max_length=40, choices=MODELS, default='claude-opus-5-5')
    budget_usd = models.DecimalField('Найбільше витратити, $', max_digits=8, decimal_places=2, default=2,
                                     help_text='Досягнуто — агент зупиняється, знайдене лишається.')
    status = models.CharField('Стан', max_length=12, choices=STATUSES, default=QUEUED, db_index=True)
    done_products = models.JSONField(default=list, blank=True, help_text='pk товарів, які агент уже пройшов')
    summary = models.TextField('Що зробив агент', blank=True, default='')
    error = models.CharField('Помилка', max_length=500, blank=True, default='')
    log = models.JSONField(default=list, blank=True, help_text='Кроки агента: [{at, text}]')
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    web_searches = models.PositiveIntegerField(default=0)
    cost_usd = models.DecimalField('Витрачено, $', max_digits=8, decimal_places=4, default=0)
    attempts = models.PositiveSmallIntegerField(default=0, help_text='Скільки разів роботу продовжено після перерви')
    created_at = models.DateTimeField('Створено', auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Завдання агенту'
        verbose_name_plural = '🤖 Завдання агенту'
        ordering = ['-created_at']

    def __str__(self):
        return self.title


class AgentProposal(models.Model):
    """Що агент пропонує змінити: поле товару, технічний параметр або лист. Застосовує лише людина (✓)."""
    FIELD, ATTRIBUTE, MAIL = 'field', 'attribute', 'mail'
    STOCK_IN, PRODUCT, EVENT = 'stock_in', 'product', 'event'
    SHOP, DK_PRICES, DK_LEAD = 'shop', 'dk_prices', 'dk_lead'
    SHIPMENT, ORDER = 'shipment', 'order'
    PURCHASE, PARCEL = 'purchase', 'parcel'
    KINDS = [(FIELD, 'Поле товару'), (ATTRIBUTE, 'Технічний параметр'), (MAIL, 'Лист'),
             (STOCK_IN, '📦 Прихід на склад'), (PRODUCT, '🆕 Новий товар'), (EVENT, '📅 Подія в календарі'),
             (SHOP, '🏪 Інтернет-магазин'), (DK_PRICES, '💲 Ціни на DigiKey'), (DK_LEAD, '⏱ Строк на DigiKey'),
             (SHIPMENT, '🚚 Чернетка відправлення'), (ORDER, '🧾 Зміна замовлення'),
             (PURCHASE, '🛒 Закупівля'), (PARCEL, '📥 Посилка до нас')]
    PENDING, APPLIED, REJECTED, FAILED = 'pending', 'applied', 'rejected', 'failed'
    STATUSES = [(PENDING, '👀 Чекає'), (APPLIED, '✓ Застосовано'), (REJECTED, '✕ Відхилено'),
                (FAILED, '⚠️ Не вдалося')]

    task = models.ForeignKey(AgentTask, on_delete=models.CASCADE, related_name='proposals', verbose_name='Завдання')
    kind = models.CharField('Що', max_length=10, choices=KINDS)
    product = models.ForeignKey('inventory.Product', null=True, blank=True, on_delete=models.CASCADE,
                                related_name='+', verbose_name='Товар')
    field = models.CharField('Поле / параметр', max_length=200, blank=True, default='')
    old_value = models.TextField('Було', blank=True, default='')
    new_value = models.TextField('Стане', blank=True, default='')
    evidence = models.TextField('Підстава (цитата)', blank=True, default='')
    source = models.CharField('Звідки', max_length=500, blank=True, default='')
    confidence = models.CharField('Впевненість', max_length=10, blank=True, default='')
    mail_to = models.CharField('Кому', max_length=500, blank=True, default='')
    mail_subject = models.CharField('Тема', max_length=500, blank=True, default='')
    mail_body = models.TextField('Текст листа', blank=True, default='')
    mail_attach = models.JSONField(default=list, blank=True, help_text='Артикули, чиї даташити додати до листа')
    data = models.JSONField(default=dict, blank=True,
                            help_text='Прихід: {sku, qty, location, reference}; новий товар: {sku, name, manufacturer, '
                                      'category}; подія: {title, type, start, end, all_day, description}')
    status = models.CharField('Стан', max_length=10, choices=STATUSES, default=PENDING, db_index=True)
    error = models.CharField('Помилка', max_length=500, blank=True, default='')
    result_url = models.CharField(max_length=300, blank=True, default='', help_text='Чернетка / запланований лист')
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Пропозиція агента'
        verbose_name_plural = 'Пропозиції агента'
        ordering = ['product__sku', 'pk']

    def __str__(self):
        return f'{self.get_kind_display()}: {self.field or self.mail_subject}'
