"""
Update order confirmation notification defaults in existing pk=1 record:
- CC: company defaults
- Sources filter: digikey
- Subject and body: already correct in model defaults, ensure populated
"""
from django.db import migrations

_SUBJECT = "Your order #{order_number} has been received"

_BODY = (
    "Dear {customer_name},\n\n"
    "Thank you for your order #{order_number} placed on {order_date}.\n\n"
    "We have received your order and it is currently being processed.\n\n"
    "Items ordered:\n"
    "{items}\n\n"
    "If you have any questions, please feel free to contact us.\n\n"
    "Best regards,\n"
    "Sevskiy GmbH Team"
)

_CC = "viacheslav.pryimak@sevskiy.de, sergey@sevskiy.de"
_SOURCES = "digikey"


def update_defaults(apps, schema_editor):
    NS = apps.get_model('config', 'NotificationSettings')
    ns = NS.objects.filter(pk=1).first()
    if not ns:
        return
    # Update fields that are currently empty or wrong
    update_fields = []
    if not ns.order_confirm_notify_cc:
        ns.order_confirm_notify_cc = _CC
        update_fields.append('order_confirm_notify_cc')
    if not ns.order_confirm_notify_sources:
        ns.order_confirm_notify_sources = _SOURCES
        update_fields.append('order_confirm_notify_sources')
    if not ns.order_confirm_notify_subject:
        ns.order_confirm_notify_subject = _SUBJECT
        update_fields.append('order_confirm_notify_subject')
    if not ns.order_confirm_notify_body:
        ns.order_confirm_notify_body = _BODY
        update_fields.append('order_confirm_notify_body')
    if update_fields:
        ns.save(update_fields=update_fields)


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0030_update_ship_notify_defaults'),
    ]

    operations = [
        migrations.RunPython(update_defaults, migrations.RunPython.noop),
    ]
