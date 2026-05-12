"""Migration 0008 — make allowed_modules nullable (None = use role defaults)."""
from django.db import migrations, models


def empty_to_null(apps, schema_editor):
    UserProfile = apps.get_model('core', 'UserProfile')
    UserProfile.objects.filter(allowed_modules=[]).update(allowed_modules=None)


def null_to_empty(apps, schema_editor):
    UserProfile = apps.get_model('core', 'UserProfile')
    UserProfile.objects.filter(allowed_modules__isnull=True).update(allowed_modules=[])


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
        migrations.RunPython(empty_to_null, reverse_code=null_to_empty),
    ]
