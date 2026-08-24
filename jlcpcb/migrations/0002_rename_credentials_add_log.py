from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('jlcpcb', '0001_initial'),
    ]

    operations = [
        migrations.RenameField('JLCConfig', 'api_key',    'access_key'),
        migrations.RenameField('JLCConfig', 'api_secret', 'secret_key'),
        migrations.AlterField(
            model_name='JLCConfig',
            name='access_key',
            field=models.CharField(
                blank=True, default='',
                help_text='JLCPCB Developer Portal → Access Key',
                max_length=500, verbose_name='Access Key',
            ),
        ),
        migrations.AlterField(
            model_name='JLCConfig',
            name='secret_key',
            field=models.CharField(
                blank=True, default='',
                help_text='JLCPCB Developer Portal → Secret Key',
                max_length=500, verbose_name='Secret Key',
            ),
        ),
        migrations.AddField(
            model_name='JLCConfig',
            name='connection_log',
            field=models.TextField(
                blank=True, default='',
                verbose_name='Лог підключення',
                help_text='Результат останнього тесту підключення / синхронізації',
            ),
        ),
    ]
