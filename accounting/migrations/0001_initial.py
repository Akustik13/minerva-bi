import datetime
from decimal import Decimal
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("crm", "0007_migrate_customer_addresses"),
        ("inventory", "0017_supplier_addr_fields"),
        ("sales", "0018_migrate_salesorder_addresses"),
    ]

    operations = [
        migrations.CreateModel(
            name="CompanySettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(default="Моя компанія", max_length=255, verbose_name="Назва компанії")),
                ("legal_name", models.CharField(blank=True, default="", max_length=255, verbose_name="Юридична назва")),
                ("addr_street", models.CharField(blank=True, default="", max_length=300, verbose_name="Адреса")),
                ("addr_city", models.CharField(blank=True, default="", max_length=100, verbose_name="Місто")),
                ("addr_zip", models.CharField(blank=True, default="", max_length=20, verbose_name="Поштовий індекс")),
                ("addr_country", models.CharField(blank=True, default="", max_length=2, verbose_name="Країна (ISO 2)")),
                ("vat_id", models.CharField(blank=True, default="", max_length=50, verbose_name="VAT ID / ІПН")),
                ("iban", models.CharField(blank=True, default="", max_length=34, verbose_name="IBAN")),
                ("bank_name", models.CharField(blank=True, default="", max_length=255, verbose_name="Банк")),
                ("swift", models.CharField(blank=True, default="", max_length=11, verbose_name="SWIFT/BIC")),
                ("email", models.EmailField(blank=True, default="", max_length=254, verbose_name="Email")),
                ("phone", models.CharField(blank=True, default="", max_length=50, verbose_name="Телефон")),
                ("logo", models.FileField(blank=True, null=True, upload_to="accounting/logos/", verbose_name="Логотип (PNG/JPG)")),
                ("invoice_prefix", models.CharField(default="INV", max_length=10, verbose_name="Префікс рахунку")),
                ("next_number", models.PositiveIntegerField(default=1, verbose_name="Наступний номер рахунку")),
            ],
            options={
                "verbose_name": "Налаштування компанії",
                "verbose_name_plural": "Налаштування компанії",
            },
        ),
        migrations.CreateModel(
            name="ExpenseCategory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100, unique=True, verbose_name="Назва")),
                ("parent", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="children", to="accounting.expensecategory", verbose_name="Батьківська категорія")),
            ],
            options={
                "verbose_name": "Категорія витрат",
                "verbose_name_plural": "Категорії витрат",
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="Invoice",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("number", models.CharField(blank=True, editable=False, max_length=30, unique=True, verbose_name="Номер")),
                ("status", models.CharField(choices=[("draft","Чернетка"),("sent","Надіслано"),("paid","Оплачено"),("overdue","Прострочено"),("cancelled","Скасовано")], db_index=True, default="draft", max_length=20, verbose_name="Статус")),
                ("currency", models.CharField(default="EUR", max_length=3, verbose_name="Валюта")),
                ("issue_date", models.DateField(default=datetime.date.today, verbose_name="Дата виставлення")),
                ("service_date", models.DateField(blank=True, null=True, verbose_name="Дата послуги (Leistungsdatum)")),
                ("due_date", models.DateField(blank=True, null=True, verbose_name="Термін оплати")),
                ("vat_rate", models.DecimalField(decimal_places=2, default=Decimal("0"), max_digits=5, verbose_name="VAT %")),
                ("notes", models.TextField(blank=True, default="", verbose_name="Примітки")),
                ("client_name", models.CharField(blank=True, default="", max_length=255, verbose_name="Клієнт (snapshot)")),
                ("client_addr", models.TextField(blank=True, default="", verbose_name="Адреса (snapshot)")),
                ("client_vat", models.CharField(blank=True, default="", max_length=50, verbose_name="VAT клієнта")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Створено")),
                ("customer", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to="crm.customer", verbose_name="Клієнт")),
                ("order", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to="sales.salesorder", verbose_name="Замовлення")),
            ],
            options={
                "verbose_name": "Рахунок-фактура",
                "verbose_name_plural": "Рахунки-фактури",
                "ordering": ["-issue_date", "-id"],
                "indexes": [
                    models.Index(fields=["status"], name="accounting_invoice_status_idx"),
                    models.Index(fields=["issue_date"], name="accounting_invoice_issue_date_idx"),
                    models.Index(fields=["due_date"], name="accounting_invoice_due_date_idx"),
                ],
            },
        ),
        migrations.CreateModel(
            name="InvoiceLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("description", models.CharField(max_length=500, verbose_name="Опис")),
                ("quantity", models.DecimalField(decimal_places=3, default=Decimal("1.000"), max_digits=12, verbose_name="Кількість")),
                ("unit_price", models.DecimalField(decimal_places=4, max_digits=18, verbose_name="Ціна за одиницю")),
                ("discount", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=5, verbose_name="Знижка %")),
                ("unit", models.CharField(blank=True, default="шт", max_length=20, verbose_name="Од.вим.")),
                ("invoice", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lines", to="accounting.invoice", verbose_name="Рахунок")),
                ("product", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to="inventory.product", verbose_name="Товар")),
            ],
            options={
                "verbose_name": "Рядок рахунку",
                "verbose_name_plural": "Рядки рахунку",
            },
        ),
        migrations.CreateModel(
            name="Payment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("date", models.DateField(default=datetime.date.today, verbose_name="Дата")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=18, verbose_name="Сума")),
                ("method", models.CharField(choices=[("bank","Bank transfer"),("card","Card"),("cash","Cash"),("stripe","Stripe"),("paypal","PayPal"),("crypto","Crypto")], default="bank", max_length=20, verbose_name="Метод")),
                ("notes", models.TextField(blank=True, default="", verbose_name="Примітки")),
                ("invoice", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="payments", to="accounting.invoice", verbose_name="Рахунок")),
            ],
            options={
                "verbose_name": "Платіж",
                "verbose_name_plural": "Платежі",
                "ordering": ["-date"],
            },
        ),
        migrations.CreateModel(
            name="Expense",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("date", models.DateField(db_index=True, default=datetime.date.today, verbose_name="Дата")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=18, verbose_name="Сума")),
                ("currency", models.CharField(default="EUR", max_length=3, verbose_name="Валюта")),
                ("description", models.CharField(max_length=500, verbose_name="Опис")),
                ("receipt", models.FileField(blank=True, null=True, upload_to="accounting/receipts/", verbose_name="Чек/документ")),
                ("is_vat_deductible", models.BooleanField(default=False, verbose_name="VAT-deductible")),
                ("category", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to="accounting.expensecategory", verbose_name="Категорія")),
                ("supplier", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to="inventory.supplier", verbose_name="Постачальник")),
            ],
            options={
                "verbose_name": "Витрата",
                "verbose_name_plural": "Витрати",
                "ordering": ["-date"],
            },
        ),
    ]
