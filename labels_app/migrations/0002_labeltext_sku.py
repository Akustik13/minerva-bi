import django.db.models.deletion
from django.db import migrations, models


def fill_sku(apps, schema_editor):
    """Наявні записи (ключ був товар) → артикул товару."""
    LabelText = apps.get_model("labels_app", "LabelText")
    for text in LabelText.objects.select_related("product"):
        text.sku = text.product.sku if text.product_id else f"#{text.pk}"
        text.save(update_fields=["sku"])


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0040_media_link_rules"),
        ("labels_app", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="labeltext",
            name="sku",
            field=models.CharField(default="", max_length=128, verbose_name="Артикул на етикетці"),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="labeltext",
            name="digikey_pn",
            field=models.CharField(blank=True, default="", max_length=80, verbose_name="Digi-Key P/N"),
        ),
        migrations.RunPython(fill_sku, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="labeltext",
            name="sku",
            field=models.CharField(max_length=128, unique=True, verbose_name="Артикул на етикетці"),
        ),
        migrations.AlterField(
            model_name="labeltext",
            name="product",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="label_texts", to="inventory.product", verbose_name="Товар"),
        ),
    ]
