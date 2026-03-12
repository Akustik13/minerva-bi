from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipping', '0003_packaging_materials'),
    ]

    operations = [
        # ── 1. Rename columns (values still in old units) ────────────────────
        migrations.RenameField('PackagingMaterial', 'length_mm', 'length_cm'),
        migrations.RenameField('PackagingMaterial', 'width_mm',  'width_cm'),
        migrations.RenameField('PackagingMaterial', 'height_mm', 'height_cm'),
        migrations.RenameField('PackagingMaterial', 'tare_weight_g', 'tare_weight_kg'),
        migrations.RenameField('PackagingMaterial', 'max_weight_g',  'max_weight_kg'),

        # ── 2. Change field types to DecimalField ─────────────────────────────
        migrations.AlterField(
            model_name='packagingmaterial',
            name='length_cm',
            field=models.DecimalField(max_digits=6, decimal_places=1, verbose_name='Довжина (см)'),
        ),
        migrations.AlterField(
            model_name='packagingmaterial',
            name='width_cm',
            field=models.DecimalField(max_digits=6, decimal_places=1, verbose_name='Ширина (см)'),
        ),
        migrations.AlterField(
            model_name='packagingmaterial',
            name='height_cm',
            field=models.DecimalField(max_digits=6, decimal_places=1, verbose_name='Висота (см)'),
        ),
        migrations.AlterField(
            model_name='packagingmaterial',
            name='tare_weight_kg',
            field=models.DecimalField(
                max_digits=6, decimal_places=3, default=0,
                verbose_name='Вага порожньої (кг)',
                help_text='Вага самої коробки без вмісту',
            ),
        ),
        migrations.AlterField(
            model_name='packagingmaterial',
            name='max_weight_kg',
            field=models.DecimalField(
                max_digits=6, decimal_places=3, null=True, blank=True,
                verbose_name='Макс. вага вмісту (кг)',
                help_text='Максимально допустима вага товарів',
            ),
        ),

        # ── 3. Convert existing data: mm→cm (/10), g→kg (/1000) ─────────────
        migrations.RunSQL(
            sql="""
                UPDATE shipping_packagingmaterial
                SET length_cm      = length_cm      / 10.0,
                    width_cm       = width_cm       / 10.0,
                    height_cm      = height_cm      / 10.0,
                    tare_weight_kg = tare_weight_kg / 1000.0,
                    max_weight_kg  = CASE
                        WHEN max_weight_kg IS NOT NULL
                        THEN max_weight_kg / 1000.0
                        ELSE NULL
                    END;
            """,
            reverse_sql="""
                UPDATE shipping_packagingmaterial
                SET length_cm      = length_cm      * 10,
                    width_cm       = width_cm       * 10,
                    height_cm      = height_cm      * 10,
                    tare_weight_kg = tare_weight_kg * 1000,
                    max_weight_kg  = CASE
                        WHEN max_weight_kg IS NOT NULL
                        THEN max_weight_kg * 1000
                        ELSE NULL
                    END;
            """,
        ),
    ]
