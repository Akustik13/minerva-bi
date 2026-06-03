from django.db import migrations


def backfill_price_min(apps, schema_editor):
    DigiKeyListing = apps.get_model('bots', 'DigiKeyListing')
    to_update = []
    for listing in DigiKeyListing.objects.only('id', 'dk_prices', 'dk_price_min'):
        try:
            tiers = [t for t in (listing.dk_prices or []) if t.get('price') not in (None, '')]
            if tiers:
                min_tier = min(tiers, key=lambda t: int(t.get('qty') or 0))
                listing.dk_price_min = float(min_tier['price'])
            else:
                listing.dk_price_min = None
        except Exception:
            listing.dk_price_min = None
        to_update.append(listing)

    if to_update:
        DigiKeyListing.objects.bulk_update(to_update, ['dk_price_min'], batch_size=200)


class Migration(migrations.Migration):

    dependencies = [
        ('bots', '0020_digikeylisting_dk_price_min'),
    ]

    operations = [
        migrations.RunPython(backfill_price_min, migrations.RunPython.noop),
    ]
