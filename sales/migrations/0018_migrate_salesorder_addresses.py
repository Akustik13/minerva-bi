from django.db import migrations


COUNTRY_MAP = {
    "UA": "UA", "UKRAINE": "UA", "УКРАЇНА": "UA",
    "DE": "DE", "GERMANY": "DE", "DEUTSCHLAND": "DE",
    "PL": "PL", "POLAND": "PL", "ПОЛЬЩА": "PL",
    "US": "US", "USA": "US",
    "GB": "GB", "UK": "GB",
    "AT": "AT", "AUSTRIA": "AT", "ÖSTERREICH": "AT",
    "CH": "CH", "SWITZERLAND": "CH",
    "CZ": "CZ", "NL": "NL", "FR": "FR", "IT": "IT", "ES": "ES",
}


def parse_address(addr_text, region=""):
    lines = [l.strip() for l in (addr_text or "").splitlines() if l.strip()]
    street, city, zip_code = "", "", ""
    if lines:
        street = lines[0]
        if len(lines) >= 2:
            last = lines[-1]
            parts = last.split(" ", 1)
            token = parts[0].replace("-", "").replace(" ", "")
            if token.isalnum() and len(token) <= 7:
                zip_code = parts[0]
                city = parts[1].strip() if len(parts) > 1 else ""
            else:
                city = last
    region_up = (region or "").strip().upper()
    country = COUNTRY_MAP.get(region_up, region_up[:2] if region_up else "")
    return street, city, zip_code, country


def migrate_forward(apps, schema_editor):
    SalesOrder = apps.get_model("sales", "SalesOrder")
    to_update = []
    for order in SalesOrder.objects.all().only(
        "id", "shipping_address", "shipping_region",
        "addr_street", "addr_city", "addr_zip", "addr_country"
    ):
        # Пропускаємо якщо вже заповнено
        if order.addr_street or order.addr_city:
            continue
        street, city, zip_code, country = parse_address(
            order.shipping_address, order.shipping_region
        )
        order.addr_street  = street
        order.addr_city    = city
        order.addr_zip     = zip_code
        order.addr_country = country
        to_update.append(order)

    if to_update:
        SalesOrder.objects.bulk_update(
            to_update, ["addr_street", "addr_city", "addr_zip", "addr_country"],
            batch_size=500
        )


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0017_salesorder_addr_fields_indexes"),
    ]

    operations = [
        migrations.RunPython(migrate_forward, migrations.RunPython.noop),
    ]
