"""Рахунки (Rechnung) на бланку фірми — розділ «🧾 Рахунки» (/rechnungen/), логіка — sales/invoice_service.py.

SalesInvoice — один документ: номер (замовлення з сайту — нумерація магазину `CI-<Kundennummer>-NNN`, інакше
префікс + номер замовлення), адреса рахунку й доставки (знімок), позиції, ПДВ, тексти, підписант, DOCX / PDF.
InvoiceSettings — усе, що задається один раз: мова, строки, тексти DE / EN, реквізити банку, підписант, листи, шаблон.
"""
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

LANGS = [("de", "Deutsch"), ("en", "English"), ("de_en", "Deutsch + English")]
PAY_KINDS = [("invoice", "Оплата за рахунком (строк)"), ("prepayment", "Передоплата (Vorkasse)"),
             ("paid", "Уже оплачено")]
DEFAULT_TEXTS = {
    "intro_de": "gemäß Ihrer Bestellung {order} berechnen wir Ihnen die folgenden Positionen:",
    "intro_en": "According to your order {order}, we invoice you for the following items:",
    "pay_invoice_de": "Bitte überweisen Sie den Rechnungsbetrag ohne Abzug bis zum {due_date} unter Angabe der "
                      "Rechnungsnummer {number} auf das folgende Konto:",
    "pay_invoice_en": "Please transfer the invoice amount without deduction by {due_date}, quoting invoice number "
                      "{number}, to the following account:",
    "pay_prepayment_de": "Bitte überweisen Sie den Rechnungsbetrag vorab unter Angabe der Rechnungsnummer {number} "
                         "auf das folgende Konto:",
    "pay_prepayment_en": "Please transfer the invoice amount in advance, quoting invoice number {number}, to the "
                         "following account:",
    "outro_de": "Diese Rechnung wurde elektronisch erstellt und ist ohne Unterschrift gültig.",
    "outro_en": "This invoice was created electronically and is valid without a signature.",
    "email_body_de": "{salutation}\n\nanbei erhalten Sie unsere Rechnung {number} zu Ihrer Bestellung {order} über "
                     "{amount}.\n\nMit freundlichen Grüßen\n{signer}\n{company}",
    "email_body_en": "{salutation}\n\nPlease find attached our invoice {number} for your order {order} amounting to "
                     "{amount}.\n\nKind regards\n{signer}\n{company}",
}
VAT_MODES = [
    ("domestic", "З ПДВ (Німеччина / покупець без USt-IdNr)"),
    ("reverse_charge", "Reverse Charge — ЄС, покупець з USt-IdNr (0 %)"),
    ("export", "Експорт за межі ЄС (0 %)"),
]


