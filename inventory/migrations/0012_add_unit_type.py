# Generated migration file
from django.db import migrations, models
from django.core.validators import MinValueValidator


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0011_rename_notes_productcomponent_note_and_more'),  # Р—Р°РјС–РЅС–С‚СЊ РЅР° РѕСЃС‚Р°РЅРЅСЋ РјС–РіСЂР°С†С–СЋ
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='unit_type',
            field=models.CharField(
                choices=[
                    ('piece', 'РЁС‚СѓРєРё (С†С–Р»С– С‡РёСЃР»Р°)'),
                    ('meter', 'РњРµС‚СЂРё (РґСЂРѕР±РЅС– С‡РёСЃР»Р°)'),
                    ('kilogram', 'РљС–Р»РѕРіСЂР°РјРё (РґСЂРѕР±РЅС– С‡РёСЃР»Р°)'),
                    ('liter', 'Р›С–С‚СЂРё (РґСЂРѕР±РЅС– С‡РёСЃР»Р°)'),
                    ('set', 'РљРѕРјРїР»РµРєС‚Рё (С†С–Р»С– С‡РёСЃР»Р°)')
                ],
                default='piece',
                help_text='Р’РёР·РЅР°С‡Р°С” С‡Рё С‚РѕРІР°СЂ РІРёРјС–СЂСЋС”С‚СЊСЃСЏ РІ С†С–Р»РёС… С‡РёСЃР»Р°С… (С€С‚СѓРєРё) С‡Рё РґСЂРѕР±РЅРёС… (РјРµС‚СЂРё, РєРі)',
                max_length=20
            ),
        ),
        migrations.AlterField(
            model_name='productcomponent',
            name='qty_per',
            field=models.DecimalField(
                decimal_places=3,
                default=1.0,
                help_text='РљС–Р»СЊРєС–СЃС‚СЊ РєРѕРјРїРѕРЅРµРЅС‚Р° РЅР° РѕРґРёРЅРёС†СЋ РіРѕС‚РѕРІРѕРіРѕ РІРёСЂРѕР±Сѓ',
                max_digits=18,
                validators=[
                    MinValueValidator(0.001)
                ]
            ),
        ),
    ]

