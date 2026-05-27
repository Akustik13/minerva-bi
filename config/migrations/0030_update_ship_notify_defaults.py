"""
Update customer (shipment) notification defaults:
- Subject: English template
- CC: company defaults
- EU body: full template with address
- Non-EU body: with customs warning
"""
from django.db import migrations

_SUBJECT = "Your order #{order_number} has been shipped"

_CC = "viacheslav.pryimak@sevskiy.de, sergey@sevskiy.de"

_BODY_EU = (
    "Dear {customer_name},\n\n"
    "We are pleased to inform you that your order #{order_number} has been shipped on {shipped_date}.\n\n"
    "Shipping details:\n"
    "Carrier: {carrier}\n"
    "Tracking Number: {tracking_number}\n\n"
    "Items shipped:\n"
    "{items}\n\n"
    "Delivery address:\n"
    "{ship_address}\n\n"
    "Thank you for your order and for choosing us.\n"
    "If you have any questions, please feel free to contact us.\n\n"
    "Best regards,\n"
    "Sevskiy GmbH Team"
)

_BODY_NONEU = (
    "Dear {customer_name},\n\n"
    "Your order #{order_number} has been shipped on {shipped_date}.\n\n"
    "Shipping details:\n"
    "Carrier: {carrier}\n"
    "Tracking Number: {tracking_number}\n\n"
    "Items shipped:\n"
    "{items}\n\n"
    "Delivery address:\n"
    "{ship_address}\n\n"
    "Please note: This shipment originates from the European Union. "
    "Depending on your country's import regulations, customs duties and/or import taxes "
    "may apply upon delivery. These charges are the responsibility of the recipient "
    "and are not included in the order price. We recommend checking with your local "
    "customs authority for further information.\n\n"
    "Best regards,\n"
    "Sevskiy GmbH Team"
)


def update_defaults(apps, schema_editor):
    NotificationSettings = apps.get_model('config', 'NotificationSettings')
    ns = NotificationSettings.objects.filter(pk=1).first()
    if not ns:
        return
    ns.customer_notify_subject      = _SUBJECT
    ns.customer_notify_cc           = _CC
    ns.customer_notify_body         = _BODY_EU
    ns.customer_notify_body_noneu   = _BODY_NONEU
    ns.save(update_fields=[
        'customer_notify_subject',
        'customer_notify_cc',
        'customer_notify_body',
        'customer_notify_body_noneu',
    ])


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0029_order_confirm_notify'),
    ]

    operations = [
        migrations.RunPython(update_defaults, migrations.RunPython.noop),
    ]
