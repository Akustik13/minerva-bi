from django.db import migrations

FORCE_SQL = """
    ALTER TABLE sales_salesorder
    ADD COLUMN IF NOT EXISTS total_price NUMERIC(18,2) NULL;

    ALTER TABLE sales_salesorder
    ADD COLUMN IF NOT EXISTS currency VARCHAR(8) DEFAULT 'EUR';

    ALTER TABLE sales_salesorderline
    ADD COLUMN IF NOT EXISTS unit_price NUMERIC(18,4) NULL;

    ALTER TABLE sales_salesorderline
    ADD COLUMN IF NOT EXISTS total_price NUMERIC(18,2) NULL;

    ALTER TABLE sales_salesorderline
    ADD COLUMN IF NOT EXISTS currency VARCHAR(8) DEFAULT 'EUR';
"""


def force_price_columns(apps, schema_editor):
    # «ADD COLUMN IF NOT EXISTS» є лише в PostgreSQL; на новій базі стовпці вже створила 0009
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(FORCE_SQL)


class Migration(migrations.Migration):
    """Ремонт старих баз: total_price/currency через RAW SQL з IF NOT EXISTS."""

    dependencies = [
        ('sales', '0009_merge_migrations'),
    ]

    operations = [
        migrations.RunPython(force_price_columns, migrations.RunPython.noop),
    ]
