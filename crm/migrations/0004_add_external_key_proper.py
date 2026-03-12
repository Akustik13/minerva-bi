# crm/migrations/0004_add_external_key_proper.py
from django.db import migrations, models


def generate_keys_for_existing(apps, schema_editor):
    """Генерує external_key для існуючих клієнтів."""
    Customer = apps.get_model('crm', 'Customer')
    import hashlib
    
    for customer in Customer.objects.all():
        source = f"{customer.email.lower().strip()}:{customer.name.lower().strip()}"
        key = hashlib.sha256(source.encode()).hexdigest()[:32]
        customer.external_key = key
        customer.save()


class Migration(migrations.Migration):
    dependencies = [
        ('crm', '0003_alter_customer_email'),
    ]

    operations = [
        # 1. Додаємо поле БЕЗ unique (можуть бути дублікати)
        migrations.AddField(
            model_name='customer',
            name='external_key',
            field=models.CharField(
                max_length=64,
                blank=True,
                default='',
                help_text='Унікальний ідентифікатор'
            ),
        ),
        
        # 2. Генеруємо ключі для всіх
        migrations.RunPython(generate_keys_for_existing),
        
        # 3. Додаємо unique constraint
        migrations.AlterField(
            model_name='customer',
            name='external_key',
            field=models.CharField(
                max_length=64,
                unique=True,
                db_index=True,
                help_text='Унікальний ідентифікатор'
            ),
        ),
    ]
