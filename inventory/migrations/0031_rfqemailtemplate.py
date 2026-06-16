from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0030_purchaseorder_supplier_nullable'),
    ]

    operations = [
        migrations.CreateModel(
            name='RFQEmailTemplate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, verbose_name='Назва шаблону')),
                ('subject', models.CharField(default='Order Request', max_length=255, verbose_name='Тема листа')),
                ('greeting', models.CharField(default='Hi {contact_person},', help_text='{contact_person} замінюється на ім\'я контактної особи постачальника', max_length=255, verbose_name='Вітання')),
                ('intro', models.TextField(default='I have a new urgent RFQ:', verbose_name='Текст вступу')),
                ('signature', models.TextField(default='Best regards,', verbose_name='Підпис')),
                ('footer_note', models.TextField(blank=True, default='', help_text="Наприклад: 'Please note that cable length L is measured...'", verbose_name='Нотатка після таблиці')),
                ('use_cable_columns', models.BooleanField(default=False, help_text='Розбирає SKU кабелю (CA-…-MCx.xx-…) та формує окремі колонки: довжина, товщина, з\'єднувачі', verbose_name='Кабельні колонки')),
                ('diagram', models.ImageField(blank=True, help_text='Вставляється після таблиці в панелі Email кошика', null=True, upload_to='rfq_diagrams/', verbose_name='Схема / зображення')),
                ('category', models.ForeignKey(blank=True, help_text='Порожньо = шаблон за замовчуванням', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='rfq_templates', to='inventory.productcategory', verbose_name='Категорія товарів')),
            ],
            options={
                'verbose_name': 'Шаблон RFQ Email',
                'verbose_name_plural': '\U0001f4e7 Шаблони RFQ Email',
                'ordering': ['category__order', 'name'],
            },
        ),
    ]
