"""Migration 0009 — add module_operations JSONField to UserProfile."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0008_userprofile_allowed_modules_nullable'),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='module_operations',
            field=models.JSONField(
                null=True, blank=True, default=None,
                verbose_name='Операції по модулях',
                help_text=(
                    'None = авто (роль). '
                    '{"crm": ["view","add","change"]} = явні операції для модуля. '
                    '{} = повна заборона всіх операцій.'
                ),
            ),
        ),
    ]
