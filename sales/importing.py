"""Імпорт замовлень з Excel: поля Minerva й авто-маппінг колонок (sales/admin.py → import_excel_view).

Авто-маппінг (guess_mapping) — за назвою колонки:
  1. точний збіг з назвою поля або підказкою («order_date», «Order Date», «Bestelldatum»…);
  2. підказка цілими словами в назві колонки («Ship Date» → дата відправки), довша підказка важить більше;
  3. підказка як початок слова («Orderno» → номер замовлення).
Пари «колонка → поле» розподіляються від найкращої: одне поле — не більше однієї колонки, одна колонка —
одне поле. Тож «order_date» більше не стає номером замовлення лише через слово «order».
"""
from __future__ import annotations

import re

ORDER_FIELDS = [
    ("order_number",      "Номер замовлення * (ключ)"),
    ("source",            "Джерело (digikey/nova_post/manual)"),
    ("order_date",        "Дата замовлення"),
    ("shipped_at",        "Дата відправки"),
    ("shipping_deadline", "Дедлайн відправки"),
    ("shipping_courier",  "Кур'єр"),
    ("tracking_number",   "Трекінг номер"),
    ("lieferschein_nr",   "Lieferschein Nr"),
    ("shipping_region",   "Регіон доставки"),
    ("shipping_address",  "Адреса доставки (legacy)"),
    ("addr_street",       "Вулиця, будинок"),
    ("addr_city",         "Місто"),
    ("addr_zip",          "Поштовий індекс"),
    ("addr_state",        "Штат / провінція (ISO-2: CA/NY/TX)"),
    ("addr_country",      "Країна (ISO-2: DE/UA/PL)"),
    ("client",            "Клієнт / Компанія (білінг)"),
    ("contact_name",      "Контактна особа (білінг)"),
    ("phone",             "Телефон (білінг)"),
    ("email",             "Email (білінг)"),
    ("ship_name",         "Ім'я отримувача посилки"),
    ("ship_company",      "Компанія отримувача"),
    ("ship_phone",        "Телефон доставки"),
    ("ship_email",        "Email доставки"),
    ("currency",          "Валюта замовлення"),
    ("shipping_cost",     "Вартість доставки"),
    ("shipping_currency", "Валюта доставки"),
    ("status",            "Статус"),
    ("document_type",     "Тип документу"),
]
LINE_FIELDS = [
    ("sku_raw",         "SKU товару (рядок)"),
    ("qty",             "Кількість (рядок)"),
    ("unit_price",      "Ціна за одиницю"),
    ("total_price_line", "Загальна сума рядка"),
    ("currency_line",   "Валюта рядка"),
]
FIELD_LABELS = dict(ORDER_FIELDS + LINE_FIELDS)

