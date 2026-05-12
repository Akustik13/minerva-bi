from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('email_assistant', '0010_emailsettings_autoreply_order_trigger'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='EmailContact',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('email', models.EmailField()),
                ('name', models.CharField(blank=True, max_length=200)),
                ('use_count', models.PositiveIntegerField(default=1)),
                ('last_used_at', models.DateTimeField(auto_now=True)),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='email_contacts',
                    to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Контакт адресної книги',
                'verbose_name_plural': 'Адресна книга',
                'ordering': ['-use_count', '-last_used_at'],
            },
        ),
        migrations.AddConstraint(
            model_name='emailcontact',
            constraint=models.UniqueConstraint(
                fields=['user', 'email'],
                name='unique_user_email_contact'),
        ),
    ]
