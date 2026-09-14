from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scraper_hub', '0003_generic_scraper_fields'),
    ]

    operations = [
        # ── site_name: add 'email' choice ─────────────────────────────────────
        migrations.AlterField(
            model_name='scrapersiteconfig',
            name='site_name',
            field=models.CharField(
                choices=[
                    ('jlcpcb', 'JLCPCB'),
                    ('ups', 'UPS Billing'),
                    ('email', 'Email (IMAP)'),
                    ('custom', 'Власний сайт'),
                ],
                max_length=50, unique=True, verbose_name='Сайт',
            ),
        ),

        # ── Remove raw cron_schedule ───────────────────────────────────────────
        migrations.RemoveField(
            model_name='scrapersiteconfig',
            name='cron_schedule',
        ),

        # ── Add structured schedule fields ────────────────────────────────────
        migrations.AddField(
            model_name='scrapersiteconfig',
            name='schedule_type',
            field=models.CharField(
                choices=[
                    ('manual', 'Лише вручну'),
                    ('daily',  'Щодня'),
                    ('weekly', 'По тижнях'),
                ],
                default='manual', max_length=20, verbose_name='Тип розкладу',
            ),
        ),
        migrations.AddField(
            model_name='scrapersiteconfig',
            name='schedule_time',
            field=models.TimeField(
                blank=True, null=True, verbose_name='Час запуску',
                help_text='Година і хвилина запуску (за локальним часом сервера).',
            ),
        ),
        migrations.AddField(
            model_name='scrapersiteconfig',
            name='schedule_weekdays',
            field=models.CharField(
                blank=True, max_length=20, verbose_name='Дні тижня',
                help_text='Числа через кому: 1=Пн, 2=Вт, ..., 7=Нд. Напр: 1,3,5 = Пн, Ср, Пт',
            ),
        ),

        # ── IMAP fields ────────────────────────────────────────────────────────
        migrations.AddField(
            model_name='scrapersiteconfig',
            name='imap_host',
            field=models.CharField(
                blank=True, default='imap.gmail.com', max_length=200,
                verbose_name='IMAP сервер',
            ),
        ),
        migrations.AddField(
            model_name='scrapersiteconfig',
            name='imap_port',
            field=models.PositiveSmallIntegerField(default=993, verbose_name='IMAP порт'),
        ),
        migrations.AddField(
            model_name='scrapersiteconfig',
            name='imap_use_ssl',
            field=models.BooleanField(default=True, verbose_name='SSL/TLS'),
        ),
        migrations.AddField(
            model_name='scrapersiteconfig',
            name='imap_folder',
            field=models.CharField(
                blank=True, default='INBOX', max_length=100, verbose_name='Папка / мітка',
            ),
        ),
        migrations.AddField(
            model_name='scrapersiteconfig',
            name='email_from_filter',
            field=models.CharField(
                blank=True, max_length=200, verbose_name='Фільтр: відправник',
            ),
        ),
        migrations.AddField(
            model_name='scrapersiteconfig',
            name='email_subject_kw',
            field=models.CharField(
                blank=True, max_length=200, verbose_name='Фільтр: слова в темі',
            ),
        ),
        migrations.AddField(
            model_name='scrapersiteconfig',
            name='email_attach_ext',
            field=models.CharField(
                blank=True, default='.pdf', max_length=50, verbose_name='Тип вкладень',
            ),
        ),
        migrations.AddField(
            model_name='scrapersiteconfig',
            name='email_mark_read',
            field=models.BooleanField(
                default=True, verbose_name='Позначати прочитаним після обробки',
            ),
        ),
    ]
