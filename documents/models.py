"""
documents/models.py

Система генерації документів з Word шаблонів.

КОНЦЕПЦІЯ ШАБЛОНІВ:
  Шаблон — звичайний .docx файл де в тексті є {{змінні}}.
  Приклад: "Замовлення №: {{order_number}}"
  Для таблиць: {% for item in items %}...{% endfor %}

ДОСТУПНІ ЗМІННІ — повний перелік в TEMPLATE_VARIABLES_GUIDE нижче.
"""
from django.db import models
from django.contrib.auth.models import User


# ── Константи ────────────────────────────────────────────────────────────────

MODULE_CHOICES = [
    ('sales',    'Продажі (замовлення)'),
    ('crm',      'Клієнти'),
    ('any',      'Всі модулі'),
]



DOC_TYPE_CHOICES = [
    ('packing_list', 'Packing List (Пакувальний лист)'),
    ('proforma',     'Proforma Invoice (Проформа)'),
    ('invoice',      'Invoice / Рахунок-фактура'),
    ('cn23',         'CN23 Митна декларація'),
    ('custom',       'Кастомний документ'),
]

LANG_CHOICES = [
    ('en', 'English'),
    ('de', 'Deutsch'),
    ('uk', 'Українська'),
]

TEMPLATE_VARIABLES_GUIDE = """
╔══════════════════════════════════════════════════════════════════════════╗
║           ПОВНИЙ ДОВІДНИК ЗМІННИХ ДЛЯ WORD ШАБЛОНІВ Minerva             ║
║   Використовуй {{змінна}} в тексті шаблону для підстановки даних        ║
╚══════════════════════════════════════════════════════════════════════════╝

── ЗАМОВЛЕННЯ ─────────────────────────────────────────────────────────────
{{order_number}}        → Номер замовлення (наприклад: 98537843)
{{order_date}}          → Дата замовлення (DD.MM.YYYY)
{{order_status}}        → Статус замовлення
{{invoice_number}}      → Номер рахунку (INV-98537843)
{{invoice_date}}        → Дата виставлення рахунку (сьогодні)
{{due_date}}            → Дата оплати (сьогодні + 30 днів)

── КЛІЄНТ / ОДЕРЖУВАЧ ─────────────────────────────────────────────────────
{{customer_name}}       → Назва компанії або ім'я клієнта
{{customer_address}}    → Адреса доставки (вулиця)
{{customer_city}}       → Місто, індекс
{{customer_country}}    → Країна
{{customer_email}}      → Email клієнта
{{customer_phone}}      → Телефон клієнта
{{customer_vat}}        → VAT номер клієнта (якщо є)

── ВІДПРАВНИК / НАША КОМПАНІЯ ─────────────────────────────────────────────
{{shipper_name}}        → Назва нашої компанії
{{shipper_address}}     → Наша адреса (вулиця)
{{shipper_city}}        → Наше місто з індексом
{{shipper_country}}     → Наша країна
{{shipper_email}}       → Наш email
{{shipper_phone}}       → Наш телефон
{{vat_number}}          → Наш VAT номер
{{eori_number}}         → EORI номер (з Системних налаштувань)
{{bank_name}}           → Назва банку
{{bank_iban}}           → IBAN рахунку
{{bank_swift}}          → SWIFT/BIC код

── ДОСТАВКА ───────────────────────────────────────────────────────────────
{{tracking_number}}     → Трекінг номер відправлення
{{carrier_name}}        → Перевізник (UPS / Nova Poshta / DHL)
{{shipping_date}}       → Дата відправки

── ФІНАНСИ ────────────────────────────────────────────────────────────────
{{currency}}            → Валюта (EUR / USD / UAH)
{{subtotal}}            → Сума без ПДВ
{{vat_rate}}            → Ставка ПДВ (%)
{{vat_amount}}          → Сума ПДВ
{{total_amount}}        → Загальна сума з ПДВ
{{payment_terms}}       → Умови оплати

── ФІЗИЧНІ ПАРАМЕТРИ ──────────────────────────────────────────────────────
{{total_weight}}        → Загальна вага (кг)
{{total_items}}         → Загальна кількість одиниць
{{items_count}}         → Кількість рядків в замовленні

── МИТНА ДЕКЛАРАЦІЯ (CN23) ────────────────────────────────────────────────
{{customs_type}}        → Тип відправлення (SALE / GIFT / SAMPLE)
{{customs_reason}}      → Причина (Commercial goods)
{{country_of_origin}}   → Країна виробництва
{{declared_value}}      → Задекларована вартість
{{gross_weight}}        → Вага брутто

── ТАБЛИЦЯ ТОВАРІВ (for loop) ─────────────────────────────────────────────
{% for item in items %}
{{item.sku}}            → Артикул / SKU товару
{{item.name}}           → Назва товару
{{item.quantity}}       → Кількість
{{item.unit_price}}     → Ціна за одиницю
{{item.total_price}}    → Сума рядка
{{item.weight}}         → Вага рядка (кг)
{{item.hs_code}}        → Код ТН ЗЕД (для митниці)
{{item.country}}        → Країна походження товару
{% endfor %}

── МЕТА ───────────────────────────────────────────────────────────────────
{{generated_date}}      → Дата і час генерації
{{generated_by}}        → Система (Minerva BI)
{{notes}}               → Примітки до документа
{{proforma_notes}}      → Примітки для Proforma

══════════════════════════════════════════════════════════════════════════
ПРИКЛАД ТАБЛИЦІ В WORD ШАБЛОНІ:
──────────────────────────────
| SKU          | Назва         | К-сть         | Ціна          | Сума  |
| {{item.sku}} | {{item.name}} | {{item.quantity}} | {{item.unit_price}} | {{item.total_price}} |

(рядок таблиці обгорни в {% for item in items %}...{% endfor %})
══════════════════════════════════════════════════════════════════════════
"""


