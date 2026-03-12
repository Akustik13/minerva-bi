from django.db import migrations, models


def seed_categories(apps, schema_editor):
    """Засів початкових категорій з існуючих hardcoded значень."""
    ProductCategory = apps.get_model("inventory", "ProductCategory")
    initial = [
        ("antenna",   "Antenna",   "#e91e63", 1),
        ("cable",     "Cable",     "#2196f3", 2),
        ("filter",    "Filter",    "#9c27b0", 3),
        ("other",     "Other",     "#607d8b", 99),
    ]
    for slug, name, color, order in initial:
        ProductCategory.objects.get_or_create(
            slug=slug,
            defaults={"name": name, "color": color, "order": order},
        )


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0014_reorderproxy"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProductCategory",
            fields=[
                ("id",    models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("slug",  models.CharField(help_text="Латиниця, без пробілів.", max_length=64, unique=True, verbose_name="Код (slug)")),
                ("name",  models.CharField(max_length=128, verbose_name="Назва")),
                ("color", models.CharField(default="#607d8b", help_text="HEX, напр. #e91e63", max_length=16, verbose_name="Колір бейджу")),
                ("order", models.PositiveSmallIntegerField(default=0, verbose_name="Порядок")),
            ],
            options={
                "verbose_name": "Категорія товару",
                "verbose_name_plural": "Категорії товарів",
                "ordering": ["order", "name"],
            },
        ),
        # max_length 20→64, remove choices — ALTER TABLE (швидко, без втрати даних)
        migrations.AlterField(
            model_name="product",
            name="category",
            field=models.CharField(
                default="other",
                help_text="Оберіть з довідника або введіть slug вручну",
                max_length=64,
                verbose_name="Категорія",
            ),
        ),
        migrations.RunPython(seed_categories, migrations.RunPython.noop),
    ]
