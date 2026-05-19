from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0013_userprofile_created_by'),
    ]

    operations = [
        migrations.AlterField(
            model_name='userprofile',
            name='interface_language',
            field=models.CharField(
                choices=[
                    ('uk', 'Українська'),
                    ('en', 'English'),
                    ('de', 'Deutsch'),
                    ('ru', 'Русский'),
                ],
                default='uk',
                max_length=5,
                verbose_name='Мова інтерфейсу',
            ),
        ),
    ]