# ── Моделі ───────────────────────────────────────────────────────────────────

class DocumentTemplate(models.Model):
    """
    Word шаблон документа (.docx файл з {{змінними}}).

    Як створити шаблон:
    1. Відкрий Word і напиши документ як звичайно
    2. Де треба підставити дані — пиши {{назва_змінної}}
    3. Для таблиць з товарами: обгорни рядок в {% for item in items %}...{% endfor %}
    4. Збережи як .docx і завантаж тут
    5. Довідник всіх змінних — в полі "Довідник змінних" нижче
    """
    name = models.CharField(
        max_length=200,
        verbose_name='Назва шаблону',
        help_text='Наприклад: Packing List EN, CN23 Митна декларація')

    doc_type = models.CharField(
        max_length=30, choices=DOC_TYPE_CHOICES, default='custom',
        verbose_name='Тип документа')

    module = models.CharField(
        max_length=20, choices=MODULE_CHOICES, default='sales',
        verbose_name='Прив\'язати до модуля',
        help_text='Кнопка генерації з\'явиться в картці цього модуля')

    language = models.CharField(
        max_length=5, choices=LANG_CHOICES, default='en',
        verbose_name='Мова документа')

    template_file = models.FileField(
        upload_to='document_templates/',
        verbose_name='Word шаблон (.docx)',
        help_text=(
            'Завантаж .docx файл з {{змінними}} в тексті. '
            'Дивись "Довідник змінних" нижче для повного списку.'
        ))

    description = models.TextField(
        blank=True,
        verbose_name='Опис шаблону',
        help_text='Для чого використовується цей шаблон')

    source = models.ForeignKey(
        'sales.SalesSource',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        verbose_name='Джерело замовлення',
        help_text='Залиш порожнім — шаблон буде доступний для всіх джерел. '
                  'Якщо вказано — кнопка з\'явиться тільки для замовлень цього джерела.',
        related_name='document_templates',
    )

    is_active = models.BooleanField(default=True, verbose_name='Активний')
    is_default = models.BooleanField(
        default=False,
        verbose_name='За замовчуванням',
        help_text='Цей шаблон буде першим в списку для свого типу')

    sort_order = models.PositiveIntegerField(default=0, verbose_name='Порядок сортування')

    created_by = models.ForeignKey(
        User, null=True, blank=True,
        on_delete=models.SET_NULL,
        verbose_name='Завантажив',
        related_name='created_doc_templates')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name        = 'Шаблон документа'
        verbose_name_plural = 'Шаблони документів'
        ordering            = ['sort_order', 'doc_type', 'name']

    def __str__(self):
        return f'{self.name} [{self.get_doc_type_display()}] [{self.get_language_display()}]'


class GeneratedDocument(models.Model):
    """Згенерований документ — зберігається на сервері."""

    STATUS_CHOICES = [
        ('generating', '⏳ Генерується'),
        ('ready',      '✅ Готовий'),
        ('error',      '❌ Помилка'),
    ]

    template = models.ForeignKey(
        DocumentTemplate, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='generated_docs',
        verbose_name='Шаблон')

    source_module    = models.CharField(max_length=20, blank=True, verbose_name='Модуль')
    source_object_id = models.PositiveIntegerField(null=True, blank=True, verbose_name='ID об\'єкта')
    source_repr      = models.CharField(max_length=300, blank=True, verbose_name='Опис')

    docx_file = models.FileField(
        upload_to='generated_documents/%Y/%m/',
        null=True, blank=True,
        verbose_name='Word файл (.docx)')
    pdf_file = models.FileField(
        upload_to='generated_documents/%Y/%m/',
        null=True, blank=True,
        verbose_name='PDF файл')

    status    = models.CharField(max_length=20, choices=STATUS_CHOICES, default='generating')
    error_msg = models.TextField(blank=True, verbose_name='Помилка')

    generated_by = models.ForeignKey(
        User, null=True, blank=True,
        on_delete=models.SET_NULL,
        verbose_name='Генерував',
        related_name='generated_docs')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name        = 'Згенерований документ'
        verbose_name_plural = 'Згенеровані документи'
        ordering            = ['-created_at']

    def __str__(self):
        name = (self.docx_file.name.split('/')[-1]
                if self.docx_file else f'Doc #{self.pk}')
        return f'{self.source_repr} — {name}'

    def file_size_display(self):
        try:
            return f'{self.docx_file.size / 1024:.1f} KB'
        except Exception:
            return '—'

    def delete_files(self):
        import os
        for field in ('docx_file', 'pdf_file'):
            f = getattr(self, field)
            if f:
                try:
                    if os.path.exists(f.path):
                        os.remove(f.path)
                except Exception:
                    pass
