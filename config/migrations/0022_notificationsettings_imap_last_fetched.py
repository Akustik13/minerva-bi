from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0021_briefingsettings'),
    ]

    operations = [
        migrations.AddField(
            model_name='notificationsettings',
            name='imap_last_fetched',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Останнє оновлення пошти'),
        ),
    ]
