from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('shipping', '0020_upsconfig'),
    ]

    operations = [
        migrations.DeleteModel(name='UPSConfig'),
    ]
