"""Копії окремо для кожного листа магазину (Shop.mail_letters): спільна «Прихована копія листів покупцям»
(mail_bcc) переходить у приховану копію кожного листа покупцю — листи йдуть як раніше. Поле mail_bcc
прибирає 0039 (окремо: на PostgreSQL зміна схеми після зміни рядків в одній міграції може не пройти)."""
from django.db import migrations, models

# Листи покупцю з копіями (shop.mail.CUSTOMER_LETTERS на момент міграції)
CUSTOMER_LETTERS = ("exists", "welcome", "paid", "changed", "deleted", "invoice", "order_customer", "quote_customer")


def forward(apps, schema_editor):
    Shop = apps.get_model("shop", "Shop")
    for shop in Shop.objects.exclude(mail_bcc=""):
        bcc = ", ".join(a.strip() for a in shop.mail_bcc.replace(";", ",").split(",") if a.strip())
        if not bcc:
            continue
        letters = dict(shop.mail_letters or {})
        for key in CUSTOMER_LETTERS:
            letters[key] = {**(letters.get(key) or {}), "bcc": bcc}
        Shop.objects.filter(pk=shop.pk).update(mail_letters=letters)


def backward(apps, schema_editor):
    Shop = apps.get_model("shop", "Shop")
    for shop in Shop.objects.all():
        bcc = ((shop.mail_letters or {}).get("welcome") or {}).get("bcc") or ""
        if bcc:
            Shop.objects.filter(pk=shop.pk).update(mail_bcc=bcc[:300])


class Migration(migrations.Migration):

    dependencies = [
        ('shop', '0037_payment_cod'),
    ]

    operations = [
        migrations.AddField(
            model_name='shop',
            name='mail_letters',
            field=models.JSONField(blank=True, default=dict, help_text='Кому ще йде кожен лист: копія (CC, адресу видно) і прихована копія (BCC; лише листи покупцю). Задається в таблиці «Хто що кому шле»; листи з одноразовими посиланнями (підтвердження, новий пароль, запрошення) не копіюються.', verbose_name='Копії листів'),
        ),
        migrations.RunPython(forward, backward),
    ]
