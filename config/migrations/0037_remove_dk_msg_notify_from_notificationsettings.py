from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0036_dk_msg_notify'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='notificationsettings',
            name='dk_msg_notify_telegram',
        ),
        migrations.RemoveField(
            model_name='notificationsettings',
            name='dk_msg_notify_email',
        ),
        migrations.RemoveField(
            model_name='notificationsettings',
            name='dk_msg_notify_email_to',
        ),
    ]
