from django.db import migrations


def create_cable_template(apps, schema_editor):
    RFQEmailTemplate = apps.get_model('inventory', 'RFQEmailTemplate')
    ProductCategory  = apps.get_model('inventory', 'ProductCategory')

    # Try to find a cable category by common slugs
    cable_cat = (
        ProductCategory.objects.filter(slug__in=['cable', 'cables', 'кабель', 'кабелі'])
        .first()
    )

    if not RFQEmailTemplate.objects.filter(use_cable_columns=True).exists():
        RFQEmailTemplate.objects.create(
            name='Cable RFQ',
            category=cable_cat,          # None = default fallback if no category found
            subject='Order Request',
            greeting='Hi {contact_person},',
            intro='I have a new urgent RFQ:',
            signature='Best regards,',
            footer_note=(
                'Please note that the cable length L is measured '
                'between the centers of the connectors.'
            ),
            use_cable_columns=True,
        )


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0031_rfqemailtemplate'),
    ]

    operations = [
        migrations.RunPython(create_cable_template, migrations.RunPython.noop),
    ]
