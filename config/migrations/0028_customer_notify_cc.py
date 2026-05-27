from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0027_customer_notify_body_noneu'),
    ]

    operations = [
        migrations.AddField(
            model_name='notificationsettings',
            name='customer_notify_cc',
            field=models.CharField(
                blank=True,
                help_text='Email-адреси через кому — автоматично підставляються в поле CC при надсиланні клієнту.',
                max_length=500,
                verbose_name='CC (копія) за замовчуванням',
            ),
        ),
    ]
