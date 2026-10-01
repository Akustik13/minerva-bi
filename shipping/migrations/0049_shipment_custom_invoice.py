# Generated migration

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipping', '0048_shipmentpackage_items_distribution'),
    ]

    operations = [
        migrations.AddField(
            model_name='shipment',
            name='use_custom_invoice',
            field=models.BooleanField(
                default=False,
                help_text='Якщо вкл — завантажити свій PDF інвойс замість UPS автогенерації',
                verbose_name='Використовувати свій інвойс'
            ),
        ),
        migrations.AddField(
            model_name='shipment',
            name='custom_invoice_pdf',
            field=models.FileField(
                blank=True,
                help_text="PDF інвойс/CN23/CN22 вашої компанії",
                null=True,
                upload_to='shipments/invoices/',
                verbose_name='Файл інвойсу (PDF)'
            ),
        ),
        migrations.AddField(
            model_name='shipment',
            name='ups_document_id',
            field=models.CharField(
                blank=True,
                default='',
                help_text='ID документу з Paperless Document API (заповнюється автоматично)',
                max_length=100,
                verbose_name='UPS Document ID'
            ),
        ),
    ]
