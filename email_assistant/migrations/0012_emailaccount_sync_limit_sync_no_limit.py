from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('email_assistant', '0011_emailcontact'),
    ]

    operations = [
        migrations.AddField(
            model_name='emailaccount',
            name='sync_limit',
            field=models.PositiveIntegerField(
                default=200,
                verbose_name='Ліміт листів на папку',
                help_text='Скільки листів максимум завантажувати на одну папку за раз.'),
        ),
        migrations.AddField(
            model_name='emailaccount',
            name='sync_no_limit',
            field=models.BooleanField(
                default=False,
                verbose_name='Без ліміту',
                help_text='Якщо увімкнено — завантажує всі листи (ігнорує ліміт). Може бути повільно для великих папок.'),
        ),
    ]
