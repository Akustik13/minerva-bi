from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0033_dk_notify_content_flags'),
    ]

    operations = [
        migrations.AddField(
            model_name='notificationsettings',
            name='notify_include_images',
            field=models.BooleanField(
                default=True,
                verbose_name='🖼️ Фото товарів у Telegram',
                help_text='Надсилати зображення товарів після текстового повідомлення. '
                          'Кілька товарів — альбомом (до 10 фото).',
            ),
        ),
    ]
