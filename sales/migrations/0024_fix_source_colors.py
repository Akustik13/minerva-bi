from django.db import migrations

SOURCE_COLORS = {
    'digikey':   '#c62828',  # DigiKey brand red
    'nova_post': '#1565c0',  # Nova Post blue
    'manual':    '#455a64',  # Steel grey
    'amazon':    '#e65100',  # Amazon deep orange
    'ebay':      '#1565c0',  # eBay blue
    'webshop':   '#2e7d32',  # Green
}


def fix_colors(apps, schema_editor):
    SalesSource = apps.get_model('sales', 'SalesSource')
    for slug, color in SOURCE_COLORS.items():
        SalesSource.objects.filter(slug=slug).update(color=color)


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0023_addr_state_max_length'),
    ]

    operations = [
        migrations.RunPython(fix_colors, migrations.RunPython.noop),
    ]
