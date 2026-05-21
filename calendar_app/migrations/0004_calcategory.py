from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('calendar_app', '0003_merge'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='CalendarCategory',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, verbose_name='Назва')),
                ('color', models.CharField(default='#607d8b', max_length=7, verbose_name='Колір (hex)')),
                ('emoji', models.CharField(blank=True, default='📌', max_length=10, verbose_name='Іконка')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='cal_categories', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Категорія календаря',
                'verbose_name_plural': 'Категорії календаря',
                'ordering': ['name'],
                'unique_together': {('user', 'name')},
            },
        ),
        migrations.AddField(
            model_name='calendarevent',
            name='custom_category',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='events', to='calendar_app.calendarcategory', verbose_name='Власна категорія'),
        ),
    ]
