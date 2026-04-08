from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipping', '0019_shipmentpackage'),
    ]

    operations = [
        migrations.CreateModel(
            name='UPSConfig',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False)),
                ('client_id', models.CharField(blank=True, help_text='UPS Developer Portal → Apps → Credentials', max_length=200, verbose_name='Client ID')),
                ('client_secret', models.CharField(blank=True, max_length=500, verbose_name='Client Secret')),
                ('account_number', models.CharField(blank=True, help_text='UPS Account Number для виставлення рахунків', max_length=50, verbose_name='Account Number')),
                ('use_sandbox', models.BooleanField(default=True, help_text='Sandbox: wwwcie.ups.com / Production: onlinetools.ups.com', verbose_name='Тестовий режим (Sandbox)')),
                ('is_enabled', models.BooleanField(default=False, verbose_name='Увімкнути UPS')),
                ('cached_token', models.CharField(blank=True, db_column='cached_token', max_length=2000)),
                ('token_expires_at', models.DateTimeField(blank=True, db_column='token_expires_at', null=True)),
                ('shipper_name', models.CharField(blank=True, max_length=200, verbose_name="Ім'я відправника")),
                ('shipper_address', models.CharField(blank=True, max_length=300, verbose_name='Адреса')),
                ('shipper_city', models.CharField(blank=True, max_length=100, verbose_name='Місто')),
                ('shipper_state', models.CharField(blank=True, help_text='США: 2 букви (NY). Інші: можна порожньо', max_length=10, verbose_name='Штат/регіон')),
                ('shipper_postal', models.CharField(blank=True, max_length=20, verbose_name='Поштовий індекс')),
                ('shipper_country', models.CharField(blank=True, default='DE', help_text='2 букви: DE, UA, US, PL...', max_length=2, verbose_name='Код країни')),
                ('shipper_phone', models.CharField(blank=True, max_length=30, verbose_name='Телефон')),
                ('label_format', models.CharField(choices=[('PDF', 'PDF — для звичайного принтера'), ('PNG', 'PNG — зображення'), ('ZPL', 'ZPL — термо-принтер Zebra'), ('GIF', 'GIF — зображення')], default='PDF', max_length=5, verbose_name='Формат мітки')),
                ('paperless_trade', models.BooleanField(default=False, help_text='Електронна митна декларація. Потребує активації в UPS акаунті.', verbose_name='Paperless Trade')),
                ('eori_number', models.CharField(blank=True, help_text='Напр.: DE123456789', max_length=50, verbose_name='EORI номер')),
                ('vat_number', models.CharField(blank=True, max_length=50, verbose_name='VAT номер')),
                ('last_sync_at', models.DateTimeField(blank=True, null=True, verbose_name='Остання синхронізація')),
                ('total_shipments', models.PositiveIntegerField(default=0)),
            ],
            options={
                'verbose_name': 'UPS Налаштування',
                'verbose_name_plural': 'UPS Налаштування',
            },
        ),
    ]
