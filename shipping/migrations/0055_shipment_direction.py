"""📤 / 📥 Напрямок відправлення і прив'язка вхідної посилки до закупівлі (PO). Посилки JLCPCB — «до нас»."""
import django.db.models.deletion
from django.db import migrations, models


def jlc_incoming(apps, schema_editor):
    Shipment = apps.get_model("shipping", "Shipment")
    Shipment.objects.filter(jlc_order__isnull=False).update(direction="in")


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0043_price_history_details'),
        ('shipping', '0054_shop_confirm_prompt'),
    ]

    operations = [
        migrations.AddField(
            model_name='shipment',
            name='direction',
            field=models.CharField(choices=[('out', '📤 Від нас'), ('in', '📥 До нас')], db_index=True, default='out',
                                   max_length=3, verbose_name='Напрямок'),
        ),
        migrations.AddField(
            model_name='shipment',
            name='purchase_order',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name='parcels', to='inventory.purchaseorder',
                                    verbose_name='Закупівля (PO)',
                                    help_text='Вхідна посилка від постачальника: що в ній — позиції закупівлі; '
                                              'прибула — оприбуткуйте PO.'),
        ),
        migrations.RunPython(jlc_incoming, migrations.RunPython.noop),
    ]
