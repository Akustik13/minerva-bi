from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0011_alter_customer_segment'),
    ]

    operations = [
        migrations.AddField(
            model_name='customer',
            name='is_favorite',
            field=models.BooleanField(db_index=True, default=False, verbose_name='⭐ Обраний'),
        ),
    ]
