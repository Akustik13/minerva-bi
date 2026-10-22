from django.db import migrations


class Migration(migrations.Migration):
    """
    Колишній ручний «ADD COLUMN IF NOT EXISTS shipping_cost» для робочої бази.
    Стовпець shipping_cost створює 0014 (AddField), а на новій базі цей SQL ламав 0014
    («column already exists»), тому тут більше нічого не робиться.
    """

    dependencies = [
        ('sales', '0010_add_price_fields_force'),
    ]

    operations = []
