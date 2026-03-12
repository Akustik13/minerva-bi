from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0008_customernote_due_date_reminder'),
    ]

    operations = [
        migrations.AlterField(
            model_name='customer',
            name='name',
            field=models.CharField(max_length=255, verbose_name='Контактна особа'),
        ),
        migrations.AlterField(
            model_name='customer',
            name='company',
            field=models.CharField(blank=True, default='', max_length=255, verbose_name='Компанія'),
        ),
    ]
