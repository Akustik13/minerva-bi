# Generated manually 2026-04-07

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('shipping', '0018_shipment_insurance_type'),
    ]

    operations = [
        migrations.CreateModel(
            name='ShipmentPackage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('weight_kg', models.DecimalField(decimal_places=3, default=1, max_digits=8, verbose_name='Вага (кг)')),
                ('length_cm', models.DecimalField(decimal_places=1, default=30, max_digits=6, verbose_name='Довжина (см)')),
                ('width_cm', models.DecimalField(decimal_places=1, default=20, max_digits=6, verbose_name='Ширина (см)')),
                ('height_cm', models.DecimalField(decimal_places=1, default=15, max_digits=6, verbose_name='Висота (см)')),
                ('quantity', models.PositiveSmallIntegerField(default=1, help_text='Кількість коробок з однаковими розмірами та вагою', verbose_name='Однакових коробок')),
                ('shipment', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='packages', to='shipping.shipment', verbose_name='Відправлення')),
            ],
            options={
                'verbose_name': 'Коробка',
                'verbose_name_plural': 'Коробки',
                'ordering': ['pk'],
            },
        ),
    ]
