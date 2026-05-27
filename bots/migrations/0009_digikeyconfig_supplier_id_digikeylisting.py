from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('bots', '0008_digikeyconfig_sync_order_status'),
        ('inventory', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='digikeyconfig',
            name='marketplace_supplier_id',
            field=models.CharField(
                blank=True, default='',
                help_text='UUID що DigiKey призначає постачальнику в Marketplace Portal '
                          '(My Account → Supplier Info → Supplier ID)',
                max_length=36,
                verbose_name='Marketplace Supplier UUID',
            ),
        ),
        migrations.CreateModel(
            name='DigiKeyListing',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('product', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='dk_listing',
                    to='inventory.product',
                    verbose_name='Товар',
                )),
                ('category_type', models.CharField(
                    choices=[('filter', 'RF Filter'), ('cable', 'Cable Assembly'), ('other', 'Інше')],
                    default='filter', max_length=20, verbose_name='Категорія',
                )),
                # Product stage
                ('dk_product_id',   models.CharField(blank=True, default='', max_length=36, verbose_name='DK Product ID')),
                ('dk_category_id',  models.CharField(blank=True, default='', max_length=256, verbose_name='DK Category ID')),
                ('dk_title',        models.CharField(max_length=50, verbose_name='Назва (DK)')),
                ('dk_description',  models.TextField(max_length=2048, verbose_name='Опис товару (DK)')),
                ('dk_manufacturer', models.CharField(blank=True, default='', max_length=50, verbose_name='Виробник (DK)')),
                ('dk_image_url',    models.URLField(blank=True, default='', verbose_name='Фото (URL)')),
                ('dk_datasheet_url', models.URLField(blank=True, default='', verbose_name='Datasheet (URL)')),
                # Offer
                ('dk_offer_id',       models.CharField(blank=True, default='', max_length=36, verbose_name='DK Offer ID')),
                ('dk_supplier_sku',   models.CharField(blank=True, default='', max_length=50, verbose_name='Supplier SKU (DK)')),
                ('dk_min_order_qty',  models.PositiveIntegerField(default=1, verbose_name='MOQ (мін. кількість)')),
                ('dk_lead_time_days', models.PositiveIntegerField(default=11, verbose_name='Термін відвантаження (дні)')),
                ('dk_qty_alert',      models.PositiveIntegerField(default=3, verbose_name='Мін. залишок (алерт)')),
                ('dk_is_active',      models.BooleanField(default=True, verbose_name='Активне на DigiKey')),
                # Pricing
                ('dk_prices', models.JSONField(default=list, verbose_name='Цінові тири')),
                # Filter attributes
                ('fa_frequency',      models.CharField(blank=True, default='', max_length=100, verbose_name='Frequency (139)')),
                ('fa_bandwidth',      models.CharField(blank=True, default='', max_length=100, verbose_name='Bandwidth (398)')),
                ('fa_filter_type',    models.CharField(blank=True, default='', max_length=100, verbose_name='Filter Type (21)')),
                ('fa_ripple',         models.CharField(blank=True, default='', max_length=100, verbose_name='Ripple (428)')),
                ('fa_insertion_loss', models.CharField(blank=True, default='', max_length=100, verbose_name='Insertion Loss (327)')),
                ('fa_mounting_type',  models.CharField(blank=True, default='', max_length=100, verbose_name='Mounting Type (69)')),
                ('fa_package_case',   models.CharField(blank=True, default='', max_length=100, verbose_name='Package / Case (16)')),
                ('fa_size_dimension', models.CharField(blank=True, default='', max_length=200, verbose_name='Size / Dimension (46)')),
                ('fa_height_max',     models.CharField(blank=True, default='', max_length=100, verbose_name='Height Max (966)')),
                # Sync
                ('sync_status',    models.CharField(
                    choices=[('draft', 'Чернетка'), ('published', 'Опубліковано'), ('error', 'Помилка')],
                    default='draft', max_length=20, verbose_name='Статус',
                )),
                ('last_synced_at', models.DateTimeField(blank=True, null=True, verbose_name='Остання синхронізація')),
                ('last_error',     models.TextField(blank=True, default='', verbose_name='Остання помилка')),
                ('created_at',     models.DateTimeField(auto_now_add=True)),
                ('updated_at',     models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'DigiKey — Лістинг',
                'verbose_name_plural': 'DigiKey — Лістинги',
                'ordering': ['product__sku'],
            },
        ),
    ]
