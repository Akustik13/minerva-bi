from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('bots', '0026_digikey_messages'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='digikeyconfig',
            name='msg_notify_telegram',
        ),
        migrations.RemoveField(
            model_name='digikeyconfig',
            name='msg_notify_email',
        ),
    ]
