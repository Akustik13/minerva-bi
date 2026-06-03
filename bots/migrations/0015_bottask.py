from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bots', '0014_digikeylisting_category_default_other'),
    ]

    operations = [
        migrations.CreateModel(
            name='BotTask',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=64, unique=True)),
                ('status', models.CharField(default='idle', max_length=16)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
                ('progress', models.CharField(blank=True, default='', max_length=300)),
                ('message', models.TextField(blank=True, default='')),
                ('cancel_requested', models.BooleanField(default=False)),
            ],
            options={
                'verbose_name': 'Background Task',
            },
        ),
    ]
