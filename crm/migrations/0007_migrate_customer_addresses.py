from django.db import migrations


COUNTRY_MAP = {
    "UA": "UA", "UKRAINE": "UA", "УКРАЇНА": "UA",
    "DE": "DE", "GERMANY": "DE", "DEUTSCHLAND": "DE",
    "PL": "PL", "POLAND": "PL",
    "US": "US", "USA": "US",
    "GB": "GB", "UK": "GB",
    "AT": "AT", "AUSTRIA": "AT",
    "CH": "CH", "CZ": "CZ", "NL": "NL", "FR": "FR", "IT": "IT", "ES": "ES",
}


def parse_address(addr_text, country_hint=""):
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
    return street, city, zip_code


def normalize_country(raw):
    up = (raw or "").strip().upper()
    return COUNTRY_MAP.get(up, up[:2] if up else "")


def migrate_forward(apps, schema_editor):
    Customer = apps.get_model("crm", "Customer")
    to_update = []
    for customer in Customer.objects.all().only(
        "id", "shipping_address", "country",
        "addr_street", "addr_city", "addr_zip"
    ):
        changed = False
        if not customer.addr_street and not customer.addr_city:
            if customer.shipping_address:
                street, city, zip_code = parse_address(customer.shipping_address)
                customer.addr_street = street
                customer.addr_city   = city
                customer.addr_zip    = zip_code
                changed = True
        # Нормалізуємо country до ISO 2
        norm = normalize_country(customer.country)
        if norm != customer.country:
            customer.country = norm
            changed = True
        if changed:
            to_update.append(customer)

    if to_update:
        Customer.objects.bulk_update(
            to_update, ["addr_street", "addr_city", "addr_zip", "country"],
            batch_size=500
        )


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0006_customer_addr_fields"),
    ]

    operations = [
        migrations.RunPython(migrate_forward, migrations.RunPython.noop),
    ]
