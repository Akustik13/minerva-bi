from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('email_assistant', '0012_emailaccount_sync_limit_sync_no_limit'),
    ]

    operations = [
        migrations.CreateModel(
            name='EmailRule',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=200, verbose_name='Назва правила')),
                ('condition_field', models.CharField(
                    choices=[
                        ('from_email', 'Відправник (email)'),
                        ('from_name', "Відправник (ім'я)"),
                        ('subject', 'Тема'),
                        ('body', 'Текст листа'),
                        ('to_email', 'Кому (email)'),
                    ],
                    default='from_email', max_length=20, verbose_name='Поле')),
                ('condition_op', models.CharField(
                    choices=[
                        ('contains', 'Містить'),
                        ('equals', 'Рівно'),
                        ('starts_with', 'Починається з'),
                        ('ends_with', 'Закінчується на'),
                        ('not_contains', 'Не містить'),
                    ],
                    default='contains', max_length=20, verbose_name='Умова')),
                ('condition_value', models.CharField(max_length=500, verbose_name='Значення')),
                ('action', models.CharField(
                    choices=[
                        ('mark_read', 'Позначити прочитаним'),
                        ('mark_spam', 'Позначити спамом'),
                        ('move_folder', 'Перемістити до папки'),
                        ('star', 'Позначити зірочкою'),
                        ('trash', 'Видалити'),
                    ],
                    default='mark_read', max_length=20, verbose_name='Дія')),
                ('action_value', models.CharField(
                    blank=True, max_length=200,
                    help_text='Для "move_folder" — назва папки IMAP',
                    verbose_name='Параметр дії')),
                ('is_active', models.BooleanField(default=True, verbose_name='Активне')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('account', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='rules',
                    to='email_assistant.emailaccount')),
            ],
            options={
                'verbose_name': 'Правило пошти',
                'verbose_name_plural': 'Правила пошти',
                'ordering': ['name'],
            },
        ),
    ]
