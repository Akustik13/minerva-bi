from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("bots", "0044_digikey_message_own"),
    ]

    operations = [
        migrations.AddField(
            model_name="digikeyconfig",
            name="vat_attach_eu",
            field=models.BooleanField(
                default=True,
                help_text="При підтвердженні відправки в ЄС Minerva завантажує PDF рахунку з «Інвойси DigiKey» на DigiKey і "
                          "передає його разом із трек-номером. API DigiKey цього офіційно не описує, тому після "
                          "відправки Minerva перечитує замовлення й каже, чи рахунок прив'язався; ні — прикріпіть на "
                          "supplier.digikey.com вручну.",
                verbose_name="Прикріплювати PDF рахунку до замовлення (ЄС)"),
        ),
        migrations.AddField(
            model_name="digikeyconfig",
            name="vat_link_last",
            field=models.JSONField(blank=True, default=dict, verbose_name="Остання спроба прикріпити рахунок"),
        ),
    ]
