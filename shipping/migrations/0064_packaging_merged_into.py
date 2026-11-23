from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('shipping', '0063_packaging_code_size_class'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='packagingmaterial',
            name='merged_into',
            field=models.ForeignKey(blank=True, help_text='Коробку злили в іншу («📊 Використання й схожі»): її відправлення рахуються й списуються як ціль. Повернули з архіву — знову окрема коробка.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='merged_from', to='shipping.packagingmaterial', verbose_name='Злито в'),
        ),
        migrations.AddField(
            model_name='packagingmaterial',
            name='merged_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Коли злито'),
        ),
        migrations.CreateModel(
            name='PackagingMerge',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Коли')),
                ('via', models.CharField(blank=True, default='', max_length=20, verbose_name='Звідки')),
                ('snapshot', models.JSONField(blank=True, default=dict, verbose_name='До злиття')),
                ('result', models.JSONField(blank=True, default=dict, verbose_name='Що зроблено')),
                ('undo_data', models.JSONField(blank=True, default=dict, verbose_name='Для відкату')),
                ('undone_at', models.DateTimeField(blank=True, null=True, verbose_name='Відкочено')),
                ('boxes', models.ManyToManyField(blank=True, related_name='merged_in', to='shipping.packagingmaterial', verbose_name='Злиті коробки')),
                ('target', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='merges', to='shipping.packagingmaterial', verbose_name='Ціль')),
                ('undone_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='Хто відкотив')),
                ('user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='Хто')),
            ],
            options={
                'verbose_name': 'Злиття коробок',
                'verbose_name_plural': 'Злиття коробок',
                'ordering': ['-created_at', '-pk'],
            },
        ),
    ]
