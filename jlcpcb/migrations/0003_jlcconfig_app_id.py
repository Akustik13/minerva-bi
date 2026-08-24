from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('jlcpcb', '0002_rename_credentials_add_log'),
    ]

    operations = [
        migrations.AddField(
            model_name='JLCConfig',
            name='app_id',
            field=models.CharField(
                blank=True, default='',
                help_text='JLCPCB Developer Portal → App ID (числовий, наприклад 614741474579288066)',
                max_length=200, verbose_name='App ID',
            ),
        ),
        migrations.AlterField(
            model_name='JLCConfig',
            name='secret_key',
            field=models.CharField(
                blank=True, default='',
                help_text='JLCPCB Developer Portal → Secret Key або Tokenization Key',
                max_length=500, verbose_name='Secret Key / Tokenization Key',
            ),
        ),
    ]
