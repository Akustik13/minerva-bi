from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('api', '0007_agent_user'),
    ]

    operations = [
        migrations.AddField(
            model_name='apikey',
            name='owner',
            field=models.ForeignKey(blank=True, editable=False, help_text='Хто створив ключ.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='Власник'),
        ),
        migrations.AddField(
            model_name='apikey',
            name='private',
            field=models.BooleanField(default=False, help_text='Повний ключ бачить і змінює лише власник; інші бачать лише початок і кінець, вимкнути чи видалити можуть (на випадок витоку). Лише особистий ключ з «🤖 Агент (MCP)» = власник дає агенту вашу пошту, календар і робочий час. Не ставте для ключа сайту магазину, RAG чи інтеграцій — їх ведуть кілька людей.', verbose_name='🔒 Лише я (особистий ключ)'),
        ),
    ]
