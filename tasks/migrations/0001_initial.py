from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
        ('crm', '0008_customernote_due_date_reminder'),
        ('inventory', '0019_product_customs_fields'),
        ('sales', '0019_rename_sales_order_date_idx_sales_sales_order_d_55e46c_idx_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='Task',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=255, verbose_name='Задача')),
                ('description', models.TextField(blank=True, default='', verbose_name='Деталі')),
                ('due_date', models.DateField(blank=True, null=True, verbose_name='Дедлайн')),
                ('status', models.CharField(
                    choices=[
                        ('pending', '⏳ Очікує'),
                        ('in_progress', '🔄 В роботі'),
                        ('done', '✅ Виконано'),
                        ('cancelled', '❌ Скасовано'),
                    ],
                    default='pending', max_length=20, verbose_name='Статус',
                )),
                ('priority', models.CharField(
                    choices=[
                        ('low', '🔵 Низький'),
                        ('medium', '🟡 Середній'),
                        ('high', '🔴 Високий'),
                        ('critical', '🚨 Критичний'),
                    ],
                    default='medium', max_length=20, verbose_name='Пріоритет',
                )),
                ('task_type', models.CharField(
                    choices=[
                        ('manual', '✏️ Вручну'),
                        ('stock_alert', '📦 Критичний склад'),
                        ('deadline_alert', '🚚 Прострочений дедлайн'),
                        ('note_reminder', '📋 Нагадування'),
                    ],
                    default='manual', max_length=30, verbose_name='Тип',
                )),
                ('notify_email', models.BooleanField(default=False, verbose_name='Email нагадування')),
                ('notified_at', models.DateTimeField(blank=True, null=True, verbose_name='Надіслано о')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Створено')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Оновлено')),
                ('assigned_to', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    to=settings.AUTH_USER_MODEL, verbose_name='Виконавець',
                )),
                ('customer', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='tasks', to='crm.customer', verbose_name='Клієнт',
                )),
                ('note', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='tasks', to='crm.customernote', verbose_name='Нотатка',
                )),
                ('order', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='tasks', to='sales.salesorder', verbose_name='Замовлення',
                )),
                ('product', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='tasks', to='inventory.product', verbose_name='Товар',
                )),
            ],
            options={
                'verbose_name': 'Задача',
                'verbose_name_plural': 'Задачі',
                'ordering': ['status', 'due_date', '-priority'],
            },
        ),
    ]
