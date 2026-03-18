# Generated manually 2026-03-18

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipping', '0017_shipment_carrier_status_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='shipment',
            name='insurance_type',
            field=models.CharField(
                choices=[
                    ('none',     'Ohne Versicherung — базова відповідальність перевізника (безкоштовно)'),
                    ('standard', 'Standard — стандартне страхування до задекларованої вартості'),
                    ('premium',  'Premium — підвищене страхування (повне покриття)'),
                ],
                default='none',
                help_text="Тип страхування посилки у Jumingo. 'Ohne' = тільки базова відповідальність.",
                max_length=20,
                verbose_name='Страхування',
            ),
        ),
    ]
