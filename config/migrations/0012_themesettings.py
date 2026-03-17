from django.db import migrations, models
import config.models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0011_sync_skip_if_no_changes'),
    ]

    operations = [
        migrations.CreateModel(
            name='ThemeSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('bg_app',    models.CharField(blank=True, default='', max_length=20, validators=[config.models._validate_hex], verbose_name='Фон сторінки (--bg-app)')),
                ('bg_card',   models.CharField(blank=True, default='', max_length=20, validators=[config.models._validate_hex], verbose_name='Фон картки (--bg-card)')),
                ('bg_card_2', models.CharField(blank=True, default='', max_length=20, validators=[config.models._validate_hex], verbose_name='Фон картки-2 (--bg-card-2)')),
                ('bg_input',  models.CharField(blank=True, default='', max_length=20, validators=[config.models._validate_hex], verbose_name='Фон інпуту (--bg-input)')),
                ('bg_hover',  models.CharField(blank=True, default='', max_length=20, validators=[config.models._validate_hex], verbose_name='Hover фон (--bg-hover)')),
                ('text_primary', models.CharField(blank=True, default='', max_length=20, validators=[config.models._validate_hex], verbose_name='Основний текст (--text)')),
                ('text_muted',   models.CharField(blank=True, default='', max_length=20, validators=[config.models._validate_hex], verbose_name='Другорядний текст (--text-muted)')),
                ('text_dim',     models.CharField(blank=True, default='', max_length=20, validators=[config.models._validate_hex], verbose_name='Приглушений текст (--text-dim)')),
                ('accent',  models.CharField(blank=True, default='', max_length=20, validators=[config.models._validate_hex], verbose_name='Акцент/синій (--accent)')),
                ('gold',    models.CharField(blank=True, default='', max_length=20, validators=[config.models._validate_hex], verbose_name='Золотий акцент (--gold)')),
                ('gold_l',  models.CharField(blank=True, default='', max_length=20, validators=[config.models._validate_hex], verbose_name='Золотий світлий (--gold-l)')),
                ('ok',   models.CharField(blank=True, default='', max_length=20, validators=[config.models._validate_hex], verbose_name='Успіх/зелений (--ok)')),
                ('warn', models.CharField(blank=True, default='', max_length=20, validators=[config.models._validate_hex], verbose_name='Увага/помаранч (--warn)')),
                ('err',  models.CharField(blank=True, default='', max_length=20, validators=[config.models._validate_hex], verbose_name='Помилка/червоний (--err)')),
            ],
            options={
                'verbose_name': 'Тема кольорів (Custom)',
                'verbose_name_plural': 'Тема кольорів (Custom)',
            },
        ),
    ]
