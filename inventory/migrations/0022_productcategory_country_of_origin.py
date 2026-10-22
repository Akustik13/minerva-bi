from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Те саме поле вже додала 0021, тому для бази тут нічого не робиться
    (на новій PostgreSQL-базі повторний AddField падав «column already exists»). Опис моделі — як і раніше.
    """

    dependencies = [
        ('inventory', '0021_productcategory_customs'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name='productcategory',
                    name='customs_country_of_origin',
                    field=models.CharField(
                        blank=True, default='DE', max_length=2,
                        help_text='Дефолт для товарів категорії без заданої країни. ISO-2: DE, UA, CN, US…',
                        verbose_name='Країна походження (ISO 2)',
                    ),
                ),
            ],
            database_operations=[],
        ),
    ]
