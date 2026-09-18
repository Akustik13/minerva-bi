from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipping', '0046_add_jlc_order_to_shipment'),
    ]

    operations = [
        # ── Rename legacy export_reason values in existing data ────────────────
        # Claim → Other  (Claim was the old value, Other is the new key)
        migrations.RunSQL(
            sql="UPDATE shipping_shipment SET export_reason='Other' WHERE export_reason='Claim'",
            reverse_sql="UPDATE shipping_shipment SET export_reason='Claim' WHERE export_reason='Other'",
        ),

        # ── Update field definition (new choices, extended max_length to 20 already ok) ─
        migrations.AlterField(
            model_name='shipment',
            name='export_reason',
            field=models.CharField(
                choices=[
                    ('Commercial', 'Commercial — продаж (Sale)'),
                    ('Gift',       'Gift — подарунок'),
                    ('Sample',     'Sample — зразок / тестовий виріб'),
                    ('Return',     'Return — повернення'),
                    ('Repair',     'Repair — ремонт / гарантія'),
                    ('Personal',   'Personal Effects — особисті речі'),
                    ('Other',      'Other — інше / рекламація'),
                ],
                default='Commercial',
                help_text='Для митної декларації (CN23, commercial invoice).',
                max_length=20,
                verbose_name='Причина експорту',
            ),
        ),

        # ── UPS billing fields ─────────────────────────────────────────────────
        migrations.AddField(
            model_name='shipment',
            name='ups_billing',
            field=models.CharField(
                choices=[
                    ('shipper',     'Відправник (BillShipper) — стандарт'),
                    ('receiver',    'Отримувач (BillReceiver) — акаунт UPS отримувача'),
                    ('third_party', 'Третя сторона (BillThirdParty)'),
                ],
                default='shipper',
                help_text='Хто оплачує вартість доставки. Застосовується тільки для UPS.',
                max_length=15,
                verbose_name='UPS: платник доставки',
            ),
        ),
        migrations.AddField(
            model_name='shipment',
            name='ups_billing_account',
            field=models.CharField(
                blank=True,
                help_text='Номер UPS-акаунту отримувача або третьої сторони.',
                max_length=50,
                verbose_name='UPS: акаунт платника',
            ),
        ),
        migrations.AddField(
            model_name='shipment',
            name='ups_billing_postal',
            field=models.CharField(
                blank=True,
                help_text='Поштовий індекс адреси платника (потрібен для BillReceiver/BillThirdParty).',
                max_length=20,
                verbose_name='UPS: індекс платника',
            ),
        ),
        migrations.AddField(
            model_name='shipment',
            name='ups_billing_country',
            field=models.CharField(
                blank=True,
                help_text='Код країни платника (DE, GB, US...).',
                max_length=2,
                verbose_name='UPS: країна платника',
            ),
        ),
    ]
