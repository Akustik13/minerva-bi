# Листи замовлення в панелі праворуч (CustomerTimeline.related_order_id) — без повного перегляду хронології.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0015_timeline_email_link'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='customertimeline',
            index=models.Index(fields=['related_order_id'], name='crm_tl_related_order'),
        ),
    ]