# Підказки назв колонок (англ. / укр. / нім.). Назва поля (order_number…) — теж підказка.
HINTS = {
    "order_number":      ["order", "order no", "order nr", "order id", "order number", "sales order", "so#", "so",
                          "№", "замовлення", "номер замовлення", "bestellung", "bestellnummer", "auftrag",
                          "auftragsnummer"],
    "source":            ["source", "джерело", "quelle", "channel", "marketplace"],
    "order_date":        ["order date", "date", "дата замовлення", "дата", "bestelldatum", "datum", "created"],
    "shipped_at":        ["shipped", "ship date", "shipped date", "shipped at", "відправлено", "відправка",
                          "дата відправки", "versanddatum", "versendet"],
    "shipping_deadline": ["deadline", "must ship", "must ship by", "ship by", "дедлайн", "дедлайн відправки"],
    "shipping_courier":  ["courier", "carrier", "delivery method", "shipping method", "кур'єр", "перевізник",
                          "метод", "versandart", "versanddienstleister"],
    "tracking_number":   ["tracking", "tracking number", "tracking no", "track no", "awb", "трекінг",
                          "трек номер", "sendungsnummer"],
    "lieferschein_nr":   ["lieferschein", "lieferschein nr", "ls nr", "delivery note"],
    "shipping_region":   ["region", "регіон"],
    "shipping_address":  ["shipping address", "ship to", "ship to address", "адреса доставки", "lieferadresse"],
    "addr_street":       ["street", "address", "address line 1", "вулиця", "адреса", "straße", "strasse"],
    "addr_city":         ["city", "town", "місто", "stadt", "ort"],
    "addr_zip":          ["zip", "zip code", "postal", "postal code", "postcode", "індекс", "plz"],
    "addr_state":        ["state", "province", "штат", "провінція", "bundesland"],
    "addr_country":      ["country", "country code", "країна", "land"],
    "client":            ["client", "customer", "company", "клієнт", "компанія", "firma", "kunde"],
    "contact_name":      ["contact", "contact name", "contact person", "person", "контакт", "контактна особа",
                          "особа", "ansprechpartner"],
    "phone":             ["phone", "telephone", "mobile", "телефон", "тел", "telefon"],
    "email":             ["email", "e mail", "mail", "customer email", "пошта", "e-mail"],
    "ship_name":         ["recipient", "recipient name", "ship to name", "отримувач", "ім'я отримувача",
                          "empfänger"],
    "ship_company":      ["recipient company", "ship to company", "компанія отримувача"],
    "ship_phone":        ["recipient phone", "delivery phone", "телефон доставки"],
    "ship_email":        ["recipient email", "delivery email", "email доставки"],
    "currency":          ["currency", "curr", "валюта", "währung"],
    "shipping_cost":     ["shipping cost", "shipping costs", "freight", "ship cost", "вартість доставки", "versandkosten"],
    "shipping_currency": ["shipping currency", "валюта доставки"],
    "status":            ["status", "order status", "статус"],
    "document_type":     ["document type", "doc type", "тип документу", "тип"],
    "sku_raw":           ["sku", "part", "part no", "part number", "product number", "item", "артикул", "товар",
                          "artikel", "artikelnummer"],
    "qty":               ["qty", "quantity", "amount", "pcs", "кількість", "кіл", "к-сть", "menge", "anzahl"],
    "unit_price":        ["unit price", "price", "price per", "each", "ціна", "ціна за одиницю", "preis",
                          "einzelpreis", "stückpreis"],
    "total_price_line":  ["total price", "line total", "total", "сума рядка", "сума", "gesamtpreis", "summe"],
    "currency_line":     ["line currency", "curr line", "валюта рядка"],
}

_SEP = re.compile(r"[\s_\-./\\,;:()\[\]{}|*\"]+")


def norm(text) -> str:
    """Назва колонки → слова через пробіл, малими літерами («Order_Date» → «order date»)."""
    return " ".join(w for w in _SEP.split(str(text or "").lower()) if w)


def _score(col: str, hint: str) -> int:
    if not col or not hint:
        return 0
    if col == hint:
        return 1000 + len(hint)
    padded = f" {col} "
    if f" {hint} " in padded:
        # англійська назва — головне слово в кінці: «Customer Email» — email, «Order Total» — сума
        tail = 50 if hint.isascii() and padded.endswith(f" {hint} ") else 0
        return 500 + len(hint) + tail
    if len(hint) >= 3 and any(w.startswith(hint) for w in col.split()):
        return 100 + len(hint)
    return 0


def field_scores(column) -> dict:
    """{поле: найкращий бал} для однієї колонки."""
    col = norm(column)
    out = {}
    for field, hints in HINTS.items():
        best = max([_score(col, norm(h)) for h in hints + [field]] or [0])
        if best:
            out[field] = best
    return out


def guess_mapping(columns) -> list:
    """Для кожної колонки — поле Minerva або «--». Жодне поле не дістається двом колонкам."""
    pairs = []
    for idx, col in enumerate(columns):
        for field, score in field_scores(col).items():
            pairs.append((score, -idx, idx, field))
    result = ["--"] * len(columns)
    used = set()
    for score, _neg, idx, field in sorted(pairs, reverse=True):
        if result[idx] != "--" or field in used:
            continue
        result[idx] = field
        used.add(field)
    return result


def duplicate_fields(mappings: dict) -> dict:
    """mappings {номер колонки: поле} → {поле: [номери колонок]} для полів, вибраних кілька разів."""
    by_field = {}
    for idx, field in sorted(mappings.items()):
        by_field.setdefault(field, []).append(idx)
    return {f: cols for f, cols in by_field.items() if len(cols) > 1}


def column_letter(idx: int) -> str:
    """0 → A, 25 → Z, 26 → AA."""
    s, n = "", idx + 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s
