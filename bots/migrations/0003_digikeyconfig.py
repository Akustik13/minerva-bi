from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bots', '0002_alter_bot_schedule_cron'),
    ]

    operations = [
        migrations.CreateModel(
            name='DigiKeyConfig',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('client_id',     models.CharField(blank=True, default='', max_length=200, verbose_name='Client ID',
                                                   help_text='DigiKey Developer Portal → My Apps → Client ID')),
                ('client_secret', models.CharField(blank=True, default='', max_length=200, verbose_name='Client Secret',
                                                   help_text='DigiKey Developer Portal → My Apps → Client Secret')),
                ('account_id',    models.CharField(blank=True, default='', max_length=100, verbose_name='Account ID',
                                                   help_text='X-DIGIKEY-Account-Id (з діагностики API або DigiKey підтримки)')),
                ('locale_site',     models.CharField(default='DE', max_length=10, verbose_name='Locale Site',
                                                     help_text='DE / US / CA / GB / AT / CH / PL …')),
                ('locale_language', models.CharField(default='en', max_length=10, verbose_name='Locale Language',
                                                     help_text='en / de / fr …')),
                ('locale_currency', models.CharField(default='EUR', max_length=8,  verbose_name='Locale Currency',
                                                     help_text='EUR / USD / GBP …')),
                ('sync_enabled',          models.BooleanField(default=False, verbose_name='Синхронізація увімкнена')),
                ('sync_interval_minutes', models.PositiveSmallIntegerField(default=30,
                                          verbose_name='Інтервал синхронізації (хвилин)',
                                          help_text='Рекомендовано: 15–60 хв')),
                ('use_sandbox', models.BooleanField(default=True, verbose_name='Sandbox режим (тестовий)',
                                                    help_text='sandbox-api.digikey.com — для тестування без реальних замовлень')),
                ('last_synced_at', models.DateTimeField(blank=True, null=True, verbose_name='Остання успішна синхронізація')),
                ('access_token',     models.TextField(blank=True, default='', verbose_name='Access Token (кеш)')),
                ('token_expires_at', models.DateTimeField(blank=True, null=True, verbose_name='Токен дійсний до (UTC)')),
                ('webhook_enabled',  models.BooleanField(default=False, verbose_name='Webhook увімкнений (Phase 2)')),
                ('webhook_secret',   models.CharField(blank=True, default='', max_length=200, verbose_name='Webhook Secret',
                                                      help_text='HMAC-підпис вхідних webhook-запитів від DigiKey')),
                ('webhook_url_note', models.CharField(blank=True, default='', max_length=300, verbose_name='Webhook URL (нотатка)',
                                                      help_text='Публічний URL куди DigiKey надсилатиме POST.')),
            ],
            options={
                'verbose_name': 'DigiKey — Конфігурація',
                'verbose_name_plural': 'DigiKey — Конфігурація',
            },
        ),
    ]
