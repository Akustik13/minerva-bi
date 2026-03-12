from django.db import models


class Task(models.Model):

    class Status(models.TextChoices):
        PENDING     = 'pending',     '⏳ Очікує'
        IN_PROGRESS = 'in_progress', '🔄 В роботі'
        DONE        = 'done',        '✅ Виконано'
        CANCELLED   = 'cancelled',   '❌ Скасовано'

    class Priority(models.TextChoices):
        LOW      = 'low',      '🔵 Низький'
        MEDIUM   = 'medium',   '🟡 Середній'
        HIGH     = 'high',     '🔴 Високий'
        CRITICAL = 'critical', '🚨 Критичний'

    class TaskType(models.TextChoices):
        MANUAL         = 'manual',         '✏️ Вручну'
        STOCK_ALERT    = 'stock_alert',    '📦 Критичний склад'
        DEADLINE_ALERT = 'deadline_alert', '🚚 Прострочений дедлайн'
        NOTE_REMINDER  = 'note_reminder',  '📋 Нагадування'

    title       = models.CharField('Задача', max_length=255)
    description = models.TextField('Деталі', blank=True, default='')
    due_date    = models.DateField('Дедлайн', null=True, blank=True)
    status      = models.CharField('Статус', max_length=20,
                                   choices=Status.choices, default=Status.PENDING)
    priority    = models.CharField('Пріоритет', max_length=20,
                                   choices=Priority.choices, default=Priority.MEDIUM)
    task_type   = models.CharField('Тип', max_length=30,
                                   choices=TaskType.choices, default=TaskType.MANUAL)

    # --- Зв'язки (всі опціональні) ---
    order    = models.ForeignKey(
        'sales.SalesOrder', on_delete=models.CASCADE,
        null=True, blank=True, related_name='tasks', verbose_name='Замовлення',
    )
    customer = models.ForeignKey(
        'crm.Customer', on_delete=models.CASCADE,
        null=True, blank=True, related_name='tasks', verbose_name='Клієнт',
    )
    product  = models.ForeignKey(
        'inventory.Product', on_delete=models.CASCADE,
        null=True, blank=True, related_name='tasks', verbose_name='Товар',
    )
    note     = models.ForeignKey(
        'crm.CustomerNote', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='tasks', verbose_name='Нотатка',
    )
    assigned_to = models.ForeignKey(
        'auth.User', on_delete=models.SET_NULL,
        null=True, blank=True, verbose_name='Виконавець',
    )

    # --- Email нотифікація ---
    notify_email = models.BooleanField('Email нагадування', default=False)
    notified_at  = models.DateTimeField('Надіслано о', null=True, blank=True)

    created_at = models.DateTimeField('Створено', auto_now_add=True)
    updated_at = models.DateTimeField('Оновлено', auto_now=True)

    class Meta:
        verbose_name = 'Задача'
        verbose_name_plural = 'Задачі'
        ordering = ['status', 'due_date', '-priority']

    def __str__(self):
        due = f' [{self.due_date}]' if self.due_date else ''
        return f'{self.title}{due}'
