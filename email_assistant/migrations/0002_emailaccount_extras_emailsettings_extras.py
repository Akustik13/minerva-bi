from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('email_assistant', '0001_initial'),
    ]

    operations = [
        # EmailAccount: per-account signature
        migrations.AddField(
            model_name='emailaccount',
            name='signature',
            field=models.TextField(
                blank=True, default='',
                verbose_name='Підпис листа',
                help_text="Використовується замість підпису профілю. {name} = ваше ім'я"),
        ),
        # EmailSettings extras
        migrations.AddField(
            model_name='emailsettings',
            name='mark_read_on_server',
            field=models.BooleanField(
                default=True, verbose_name='Помічати прочитані на IMAP-сервері',
                help_text='Коли відкриваєш лист у Minerva — позначити \\Seen на сервері'),
        ),
        migrations.AddField(
            model_name='emailsettings',
            name='auto_signature',
            field=models.BooleanField(default=True, verbose_name='Автоматично вставляти підпис'),
        ),
        migrations.AddField(
            model_name='emailsettings',
            name='signature_position',
            field=models.CharField(
                max_length=20, default='after_reply',
                choices=[
                    ('after_reply', 'Після мого тексту (перед цитатою)'),
                    ('end',         'В кінці листа'),
                ],
                verbose_name='Позиція підпису',
            ),
        ),
        migrations.AddField(
            model_name='emailsettings',
            name='telegram_notify_new',
            field=models.BooleanField(
                default=True, verbose_name='Telegram: сповіщати про нові листи',
                help_text='Надсилати особисте повідомлення в Telegram коли приходить новий лист'),
        ),
        migrations.AddField(
            model_name='emailsettings',
            name='telegram_quiet_from',
            field=models.TimeField(
                null=True, blank=True,
                verbose_name='Тихий режим з (година)',
                help_text='Не надсилати Telegram починаючи з цієї години. Напр. 22:00'),
        ),
        migrations.AddField(
            model_name='emailsettings',
            name='telegram_quiet_to',
            field=models.TimeField(
                null=True, blank=True,
                verbose_name='Тихий режим до (година)',
                help_text='Не надсилати Telegram до цієї години. Напр. 08:00'),
        ),
        migrations.AddField(
            model_name='emailsettings',
            name='spam_folder',
            field=models.CharField(
                max_length=200, blank=True, default='Spam',
                verbose_name='Папка спаму на сервері',
                help_text='IONOS: Spam | Gmail: [Gmail]/Spam'),
        ),
        # EmailMessage: is_spam flag
        migrations.AddField(
            model_name='emailmessage',
            name='is_spam',
            field=models.BooleanField(default=False, verbose_name='Спам', db_index=True),
        ),
    ]
