from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [
        ('inventory', '0027_incoming_shipment'),
    ]
    operations = [
        migrations.AddField(
            model_name='product',
            name='tech_attributes',
            field=models.JSONField(blank=True, default=dict, help_text='Технічні параметри компонента (частота, смуга, тип тощо). Синхронізується з DigiKey лістингом.', verbose_name='Технічні атрибути'),
        ),
    ]
