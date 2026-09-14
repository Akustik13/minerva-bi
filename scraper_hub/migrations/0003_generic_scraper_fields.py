from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scraper_hub', '0002_add_jlc_order_to_scraperdocument'),
    ]

    operations = [
        # site_name: add 'custom' choice (no DB change needed for choices, but alter for max_length safety)
        migrations.AlterField(
            model_name='scrapersiteconfig',
            name='site_name',
            field=models.CharField(
                choices=[
                    ('jlcpcb', 'JLCPCB'),
                    ('ups', 'UPS Billing'),
                    ('custom', 'Власний сайт'),
                ],
                max_length=50, unique=True, verbose_name='Сайт',
            ),
        ),

        # Notification label updates (no DB change, but update help_text)
        migrations.AlterField(
            model_name='scrapersiteconfig',
            name='notify_email',
            field=models.BooleanField(default=True, verbose_name='Email-сповіщення (успіх)'),
        ),
        migrations.AlterField(
            model_name='scrapersiteconfig',
            name='notify_telegram',
            field=models.BooleanField(default=False, verbose_name='Telegram-сповіщення (успіх)'),
        ),

        # New notification fields
        migrations.AddField(
            model_name='scrapersiteconfig',
            name='notify_on_error',
            field=models.BooleanField(
                default=True,
                verbose_name='Сповіщення при помилці',
                help_text='Слати email/Telegram якщо scraper завершився з помилкою або не завантажив жодного файлу.',
            ),
        ),
        migrations.AddField(
            model_name='scrapersiteconfig',
            name='notify_email_to',
            field=models.CharField(
                blank=True, max_length=500,
                verbose_name='Email одержувачів (помилка)',
                help_text='Адреси через кому. Порожньо — використовувати alert_email із загальних налаштувань.',
            ),
        ),

        # Site URL fields
        migrations.AddField(
            model_name='scrapersiteconfig',
            name='site_url',
            field=models.URLField(
                blank=True,
                verbose_name='URL сайту',
                help_text='Базовий URL сайту (напр. https://jlcpcb.com).',
            ),
        ),
        migrations.AddField(
            model_name='scrapersiteconfig',
            name='invoice_url_tpl',
            field=models.CharField(
                blank=True, max_length=500,
                verbose_name='Шаблон URL рахунку',
                help_text='URL з {batch} placeholder — напр. https://jlcpcb.com/order/{batch}.',
            ),
        ),

        # Generic scraper config fields
        migrations.AddField(
            model_name='scrapersiteconfig',
            name='login_url',
            field=models.CharField(blank=True, max_length=500, verbose_name='URL сторінки логіну'),
        ),
        migrations.AddField(
            model_name='scrapersiteconfig',
            name='invoice_list_url',
            field=models.CharField(blank=True, max_length=500, verbose_name='URL списку рахунків'),
        ),
        migrations.AddField(
            model_name='scrapersiteconfig',
            name='email_selector',
            field=models.CharField(
                blank=True, default='input[type=email]', max_length=200,
                verbose_name='CSS: поле email',
            ),
        ),
        migrations.AddField(
            model_name='scrapersiteconfig',
            name='password_selector',
            field=models.CharField(
                blank=True, default='input[type=password]', max_length=200,
                verbose_name='CSS: поле пароля',
            ),
        ),
        migrations.AddField(
            model_name='scrapersiteconfig',
            name='submit_selector',
            field=models.CharField(
                blank=True, default='button[type=submit]', max_length=200,
                verbose_name='CSS: кнопка входу',
            ),
        ),
        migrations.AddField(
            model_name='scrapersiteconfig',
            name='login_success_url',
            field=models.CharField(
                blank=True, max_length=200,
                verbose_name='Фрагмент URL після входу',
            ),
        ),
        migrations.AddField(
            model_name='scrapersiteconfig',
            name='row_selector',
            field=models.CharField(
                blank=True, default='table tbody tr', max_length=200,
                verbose_name='CSS: рядки таблиці рахунків',
            ),
        ),
        migrations.AddField(
            model_name='scrapersiteconfig',
            name='batch_col_index',
            field=models.PositiveSmallIntegerField(
                default=0, verbose_name='Індекс колонки: batch #',
            ),
        ),
        migrations.AddField(
            model_name='scrapersiteconfig',
            name='date_col_index',
            field=models.PositiveSmallIntegerField(
                default=2, verbose_name='Індекс колонки: дата',
            ),
        ),
        migrations.AddField(
            model_name='scrapersiteconfig',
            name='download_selector',
            field=models.CharField(
                blank=True, max_length=200,
                verbose_name='CSS: кнопка завантаження',
            ),
        ),
    ]
