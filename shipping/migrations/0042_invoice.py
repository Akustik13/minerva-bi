from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('shipping', '0041_incoming_shipment'),
        ('sales', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Invoice',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('digikey_order_no', models.CharField(db_index=True, max_length=50, verbose_name='DigiKey Order No.')),
                ('invoice_number', models.CharField(max_length=20, unique=True, verbose_name='Номер інвойсу')),
                ('invoice_date', models.DateField(auto_now_add=True, verbose_name='Дата інвойсу')),
                ('order_date', models.DateField(verbose_name='Дата замовлення')),
                ('shipment_date', models.DateField(blank=True, null=True, verbose_name='Дата відправки')),
                ('subtotal', models.DecimalField(decimal_places=2, default=0, max_digits=10, verbose_name='Subtotal')),
                ('discount_amount', models.DecimalField(decimal_places=2, default=0, max_digits=10, verbose_name='Знижка')),
                ('shipping_charges', models.DecimalField(decimal_places=2, default=0, max_digits=10, verbose_name='Доставка')),
                ('vat_amount', models.DecimalField(decimal_places=2, default=0, max_digits=10, verbose_name='ПДВ (VAT)')),
                ('total_amount', models.DecimalField(decimal_places=2, default=0, max_digits=10, verbose_name='Разом з ПДВ')),
                ('shipped_to_company', models.CharField(max_length=200, verbose_name='Компанія отримувача')),
                ('shipped_to_vat', models.CharField(blank=True, default='', max_length=50, verbose_name='VAT ID отримувача')),
                ('docx_file', models.FileField(blank=True, null=True, upload_to='invoices/', verbose_name='Файл .docx')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Створено')),
                ('sales_order', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='invoices', to='sales.salesorder', verbose_name='Замовлення')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL, verbose_name='Автор')),
            ],
            options={
                'verbose_name': 'Інвойс',
                'verbose_name_plural': 'Інвойси',
                'ordering': ['-invoice_number'],
            },
        ),
    ]
