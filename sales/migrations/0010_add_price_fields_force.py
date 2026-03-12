from django.db import migrations


class Migration(migrations.Migration):
    """Додає total_price/currency через RAW SQL з IF NOT EXISTS."""
    
    dependencies = [
        ('sales', '0009_merge_migrations'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
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
            """,
            reverse_sql="-- No reverse"
        ),
    ]
