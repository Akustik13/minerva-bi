from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0010_userprofile_imap'),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='smtp_host',
            field=models.CharField(
                blank=True, max_length=255,
                verbose_name='SMTP Host',
                help_text='ionos: smtp.ionos.de | Gmail: smtp.gmail.com',
            ),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='smtp_port',
            field=models.PositiveSmallIntegerField(
                default=587,
                verbose_name='SMTP Port',
                help_text='TLS: 587 | SSL: 465',
            ),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='smtp_use_tls',
            field=models.BooleanField(
                default=True,
                verbose_name='TLS (STARTTLS)',
                help_text='Зазвичай порт 587. Вимкни якщо використовуєш SSL (порт 465).',
            ),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='smtp_use_ssl',
            field=models.BooleanField(
                default=False,
                verbose_name='SSL',
                help_text='Зазвичай порт 465. Несумісне з TLS — увімкни лише одне.',
            ),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='smtp_user',
            field=models.CharField(
                blank=True, max_length=255,
                verbose_name='SMTP Login (email)',
            ),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='smtp_password',
            field=models.CharField(
                blank=True, max_length=255,
                verbose_name='SMTP Password',
            ),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='smtp_from',
            field=models.CharField(
                blank=True, max_length=255,
                verbose_name='Від кого (From)',
                help_text="Ім'я та email: \"Іван Петренко <ivan@example.com>\". Якщо порожньо — використовується SMTP Login.",
            ),
        ),
    ]
