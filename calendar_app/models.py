from django.contrib.auth.models import User
from django.db import models


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

    is_done    = models.BooleanField(default=False, verbose_name='Виконано', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name        = 'Подія календаря'
        verbose_name_plural = 'Календар'
        ordering            = ['start_at']

    def __str__(self):
        return f'{self.title} ({self.start_at:%d.%m.Y %H:%M})'
