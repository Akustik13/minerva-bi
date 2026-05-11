from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('email_assistant', '0002_emailaccount_extras_emailsettings_extras'),
    ]

    operations = [
        migrations.AddField(
            model_name='emailmessage',
            name='imap_folder_name',
            field=models.CharField(
                max_length=200, blank=True, default='', db_index=True,
                verbose_name='Папка IMAP',
                help_text='Оригінальна назва папки на IMAP-сервері'),
        ),
    ]
