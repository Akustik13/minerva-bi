import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ('crm', '0001_initial'),
        ('email_assistant', '0009_scheduledmail'),
        ('sales', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='CalendarEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True)),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='calendar_events',
                    to=settings.AUTH_USER_MODEL,
                )),
                ('title',       models.CharField(max_length=300, verbose_name='Назва')),
                ('description', models.TextField(blank=True, verbose_name='Опис')),
                ('event_type',  models.CharField(
                    choices=[
                        ('deadline',        '⏰ Дедлайн'),
                        ('meeting',         '🤝 Зустріч'),
                        ('reminder',        '🔔 Нагадування'),
                        ('email_follow_up', '📧 Email follow-up'),
                        ('other',           '📌 Інше'),
                    ],
                    default='other', max_length=30, verbose_name='Тип',
                )),
                ('start_at', models.DateTimeField(db_index=True, verbose_name='Початок')),
                ('end_at',   models.DateTimeField(blank=True, null=True, verbose_name='Кінець')),
                ('all_day',  models.BooleanField(default=False, verbose_name='Весь день')),
                ('crm_customer', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='calendar_events', to='crm.customer',
                    verbose_name='CRM клієнт',
                )),
                ('email_message', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='calendar_events',
                    to='email_assistant.emailmessage',
                    verbose_name='Лист',
                )),
                ('sales_order', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='calendar_events', to='sales.salesorder',
                    verbose_name='Замовлення',
                )),
                ('remind_minutes_before', models.PositiveIntegerField(
                    default=60, verbose_name='Нагадати за N хвилин')),
                ('remind_sent', models.BooleanField(default=False)),
                ('is_done',    models.BooleanField(db_index=True, default=False,
                                                   verbose_name='Виконано')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'verbose_name': 'Подія календаря',
                'verbose_name_plural': 'Календар',
                'ordering': ['start_at'],
            },
        ),
    ]
