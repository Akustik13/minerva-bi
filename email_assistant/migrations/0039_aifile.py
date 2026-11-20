import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('email_assistant', '0038_mail_app_device'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='AIFile',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('file', models.CharField(help_text='Шлях відносно MEDIA_ROOT (email_ai/<скринька>/…)', max_length=300)),
                ('name', models.CharField(max_length=255, verbose_name='Назва')),
                ('content_type', models.CharField(default='application/octet-stream', max_length=100)),
                ('kind', models.CharField(choices=[('image', 'Зображення'), ('pdf', 'PDF'), ('docx', 'Word (DOCX)'), ('text', 'Текст (TXT / CSV)')], max_length=10)),
                ('size', models.PositiveIntegerField(default=0)),
                ('text', models.TextField(blank=True, default='', help_text='Текст PDF / DOCX / TXT / CSV для AI')),
                ('info', models.CharField(blank=True, default='', help_text='«12 стор.», «1200×900»', max_length=120)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('account', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='email_assistant.emailaccount', verbose_name='Скринька')),
                ('message', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='email_assistant.emailmessage', verbose_name='Лист, на який відповідали')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='Хто додав')),
            ],
            options={
                'verbose_name': 'Файл до запиту AI',
                'verbose_name_plural': '📎 Файли до запитів AI',
                'ordering': ['created_at', 'pk'],
            },
        ),
    ]
