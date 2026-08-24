from decimal import Decimal
import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('inventory', '0033_rfqemailtemplate_diagram_in_body'),
    ]

    operations = [
        migrations.CreateModel(
            name='JLCConfig',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('api_key', models.CharField(blank=True, default='', max_length=500, verbose_name='API Ключ')),
                ('api_secret', models.CharField(blank=True, default='', max_length=500, verbose_name='API Secret')),
                ('use_sandbox', models.BooleanField(default=True, verbose_name='Sandbox режим')),
                ('sync_enabled', models.BooleanField(default=False, verbose_name='Авто-синхронізація')),
                ('sync_interval_hours', models.PositiveSmallIntegerField(default=4, verbose_name='Інтервал (годин)')),
                ('last_synced_at', models.DateTimeField(blank=True, null=True, verbose_name='Остання синхронізація')),
                ('auto_receive_on_delivered', models.BooleanField(default=True, verbose_name='Авто-прийом на склад при доставці')),
                ('default_location', models.CharField(default='MAIN', max_length=50, verbose_name='Локація складу за замовчуванням')),
                ('notify_on_shipped', models.BooleanField(default=True, verbose_name='Сповіщення: відправлено')),
                ('notify_on_status_change', models.BooleanField(default=True, verbose_name='Сповіщення: будь-яка зміна статусу')),
                ('notify_on_delivered', models.BooleanField(default=True, verbose_name='Сповіщення: доставлено')),
                ('notify_telegram', models.BooleanField(default=True, verbose_name='Telegram')),
                ('notify_email', models.BooleanField(default=False, verbose_name='Email')),
                ('notify_email_to', models.CharField(blank=True, default='', max_length=500, verbose_name='Email одержувачів (через кому)')),
            ],
            options={
                'verbose_name': 'Налаштування JLCPCB',
                'verbose_name_plural': '⚙️ Налаштування JLCPCB',
            },
        ),
        migrations.CreateModel(
            name='JLCOrder',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('jlc_order_id', models.CharField(max_length=100, unique=True, verbose_name='ID замовлення JLC')),
                ('jlc_order_number', models.CharField(blank=True, default='', max_length=100, verbose_name='Номер замовлення')),
                ('order_type', models.CharField(choices=[('pcb', 'PCB'), ('pcba', 'PCBA (складання)'), ('stencil', 'Stencil'), ('3d', '3D Printing'), ('other', 'Інше')], default='pcb', max_length=20, verbose_name='Тип')),
                ('description', models.CharField(blank=True, default='', help_text='Назва з JLC, наприклад: AN120202-01H_2L_FR4_0.8x160x77.5mm_', max_length=500, verbose_name='Опис/назва JLC')),
                ('quantity', models.PositiveIntegerField(default=1, verbose_name='Кількість (шт)')),
                ('jlc_status', models.CharField(blank=True, default='', max_length=100, verbose_name='Статус JLC (raw)')),
                ('local_status', models.CharField(choices=[('ordered', 'Замовлено'), ('reviewed', 'Перевірено JLC'), ('in_production', 'У виробництві'), ('manufactured', 'Виготовлено'), ('shipped', 'Відправлено'), ('delivered', 'Доставлено'), ('cancelled', 'Скасовано')], db_index=True, default='ordered', max_length=20, verbose_name='Статус')),
                ('tracking_number', models.CharField(blank=True, default='', max_length=255, verbose_name='Трекінг номер')),
                ('tracking_carrier', models.CharField(blank=True, default='', max_length=100, verbose_name='Перевізник')),
                ('tracking_url', models.URLField(blank=True, default='', verbose_name='URL трекінгу')),
                ('order_date', models.DateField(default=django.utils.timezone.now, verbose_name='Дата замовлення')),
                ('shipped_date', models.DateField(blank=True, null=True, verbose_name='Дата відправлення')),
                ('delivered_date', models.DateField(blank=True, null=True, verbose_name='Дата доставки')),
                ('expected_date', models.DateField(blank=True, null=True, verbose_name='Очікувана дата')),
                ('unit_price', models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True, verbose_name='Ціна за шт')),
                ('total_price', models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True, verbose_name='Загальна вартість')),
                ('currency', models.CharField(default='USD', max_length=3, verbose_name='Валюта')),
                ('mapping_status', models.CharField(choices=[('unmatched', "Не прив'язано"), ('matched', "Прив'язано"), ('track_only', 'Тільки трекінг'), ('ignored', 'Ігнорувати')], db_index=True, default='unmatched', max_length=20, verbose_name="Прив'язка")),
                ('auto_matched_sku', models.CharField(blank=True, default='', max_length=255, verbose_name='Авто-знайдений SKU')),
                ('received_qty', models.DecimalField(decimal_places=3, default=Decimal('0'), max_digits=12, verbose_name='Отримано на склад (шт)')),
                ('last_notified_status', models.CharField(blank=True, default='', max_length=20, verbose_name='Останній сповіщений статус')),
                ('raw_data', models.JSONField(blank=True, default=dict, verbose_name='Дані API (JSON)')),
                ('notes', models.TextField(blank=True, default='', verbose_name='Нотатки')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('product', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='jlc_orders', to='inventory.product', verbose_name='Товар на складі')),
                ('purchase_order', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='jlc_orders', to='inventory.purchaseorder', verbose_name='Замовлення на закупівлю (PO)')),
            ],
            options={
                'verbose_name': 'Замовлення JLCPCB',
                'verbose_name_plural': 'Замовлення JLCPCB',
                'ordering': ['-order_date', '-created_at'],
            },
        ),
        migrations.CreateModel(
            name='JLCProductMapping',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('jlc_reference', models.CharField(help_text='Повна або часткова назва замовлення з JLCPCB (наприклад: AN120202-01H або AN120202-01H_2L_FR4_...)', max_length=500, unique=True, verbose_name='Назва/reference JLC')),
                ('match_type', models.CharField(choices=[('auto', 'Авто'), ('manual', 'Вручну')], default='manual', max_length=20, verbose_name='Тип матчу')),
                ('notes', models.CharField(blank=True, default='', max_length=255, verbose_name='Нотатки')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('product', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='jlc_mappings', to='inventory.product', verbose_name='Товар на складі')),
            ],
            options={
                'verbose_name': "Прив'язка JLC → Товар",
                'verbose_name_plural': "Прив'язки JLC → Товар",
                'ordering': ['jlc_reference'],
            },
        ),
    ]
