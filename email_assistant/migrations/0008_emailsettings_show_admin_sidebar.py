from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('email_assistant', '0007_fix_unique_imap_folder_name'),
    ]
    operations = [
        migrations.AddField(
            model_name='emailsettings',
            name='show_admin_sidebar',
            field=models.BooleanField(
                default=True,
                verbose_name='Показувати панель навігації Minerva',
                help_text='Відображати ліве меню системи на сторінках Email асистента',
            ),
        ),
    ]
