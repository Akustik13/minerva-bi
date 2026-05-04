"""Migration 0008 — make allowed_modules nullable (None = use role defaults)."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0007_userprofile_personal_settings'),
    ]

    operations = [
        migrations.AlterField(
            model_name='userprofile',
            name='allowed_modules',
            field=models.JSONField(
                null=True, blank=True, default=None,
                verbose_name='Ручний список модулів',
                help_text='None = авто (роль/пакет). [] = заблокувати все крім ядра. [...] = явний список.',
            ),
        ),
        # Data migration: convert [] → None so existing users use role defaults
        migrations.RunSQL(
            sql="""
                UPDATE core_userprofile
                SET allowed_modules = NULL
                WHERE allowed_modules::text = '[]';
            """,
            reverse_sql="""
                UPDATE core_userprofile
                SET allowed_modules = '[]'
                WHERE allowed_modules IS NULL;
            """,
        ),
    ]
