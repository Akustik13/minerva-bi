"""
Data migration: pre-populate ship_* fields from existing contact/client/phone/email
so that existing orders keep working correctly with copy_from_order().
"""
from django.db import migrations


def backfill_ship_fields(apps, schema_editor):
    SalesOrder = apps.get_model('sales', 'SalesOrder')
    batch = []
    for o in SalesOrder.objects.all().only(
        'id', 'contact_name', 'client', 'phone', 'email',
        'ship_name', 'ship_company', 'ship_phone', 'ship_email',
    ):
        if o.contact_name:
            # B2B: contact_name = person, client = company
            o.ship_name    = o.contact_name
            o.ship_company = o.client
        else:
            # B2C: client = person/company name
            o.ship_name    = o.client
            o.ship_company = ''
        o.ship_phone = o.phone
        o.ship_email = o.email
        batch.append(o)
        if len(batch) >= 500:
            SalesOrder.objects.bulk_update(
                batch, ['ship_name', 'ship_company', 'ship_phone', 'ship_email']
            )
            batch = []
    if batch:
        SalesOrder.objects.bulk_update(
            batch, ['ship_name', 'ship_company', 'ship_phone', 'ship_email']
        )


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0033_ship_recipient_fields'),
    ]

    operations = [
        migrations.RunPython(backfill_ship_fields, migrations.RunPython.noop),
    ]
