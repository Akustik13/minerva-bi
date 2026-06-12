from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bots', '0027_remove_digikeyconfig_msg_notify_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='digikeyconfig',
            name='msg_topics_cache',
            field=models.JSONField(blank=True, default=None, help_text='Зберігається автоматично після check_digikey_messages або оновлення в хабі.', null=True, verbose_name='Кеш повідомлень'),
        ),
        migrations.AddField(
            model_name='digikeyconfig',
            name='msg_cache_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Кеш оновлено'),
        ),
    ]