class SalesInvoice(models.Model):
    number = models.CharField("Номер рахунку", max_length=40, db_index=True)
    inv_date = models.DateField("Дата рахунку")
    service_date = models.DateField("Дата поставки (Leistungsdatum)", null=True, blank=True)
    due_date = models.DateField("Оплатити до", null=True, blank=True)
    sales_order = models.ForeignKey("sales.SalesOrder", on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="sales_invoices", verbose_name="Замовлення")
    language = models.CharField("Мова документа", max_length=5, choices=LANGS, default="de")
    currency = models.CharField("Валюта", max_length=3, default="EUR")
    bill_to = models.JSONField("Рахунок на", default=dict, blank=True,
                               help_text="{company, name, street, zip_city, country, vat_id, email}")
    ship_to = models.JSONField("Доставка на", default=dict, blank=True, help_text="Порожньо — та сама адреса")
    customer_number = models.CharField("Kundennummer", max_length=40, blank=True, default="")
    order_ref = models.CharField("Замовлення (номер)", max_length=60, blank=True, default="")
    customer_po = models.CharField("Номер замовлення клієнта", max_length=60, blank=True, default="")
    lines = models.JSONField("Позиції", default=list, blank=True,
                             help_text="[{description, part_no, qty, unit, unit_price}]")
    shipping_cost = models.DecimalField("Доставка (нетто)", max_digits=12, decimal_places=2, default=Decimal("0"),
                                        validators=[MinValueValidator(Decimal("0"))])
    vat_mode = models.CharField("ПДВ", max_length=20, choices=VAT_MODES, default="domestic")
    vat_rate = models.DecimalField("Ставка ПДВ, %", max_digits=5, decimal_places=2, default=Decimal("19"))
    payment_method = models.CharField("Спосіб оплати", max_length=20, blank=True, default="")
    pay_kind = models.CharField("Текст оплати", max_length=12, choices=PAY_KINDS, default="invoice",
                                help_text="Уже оплачено — без банківських реквізитів у рахунку.")
    intro_text = models.TextField("Вступ", blank=True, default="")
    payment_text = models.TextField("Оплата (текст у рахунку)", blank=True, default="")
    vat_note = models.TextField("Примітка про ПДВ", blank=True, default="")
    outro_text = models.TextField("Завершення", blank=True, default="")
    signer_name = models.CharField("Підписант", max_length=120, blank=True, default="")
    signer_position = models.CharField("Посада підписанта", max_length=120, blank=True, default="")
    docx_file = models.FileField("DOCX", upload_to="sales_invoices/", blank=True)
    pdf_file = models.FileField("PDF", upload_to="sales_invoices/", blank=True)
    sent_at = models.DateTimeField("Надіслано", null=True, blank=True)
    sent_to = models.CharField("Кому надіслано", max_length=255, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+", verbose_name="Створив")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Рахунок (Rechnung)"
        verbose_name_plural = "🧾 Рахунки (Rechnung)"
        ordering = ["-inv_date", "-pk"]

    def __str__(self):
        return self.number

    @property
    def goods_total(self) -> Decimal:
        total = Decimal("0")
        for line in self.lines or []:
            try:
                total += (Decimal(str(line.get("qty") or 0)) * Decimal(str(line.get("unit_price") or 0))).quantize(
                    Decimal("0.01"))
            except Exception:  # noqa: BLE001 — зіпсований рядок не ламає список
                continue
        return total

    @property
    def net_total(self) -> Decimal:
        return self.goods_total + Decimal(self.shipping_cost or 0)

    @property
    def effective_vat_rate(self) -> Decimal:
        return Decimal(self.vat_rate or 0) if self.vat_mode == "domestic" else Decimal("0")

    @property
    def vat_total(self) -> Decimal:
        return (self.net_total * self.effective_vat_rate / 100).quantize(Decimal("0.01"))

    @property
    def gross_total(self) -> Decimal:
        return self.net_total + self.vat_total


class InvoiceSettings(models.Model):
    """Налаштування рахунків (синглтон pk=1): усе, що не міняється від рахунку до рахунку."""
    LANG_AUTO = "auto"
    number_prefix = models.CharField(
        "Префікс номера (не з сайту)", max_length=20, default="RE-", blank=True,
        help_text="Замовлення з інтернет-магазину — номер магазину на кшталт CI-31728-001 (Kundennummer і порядковий номер, як у кабінеті покупця). "
                  "Інші замовлення — префікс + номер замовлення: «RE-PO-1234».")
    default_language = models.CharField(
        "Мова за замовчуванням", max_length=5, default=LANG_AUTO,
        choices=[(LANG_AUTO, "Автоматично: Німеччина / Австрія / Швейцарія — Deutsch, інші — English")] + LANGS)
    ORDER_BUTTON = [("quick", "⚡ Одразу сформувати рахунок із даних замовлення"),
                    ("form", "✏️ Відкрити форму рахунку")]
    order_button = models.CharField(
        "Клік «🧾 Rechnung» у картці замовлення", max_length=10, choices=ORDER_BUTTON, default="quick",
        help_text="Правий клік на кнопці — інший варіант.")
    due_days = models.PositiveSmallIntegerField("Оплатити протягом, днів (рахунок)", default=14)
    prepayment_due_days = models.PositiveSmallIntegerField("Оплатити протягом, днів (передоплата)", default=7)
    vat_rate = models.DecimalField("Ставка ПДВ за замовчуванням, %", max_digits=5, decimal_places=2,
                                   default=Decimal("19"))
    home_country = models.CharField("Країна фірми (ISO)", max_length=2, default="DE",
                                    help_text="Для вибору ПДВ: своя країна — з ПДВ, ЄС з USt-IdNr — Reverse Charge, "
                                              "поза ЄС — експорт.")
    signer_name = models.CharField("Підписант за замовчуванням", max_length=120, blank=True, default="",
                                   help_text="Порожньо — як в останньому Commercial Invoice / Packing List.")
    signer_position = models.CharField("Посада підписанта", max_length=120, blank=True, default="")
    payment_info = models.TextField(
        "Банківські реквізити", blank=True, default="",
        help_text="Блок «Bankverbindung» у рахунку. Порожньо — з «Бухгалтерія → Налаштування компанії» (банк, IBAN, BIC).")
    # Тексти — {number}, {date}, {due_date}, {amount}, {order}, {customer} підставляються
    intro_de = models.TextField("Вступ (DE)", blank=True, default=DEFAULT_TEXTS["intro_de"])
    intro_en = models.TextField("Вступ (EN)", blank=True, default=DEFAULT_TEXTS["intro_en"])
    pay_invoice_de = models.TextField("Оплата за рахунком (DE)", blank=True, default=DEFAULT_TEXTS["pay_invoice_de"])
    pay_invoice_en = models.TextField("Оплата за рахунком (EN)", blank=True, default=DEFAULT_TEXTS["pay_invoice_en"])
    pay_prepayment_de = models.TextField("Передоплата / Vorkasse (DE)", blank=True, default=DEFAULT_TEXTS["pay_prepayment_de"])
    pay_prepayment_en = models.TextField("Передоплата / Vorkasse (EN)", blank=True, default=DEFAULT_TEXTS["pay_prepayment_en"])
    pay_paid_de = models.TextField("Уже оплачено (DE)", blank=True,
                                   default="Der Rechnungsbetrag wurde bereits bezahlt ({method}). Vielen Dank!")
    pay_paid_en = models.TextField("Уже оплачено (EN)", blank=True,
                                   default="The invoice amount has already been paid ({method}). Thank you!")
    vat_reverse_de = models.TextField(
        "Примітка Reverse Charge (DE)", blank=True,
        default="Steuerfreie innergemeinschaftliche Lieferung gemäß § 4 Nr. 1b UStG. "
                "Steuerschuldnerschaft des Leistungsempfängers (Reverse Charge).")
    vat_reverse_en = models.TextField(
        "Примітка Reverse Charge (EN)", blank=True,
        default="VAT-exempt intra-community supply. Reverse charge: VAT to be accounted for by the recipient.")
    vat_export_de = models.TextField("Примітка експорт (DE)", blank=True,
                                     default="Steuerfreie Ausfuhrlieferung gemäß § 4 Nr. 1a UStG.")
    vat_export_en = models.TextField("Примітка експорт (EN)", blank=True,
                                     default="VAT-exempt export delivery.")
    outro_de = models.TextField("Завершення (DE)", blank=True, default=DEFAULT_TEXTS["outro_de"])
    outro_en = models.TextField("Завершення (EN)", blank=True, default=DEFAULT_TEXTS["outro_en"])
    email_subject_de = models.CharField("Тема листа (DE)", max_length=200, blank=True,
                                        default="Rechnung {number}")
    email_subject_en = models.CharField("Тема листа (EN)", max_length=200, blank=True,
                                        default="Invoice {number}")
    email_body_de = models.TextField("Текст листа (DE)", blank=True, default=DEFAULT_TEXTS["email_body_de"])
    email_body_en = models.TextField("Текст листа (EN)", blank=True, default=DEFAULT_TEXTS["email_body_en"])
    auto_send = models.BooleanField(
        "Надсилати рахунок покупцю автоматично", default=False,
        help_text="Замовлення з інтернет-магазину: Vorkasse й рахунок — одразу після замовлення, онлайн-оплата "
                  "(Stripe, PayPal) — коли оплату отримано. Лист — з пошти магазину, рахунок PDF у вкладенні, "
                  "один раз на замовлення.")
    auto_log = models.JSONField("Останні автоматичні листи", default=list, blank=True)
    file_name = models.CharField("Назва файлу", max_length=80, default="Rechnung_{number}",
                                 help_text="{number}, {order} — без розширення. Файл іде й у документи замовлення "
                                           "(і в кабінет покупця сайту).")
    template = models.FileField("Власний шаблон DOCX", upload_to="invoice_templates/", blank=True,
                                help_text="Порожньо — шаблон на бланку Commercial Invoice (логотип, реквізити, печатка, "
                                          "підпис).")

    class Meta:
        verbose_name = "Налаштування рахунків"
        verbose_name_plural = "⚙️ Налаштування рахунків"

    def __str__(self):
        return "Налаштування рахунків"

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def text(self, key: str, lang: str) -> str:
        """Текст мовою документа: de / en; de_en — обидва абзацами (DE, потім EN)."""
        if lang == "de_en":
            de, en = getattr(self, f"{key}_de", ""), getattr(self, f"{key}_en", "")
            return "\n".join(x for x in (de, en) if x)
        return getattr(self, f"{key}_{'de' if lang == 'de' else 'en'}", "")
