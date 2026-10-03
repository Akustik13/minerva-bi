"""Кілька магазинів (3/3): ступені цін належать лише позиції магазину."""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("shop", "0003_multi_shop_data")]

    operations = [
        migrations.RemoveConstraint(model_name="shoppricetier", name="uniq_shop_tier_qty"),
        migrations.RemoveField(model_name="shoppricetier", name="product"),
        migrations.AlterField(
            model_name="shoppricetier",
            name="listing",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="tiers",
                                    to="shop.shoplisting", verbose_name="Позиція"),
        ),
        migrations.AlterModelOptions(
            name="shoppricetier",
            options={"ordering": ["listing", "min_qty"], "verbose_name": "Ступінь ціни", "verbose_name_plural": "Ступені цін"},
        ),
        migrations.AddConstraint(
            model_name="shoppricetier",
            constraint=models.UniqueConstraint(fields=("listing", "min_qty"), name="uniq_listing_tier_qty"),
        ),
        migrations.AlterModelOptions(
            name="shopproduct",
            options={"proxy": True, "verbose_name": "Товар каталогу", "verbose_name_plural": "📦 Каталог → додати в магазин"},
        ),
    ]
