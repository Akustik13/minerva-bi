from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0007_migrate_customer_addresses'),
    ]

    operations = [
        migrations.AddField(
            model_name='customernote',
            name='due_date',
            field=models.DateField(
                blank=True, null=True,
                verbose_name='Дедлайн нагадування',
                help_text='Заповни для типу ⏰ Нагадування → авто-Task',
            ),
        ),
        migrations.AlterField(
            model_name='customernote',
            name='note_type',
            field=models.CharField(
                choices=[
                    ('call', 'Дзвінок'),
                    ('email', 'Email'),
                    ('meeting', 'Зустріч'),
                    ('note', 'Нотатка'),
                    ('reminder', '⏰ Нагадування'),
                    ('other', 'Інше'),
                ],
                default='note', max_length=20, verbose_name='Тип',
            ),
        ),
    ]
