from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('shop', '0038_mail_letters'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='shop',
            name='mail_bcc',
        ),
    ]
