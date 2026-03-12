from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0019_product_customs_fields'),
        ('sales', '0019_rename_sales_order_date_idx_sales_sales_order_d_55e46c_idx_and_more'),
        ('shipping', '0002_alter_carrier_api_key_alter_carrier_api_secret_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='PackagingMaterial',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(blank=True, default='', help_text='Залиш порожнім — заповниться автоматично з розмірів', max_length=255, verbose_name='Назва')),
                ('box_type', models.CharField(
                    choices=[('box', '📦 Коробка'), ('envelope', '✉️ Конверт'),
                             ('tube', '🗄️ Тубус'), ('bag', '🛍️ Пакет'), ('custom', '⚙️ Інше')],
                    default='box', max_length=20, verbose_name='Тип',
                )),
                ('length_mm', models.PositiveIntegerField(verbose_name='Довжина (мм)')),
                ('width_mm', models.PositiveIntegerField(verbose_name='Ширина (мм)')),
                ('height_mm', models.PositiveIntegerField(verbose_name='Висота (мм)')),
                ('tare_weight_g', models.PositiveIntegerField(default=0, help_text='Вага самої коробки без вмісту', verbose_name='Вага порожньої (г)')),
                ('max_weight_g', models.PositiveIntegerField(blank=True, help_text='Максимально допустима вага товарів', null=True, verbose_name='Макс. вага вмісту (г)')),
                ('cost', models.DecimalField(blank=True, decimal_places=2, max_digits=8, null=True, verbose_name='Вартість за шт')),
                ('notes', models.TextField(blank=True, default='', verbose_name='Нотатки')),
                ('is_active', models.BooleanField(default=True, verbose_name='Активна')),
            ],
            options={
                'verbose_name': 'Пакувальний матеріал',
                'verbose_name_plural': 'Пакувальні матеріали',
                'ordering': ['box_type', 'length_mm', 'width_mm'],
            },
        ),
        migrations.CreateModel(
            name='ProductPackaging',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('qty_per_box', models.PositiveSmallIntegerField(default=1, help_text='Скільки одиниць товару вміщується в одну коробку', verbose_name='Товарів в коробку')),
                ('estimated_weight_g', models.PositiveIntegerField(blank=True, help_text='Якщо порожньо — розраховується автоматично з ваги товару + коробки', null=True, verbose_name='Орієнт. вага посилки (г)')),
                ('is_default', models.BooleanField(default=True, help_text='Основна рекомендація для цього товару', verbose_name='Рекомендована')),
                ('notes', models.TextField(blank=True, default='', verbose_name='Нотатки')),
                ('packaging', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='shipping.packagingmaterial', verbose_name='Упаковка')),
                ('product', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='packaging_recommendations', to='inventory.product', verbose_name='Товар')),
            ],
            options={
                'verbose_name': 'Рекомендована упаковка',
                'verbose_name_plural': 'Рекомендовані упаковки',
                'ordering': ['-is_default'],
            },
        ),
        migrations.CreateModel(
            name='OrderPackaging',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('qty_boxes', models.PositiveSmallIntegerField(default=1, verbose_name='Кількість коробок')),
                ('actual_weight_g', models.PositiveIntegerField(blank=True, help_text='Фактична вага готової посилки', null=True, verbose_name='Фактична вага (г)')),
                ('notes', models.TextField(blank=True, default='', verbose_name='Нотатки')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Зафіксовано')),
                ('order', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='packaging_used', to='sales.salesorder', verbose_name='Замовлення')),
                ('packaging', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='shipping.packagingmaterial', verbose_name='Упаковка')),
            ],
            options={
                'verbose_name': 'Упаковка замовлення',
                'verbose_name_plural': 'Упаковки замовлень',
                'ordering': ['-created_at'],
            },
        ),
    ]
