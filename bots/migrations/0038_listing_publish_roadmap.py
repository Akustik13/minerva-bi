from django.db import migrations, models


def fill_events(apps, schema_editor):
    """Наявні лістинги: етапи публікації з того, що вже відомо (час — останньої синхронізації)."""
    Listing = apps.get_model("bots", "DigiKeyListing")
    for listing in Listing.objects.all().only("pk", "created_at", "last_synced_at", "dk_product_id",
                                              "dk_offer_id", "pub_events"):
        events = dict(listing.pub_events or {})
        when = (listing.last_synced_at or listing.created_at)
        if listing.created_at:
            events.setdefault("card", listing.created_at.isoformat())
        if listing.dk_product_id and when:
            events.setdefault("sent", when.isoformat())
        if listing.dk_offer_id and when:        # офер є — товар уже затверджено
            events.setdefault("approved", when.isoformat())
            events.setdefault("offer", when.isoformat())
        if events != (listing.pub_events or {}):
            Listing.objects.filter(pk=listing.pk).update(pub_events=events)


class Migration(migrations.Migration):

    dependencies = [
        ("bots", "0037_dk_price_heartbeat"),
    ]

    operations = [
        migrations.AddField(
            model_name="digikeylisting",
            name="card_source",
            field=models.CharField(blank=True, choices=[("", "—"), ("digikey", "Копія з DigiKey"),
                                                        ("stock", "Зі складу Minerva"), ("copy", "Копія лістингу"),
                                                        ("file", "З таблиці")],
                                   default="", max_length=10, verbose_name="Звідки картка"),
        ),
        migrations.AddField(
            model_name="digikeylisting",
            name="pub_events",
            field=models.JSONField(blank=True, default=dict, help_text="Автоматично: коли вперше пройдено кожен "
                                   "крок публікації (bots/services/dk_publish.py)", verbose_name="Етапи публікації"),
        ),
        migrations.RunPython(fill_events, migrations.RunPython.noop),
    ]
