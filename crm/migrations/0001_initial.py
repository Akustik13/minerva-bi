from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.CreateModel(
            name="Customer",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True)),
                ("name", models.CharField(max_length=255, verbose_name="Ім'я / Компанія")),
                ("email", models.EmailField(unique=True, verbose_name="Email")),
                ("phone", models.CharField(blank=True, default="", max_length=50, verbose_name="Телефон")),
                ("company", models.CharField(blank=True, default="", max_length=255, verbose_name="Назва компанії")),
                ("country", models.CharField(blank=True, default="", max_length=3, verbose_name="Країна (ISO)")),
                ("shipping_address", models.TextField(blank=True, default="", verbose_name="Адреса доставки")),
                ("segment", models.CharField(choices=[("b2b","B2B (компанія)"),("b2c","B2C (фізична особа)"),("distributor","Дистрибютор"),("reseller","Реселер"),("other","Інше")], default="b2c", max_length=20, verbose_name="Сегмент")),
                ("status", models.CharField(choices=[("active","Активний"),("inactive","Неактивний"),("vip","VIP"),("blocked","Заблокований")], default="active", max_length=20, verbose_name="Статус")),
                ("source", models.CharField(blank=True, default="", help_text="digikey / webshop / manual / etc", max_length=64, verbose_name="Джерело")),
                ("notes", models.TextField(blank=True, default="", verbose_name="Примітки")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Створено")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Оновлено")),
            ],
            options={"verbose_name": "Клієнт", "verbose_name_plural": "Клієнти", "ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="CustomerNote",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True)),
                ("note_type", models.CharField(choices=[("call","Дзвінок"),("email","Email"),("meeting","Зустріч"),("note","Нотатка"),("other","Інше")], default="note", max_length=20, verbose_name="Тип")),
                ("subject", models.CharField(max_length=255, verbose_name="Тема")),
                ("body", models.TextField(blank=True, default="", verbose_name="Текст")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Дата")),
                ("customer", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="notes_crm", to="crm.customer", verbose_name="Клієнт")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to="auth.user", verbose_name="Автор")),
            ],
            options={"verbose_name": "Нотатка", "verbose_name_plural": "Нотатки", "ordering": ["-created_at"]},
        ),
    ]
