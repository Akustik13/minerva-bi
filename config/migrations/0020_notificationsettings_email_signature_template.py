from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0019_notificationsettings_imap'),
    ]

    operations = [
        migrations.AddField(
            model_name='notificationsettings',
            name='email_signature_template',
            field=models.TextField(
                blank=True,
                default='З повагою,\n{name}',
                help_text="Підпис, що автоматично додається до листів з CRM. {name} замінюється на ім'я поточного користувача.",
                verbose_name='Шаблон підпису',
            ),
        ),
    ]
