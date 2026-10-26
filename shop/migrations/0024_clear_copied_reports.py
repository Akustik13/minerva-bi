from django.db import migrations
from django.utils.dateparse import parse_datetime


def clear_copied_reports(apps, schema_editor):
    """Копії магазинів успадкували звіти сайтів оригіналу: прибрати записи, старші за саму копію."""
    Shop = apps.get_model("shop", "Shop")
    for shop in Shop.objects.exclude(copied_from=None):
        update = {}
        for field in ("site_mail_reports", "assistant_sites"):
            reports = getattr(shop, field) or {}
            kept = {host: r for host, r in reports.items()
                    if (at := parse_datetime(str((r or {}).get("at") or ""))) is not None and at >= shop.created_at}
            if kept != reports:
                update[field] = kept
        if update:
            Shop.objects.filter(pk=shop.pk).update(**update)


class Migration(migrations.Migration):

    dependencies = [
        ('shop', '0023_site_mail'),
    ]

    operations = [
        migrations.RunPython(clear_copied_reports, migrations.RunPython.noop),
    ]
