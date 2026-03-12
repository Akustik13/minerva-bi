from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("sales", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Carrier",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True)),
                ("name", models.CharField(max_length=100, verbose_name="Назва")),
                ("carrier_type", models.CharField(
                    choices=[("jumingo","Jumingo (агрегатор)"),("dhl","DHL"),
                             ("ups","UPS"),("fedex","FedEx"),("other","Інше")],
                    default="jumingo", max_length=20, verbose_name="Тип")),
                ("is_active",  models.BooleanField(default=True,  verbose_name="Активний")),
                ("is_default", models.BooleanField(default=False, verbose_name="За замовчуванням")),
                ("api_key",    models.CharField(blank=True, default="", max_length=500, verbose_name="API ключ / логін")),
                ("api_secret", models.CharField(blank=True, default="", max_length=500, verbose_name="API секрет / пароль")),
                ("api_url",    models.CharField(blank=True, default="", max_length=300, verbose_name="API URL")),
                ("sender_name",    models.CharField(blank=True, default="", max_length=200, verbose_name="Ім'я відправника")),
                ("sender_company", models.CharField(blank=True, default="", max_length=200, verbose_name="Компанія відправника")),
                ("sender_street",  models.CharField(blank=True, default="", max_length=300, verbose_name="Вулиця, будинок")),
                ("sender_city",    models.CharField(blank=True, default="", max_length=100, verbose_name="Місто")),
                ("sender_zip",     models.CharField(blank=True, default="", max_length=20,  verbose_name="Поштовий індекс")),
                ("sender_country", models.CharField(blank=True, default="DE", max_length=2, verbose_name="Країна (ISO 2)")),
                ("sender_phone",   models.CharField(blank=True, default="", max_length=50,  verbose_name="Телефон відправника")),
                ("sender_email",   models.EmailField(blank=True, default="",               verbose_name="Email відправника")),
                ("notes", models.TextField(blank=True, default="", verbose_name="Нотатки")),
            ],
            options={"verbose_name": "Перевізник", "verbose_name_plural": "Перевізники",
                     "ordering": ["-is_default", "name"]},
        ),
        migrations.CreateModel(
            name="Shipment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True)),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                    related_name="shipments", to="sales.salesorder", verbose_name="Замовлення")),
                ("carrier", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                    to="shipping.carrier", verbose_name="Перевізник")),
                ("status", models.CharField(
                    choices=[("draft","Чернетка"),("submitted","Передано перевізнику"),
                             ("label_ready","Етикетка готова"),("in_transit","В дорозі"),
                             ("delivered","Доставлено"),("error","Помилка"),
                             ("cancelled","Скасовано")],
                    default="draft", max_length=20, verbose_name="Статус")),
                ("recipient_name",    models.CharField(blank=True, default="", max_length=255, verbose_name="Ім'я / Компанія")),
                ("recipient_company", models.CharField(blank=True, default="", max_length=255, verbose_name="Компанія")),
                ("recipient_street",  models.CharField(blank=True, default="", max_length=300, verbose_name="Вулиця, будинок")),
                ("recipient_city",    models.CharField(blank=True, default="", max_length=100, verbose_name="Місто")),
                ("recipient_zip",     models.CharField(blank=True, default="", max_length=20,  verbose_name="Поштовий індекс")),
                ("recipient_country", models.CharField(blank=True, default="", max_length=2,   verbose_name="Країна (ISO 2)")),
                ("recipient_phone",   models.CharField(blank=True, default="", max_length=50,  verbose_name="Телефон")),
                ("recipient_email",   models.EmailField(blank=True, default="",               verbose_name="Email")),
                ("weight_kg",   models.DecimalField(decimal_places=3, default=1, max_digits=8,  verbose_name="Вага (кг)")),
                ("length_cm",   models.DecimalField(blank=True, decimal_places=1, max_digits=6, null=True, verbose_name="Довжина (см)")),
                ("width_cm",    models.DecimalField(blank=True, decimal_places=1, max_digits=6, null=True, verbose_name="Ширина (см)")),
                ("height_cm",   models.DecimalField(blank=True, decimal_places=1, max_digits=6, null=True, verbose_name="Висота (см)")),
                ("description",       models.CharField(blank=True, default="", max_length=300, verbose_name="Опис вмісту")),
                ("declared_value",    models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True, verbose_name="Задекларована вартість")),
                ("declared_currency", models.CharField(default="EUR", max_length=3, verbose_name="Валюта")),
                ("reference",         models.CharField(blank=True, default="", max_length=100, verbose_name="Референс")),
                ("carrier_shipment_id", models.CharField(blank=True, default="", max_length=200, verbose_name="ID відправлення (перевізник)")),
                ("tracking_number",   models.CharField(blank=True, default="", max_length=200, verbose_name="Трекінг номер")),
                ("label_url",         models.URLField(blank=True, default="", max_length=500,  verbose_name="URL етикетки (PDF)")),
                ("carrier_price",     models.DecimalField(blank=True, decimal_places=2, max_digits=8, null=True, verbose_name="Вартість доставки")),
                ("carrier_currency",  models.CharField(blank=True, default="EUR", max_length=3, verbose_name="Валюта вартості")),
                ("carrier_service",   models.CharField(blank=True, default="", max_length=200, verbose_name="Послуга перевізника")),
                ("raw_request",   models.JSONField(blank=True, null=True, verbose_name="Запит (JSON)")),
                ("raw_response",  models.JSONField(blank=True, null=True, verbose_name="Відповідь (JSON)")),
                ("error_message", models.TextField(blank=True, default="", verbose_name="Повідомлення про помилку")),
                ("created_at",   models.DateTimeField(auto_now_add=True, verbose_name="Створено")),
                ("submitted_at", models.DateTimeField(blank=True, null=True, verbose_name="Відправлено")),
                ("created_by", models.ForeignKey(blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    to="auth.user", verbose_name="Автор")),
            ],
            options={"verbose_name": "Відправлення", "verbose_name_plural": "Відправлення",
                     "ordering": ["-created_at"]},
        ),
    ]
