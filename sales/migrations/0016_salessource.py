from django.db import migrations, models


def seed_sources(apps, schema_editor):
    """Засів початкових джерел з існуючих hardcoded значень."""
    SalesSource = apps.get_model("sales", "SalesSource")
    initial = [
        ("digikey",   "DigiKey",    "#e91e63", 1),
        ("nova_post", "Nova Post",  "#ff9800", 2),
        ("manual",    "Manual",     "#607d8b", 3),
    ]
    for slug, name, color, order in initial:
        SalesSource.objects.get_or_create(
            slug=slug,
            defaults={"name": name, "color": color, "order": order},
        )


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0015_salesorder_customer_key"),
    ]

    operations = [
        migrations.CreateModel(
            name="SalesSource",
            fields=[
                ("id",    models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("slug",  models.CharField(help_text="Латиниця, без пробілів.", max_length=32, unique=True, verbose_name="Код (slug)")),
                ("name",  models.CharField(max_length=128, verbose_name="Назва")),
                ("color", models.CharField(default="#607d8b", help_text="HEX, напр. #e91e63", max_length=16, verbose_name="Колір бейджу")),
                ("order", models.PositiveSmallIntegerField(default=0, verbose_name="Порядок")),
            ],
            options={
                "verbose_name": "Джерело замовлення",
                "verbose_name_plural": "Джерела замовлень",
                "ordering": ["order", "name"],
            },
        ),
        migrations.RunPython(seed_sources, migrations.RunPython.noop),
    ]
