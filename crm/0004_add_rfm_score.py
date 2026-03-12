from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0003_customer_rfm_f_customer_rfm_m_customer_rfm_r'),
    ]

    operations = [
        migrations.AddField(
            model_name='customer',
            name='rfm_score',
            field=models.IntegerField(default=3, help_text='Сума R+F+M', verbose_name='RFM Score'),
        ),
    ]