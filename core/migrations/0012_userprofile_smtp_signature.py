from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0011_userprofile_smtp'),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='smtp_signature',
            field=models.TextField(
                blank=True,
                default='',
                verbose_name='Підпис листа',
                help_text="{name} замінюється на ім'я користувача при відправці.",
            ),
        ),
    ]
