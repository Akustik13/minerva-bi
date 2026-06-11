"""
generate_invoice.py  v2  (Windows + Linux + Mac)
=================================================
Генератор інвойсів Sevskiy GmbH → DigiKey Marketplace.
Використовує Invoice_10234.docx як шаблон.

ЗАЛЕЖНОСТІ: тільки стандартна бібліотека Python.
Не потрібен subprocess / python3 / LibreOffice.

Запуск:
    python generate_invoice.py              # тест (SAMPLE_ORDER)
    python generate_invoice.py --json order.json
    python generate_invoice.py --out C:/invoices/Invoice_10235.docx

ЗМІННІ ПОЛЯ:
    invoice_number   str   — номер інвойсу ("10235"), Minerva: max+1
    invoice_date     str   — дата MM/DD/YYYY
    digikey_order_no str   — Sales Order # з DigiKey Marketplace
    order_date       str   — Order Date MM/DD/YYYY
    shipment_date    str   — Ship Date MM/DD/YYYY
    items            list  — позиції замовлення (see below)
    discount_amount  float — знижка, від'ємна (-39.95)
    shipping_charges float — доставка (29.95)
    shipped_to       dict  — адреса отримувача (see below)

item:
    {"part_no": "3228-AN220201-04A-ND", "description": "RF Antennas",
     "qty": 1.0, "unit_price": 79.69}

shipped_to:
    {"company": "SENSOFUSION OY", "contact": "AKI NYYSSOLA",
     "address1": "KOIVULEHDONTIE 20A", "city_zip": "VANTAA, 01510",
     "country": "FIN", "vat_id": "FI27577221"}
"""

import json, re, shutil, argparse, zipfile, tempfile
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP

BASE_DIR      = Path(__file__).parent.parent  # shipping/
TEMPLATE_PATH = BASE_DIR / "templates_docx" / "invoice_template.docx"
OUTPUT_DIR    = BASE_DIR / "invoices_output"
VAT_RATE      = Decimal("0.19")

# ── ТЕСТОВІ ДАНІ ─────────────────────────────────────────────────────────────
SAMPLE_ORDER = {
    "invoice_number":   "10235",
    "invoice_date":     "06/10/2026",
    "digikey_order_no": "99680001",
    "order_date":       "06/09/2026",
    "shipment_date":    "06/10/2026",
    "items": [
        {"part_no": "3228-AN220201-04A-ND", "description": "RF Antennas",
         "qty": 2.0, "unit_price": 79.69},
    ],
    "discount_amount":  -39.85,
    "shipping_charges": 29.95,
    "shipped_to": {
        "company":  "TEST COMPANY GMBH",
        "contact":  "MAX MUSTERMANN",
        "address1": "MUSTERSTRASSE 1",
        "city_zip": "10115 BERLIN",
        "country":  "DE",
        "vat_id":   "DE123456789",
    },
}

# ── РОЗРАХУНКИ ────────────────────────────────────────────────────────────────
def totals(order):
    s = sum(Decimal(str(i["qty"])) * Decimal(str(i["unit_price"])) for i in order["items"])
    sub = (s + Decimal(str(order["discount_amount"])) + Decimal(str(order["shipping_charges"]))
           ).quantize(Decimal("0.01"), ROUND_HALF_UP)
    vat = (sub * VAT_RATE).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return {"sub": sub, "vat": vat, "total": (sub + vat).quantize(Decimal("0.01"), ROUND_HALF_UP)}


def fmt(v):
    d = Decimal(str(v)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    s = f"{abs(d):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"-  {s}" if d < 0 else s


# ── XML РЯДКИ ТАБЛИЦІ ────────────────────────────────────────────────────────
def _b(left=True, right=True):
    t = '<w:top w:val="single" w:sz="6" w:space="0" w:color="000000"/>'
    b = '<w:bottom w:val="single" w:sz="6" w:space="0" w:color="000000"/>'
    l = '<w:left w:val="single" w:sz="6" w:space="0" w:color="000000"/>' if left else ''
    r = '<w:right w:val="single" w:sz="6" w:space="0" w:color="000000"/>' if right else ''
    return f'<w:tcBorders>{t}{l}{b}{r}</w:tcBorders>'


def item_row(pos, part_no, desc, qty, price):
    q = Decimal(str(qty)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    p = Decimal(str(price)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    a = (q * p).quantize(Decimal("0.01"), ROUND_HALF_UP)
    qs = f"{q:.1f}".replace(".", ",")
    return (
        f'<w:tr>'
        f'<w:tc><w:tcPr><w:tcW w:w="585" w:type="dxa"/>{_b(left=False)}</w:tcPr>'
        f'<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:t>{pos}</w:t></w:r></w:p></w:tc>'
        f'<w:tc><w:tcPr><w:tcW w:w="4609" w:type="dxa"/>{_b()}</w:tcPr>'
        f'<w:p><w:r><w:rPr><w:b/><w:bCs/></w:rPr><w:t>{part_no}</w:t></w:r></w:p>'
        f'<w:p><w:r><w:t>{desc}</w:t></w:r></w:p></w:tc>'
        f'<w:tc><w:tcPr><w:tcW w:w="1276" w:type="dxa"/>{_b()}<w:vAlign w:val="center"/></w:tcPr>'
        f'<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:t>{qs}</w:t></w:r></w:p></w:tc>'
        f'<w:tc><w:tcPr><w:tcW w:w="1276" w:type="dxa"/>{_b()}<w:vAlign w:val="center"/></w:tcPr>'
        f'<w:p><w:pPr><w:jc w:val="right"/></w:pPr><w:r><w:t>{fmt(p)}</w:t></w:r></w:p></w:tc>'
        f'<w:tc><w:tcPr><w:tcW w:w="1275" w:type="dxa"/>{_b(right=False)}<w:vAlign w:val="center"/></w:tcPr>'
        f'<w:p><w:pPr><w:jc w:val="right"/></w:pPr><w:r><w:t>{fmt(a)}</w:t></w:r></w:p></w:tc>'
        f'</w:tr>'
    )


def simple_row(label, amount, bold=False):
    rpr = "<w:b/><w:bCs/><w:iCs/>" if bold else "<w:bCs/><w:iCs/>"
    br  = "<w:b/>" if bold else ""
    return (
        f'<w:tr>'
        f'<w:tc><w:tcPr><w:tcW w:w="585" w:type="dxa"/>{_b(left=False)}</w:tcPr>'
        f'<w:p><w:pPr><w:jc w:val="center"/></w:pPr></w:p></w:tc>'
        f'<w:tc><w:tcPr><w:tcW w:w="4609" w:type="dxa"/>{_b()}</w:tcPr>'
        f'<w:p><w:pPr><w:rPr>{rpr}</w:rPr></w:pPr>'
        f'<w:r><w:rPr>{rpr}</w:rPr><w:t>{label}</w:t></w:r></w:p></w:tc>'
        f'<w:tc><w:tcPr><w:tcW w:w="1276" w:type="dxa"/>{_b()}</w:tcPr><w:p/></w:tc>'
        f'<w:tc><w:tcPr><w:tcW w:w="1276" w:type="dxa"/>{_b()}</w:tcPr><w:p/></w:tc>'
        f'<w:tc><w:tcPr><w:tcW w:w="1275" w:type="dxa"/>{_b(right=False)}</w:tcPr>'
        f'<w:p><w:pPr><w:jc w:val="right"/></w:pPr>'
        f'<w:r><w:rPr>{br}</w:rPr><w:t>{amount}</w:t></w:r></w:p></w:tc>'
        f'</w:tr>'
    )


# ── ГЕНЕРАТОР ─────────────────────────────────────────────────────────────────
def generate(order: dict, output_path: Path, template: Path = TEMPLATE_PATH) -> Path:
    T = totals(order)
    work = Path(tempfile.mkdtemp(prefix=f"inv_{order['invoice_number']}_"))
    try:
        # Розпакувати шаблон
        with zipfile.ZipFile(template, 'r') as z:
            z.extractall(work)

        # ── HEADER: Invoice No. ───────────────────────────────────────────────
        # Шаблон (raw zip): "Invoice No.: 102" + "34" в окремих <w:t> runs
        hdr_path = work / "word" / "header1.xml"
        hdr = hdr_path.read_text(encoding="utf-8")
        # Замінюємо "Invoice No.: 102" + видаляємо ТІЛЬКИ сусідній run з "34"
        # Цей run має унікальний w:rsidR="00FF4223" — так відрізняємо від решти документу
        hdr = re.sub(
            r'<w:t>Invoice No\.: 102</w:t></w:r>'
            r'<w:r w:rsidR="00FF4223"><w:rPr>.*?</w:rPr><w:t>34</w:t></w:r>',
            f'<w:t>Invoice No.: {order["invoice_number"]}</w:t></w:r>',
            hdr, flags=re.DOTALL
        )
        hdr_path.write_text(hdr, encoding="utf-8")

        # ── DOCUMENT.XML ──────────────────────────────────────────────────────
        doc_path = work / "word" / "document.xml"
        doc = doc_path.read_text(encoding="utf-8")

        # Your Order No. (в raw zip немає \xa0 — просто текст)
        doc = doc.replace(">99674401<",           f">{order['digikey_order_no']}<")

        # Your Order Date — розбита на 5 runs: "0"+"6"+"/"+"08"+"/2026"
        doc = re.sub(
            r'Your Order Date: \d</w:t>.*?/2026</w:t>',
            f'Your Order Date: {order["order_date"]}</w:t>',
            doc, flags=re.DOTALL, count=1
        )

        # Our Invoice No. в тілі — два runs: ">102<" + ">34<"
        # Замінюємо перший на повний номер, другий на порожньо
        doc = doc.replace(
            f'<w:t>102</w:t></w:r><w:r w:rsidR="00FF4223">',
            f'<w:t>{order["invoice_number"]}</w:t></w:r><w:r w:rsidR="00FF4223_DEL">'
        )
        doc = re.sub(
            r'<w:r w:rsidR="00FF4223_DEL">[^<]*<w:rPr>.*?</w:rPr><w:t>34</w:t></w:r>',
            '', doc, flags=re.DOTALL
        )
        # Fallback: якщо вище не спрацювало
        doc = re.sub(r'<w:t>102</w:t>.*?<w:t>34</w:t>',
                     f'<w:t>{order["invoice_number"]}</w:t>', doc, count=1, flags=re.DOTALL)

        # Our Invoice Date (розбита на окремі runs)
        doc = re.sub(
            r'Our Invoice Date: 0</w:t>.*?/2026</w:t>',
            f'Our Invoice Date: {order["invoice_date"]}</w:t>',
            doc, flags=re.DOTALL
        )

        # Date of shipment (теж розбита)
        doc = re.sub(
            r'Date of shipment: </w:t>.*?/2026</w:t>',
            f'Date of shipment: {order["shipment_date"]}</w:t>',
            doc, flags=re.DOTALL
        )
        # Запасний варіант якщо не розбита
        doc = re.sub(r'Date of shipment: \d{2}/\d{2}/\d{4}',
                     f'Date of shipment: {order["shipment_date"]}', doc)

        # Shipped To (шаблон містить ці точні рядки у <w:t>)
        st = order["shipped_to"]
        doc = doc.replace("LOG.IN SRL",             st["company"])
        doc = doc.replace("FILIBERTO\xa0LANCIOTTI", st["contact"])
        doc = doc.replace("VIA AURELIA, 714",        st["address1"])
        doc = doc.replace("ROMA,\xa000165\xa0ITA",   f"{st['city_zip']} {st['country']}")
        doc = doc.replace("IT01520721000",            st["vat_id"])

        # Таблиця позицій: знижка %
        s_sum = sum(Decimal(str(i["qty"])) * Decimal(str(i["unit_price"])) for i in order["items"])
        disc_pct = int(round(abs(Decimal(str(order["discount_amount"])) / s_sum * 100)))

        rows = ""
        for idx, it in enumerate(order["items"], 1):
            rows += item_row(idx, it["part_no"], it["description"], it["qty"], it["unit_price"])
        rows += simple_row(f"Discount {disc_pct}%",   fmt(order["discount_amount"]))
        rows += simple_row("Shipping Charges",         fmt(order["shipping_charges"]))
        rows += simple_row("Total Amount without VAT", fmt(T["sub"]))
        rows += simple_row("VAT 19% ",                 fmt(T["vat"]))
        rows += simple_row("Total Amount with VAT",    fmt(T["total"]), bold=True)

        # Замінюємо всі data-рядки між header row і </w:tbl>
        doc = re.sub(
            r'(<w:t>Amount, USD</w:t>.*?</w:tr>)(.*?)(<\s*/w:tbl>)',
            lambda m: m.group(1) + rows + m.group(3),
            doc, flags=re.DOTALL, count=1
        )

        doc_path.write_text(doc, encoding="utf-8")

        # Упакувати назад у .docx
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = output_path.with_suffix(".tmp.docx")
        with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as z:
            for p in ("[Content_Types].xml", "_rels/.rels"):
                f = work / p
                if f.exists():
                    z.write(f, p)
            for f in work.rglob("*"):
                if f.is_file():
                    arc = f.relative_to(work).as_posix()
                    if arc not in ("[Content_Types].xml", "_rels/.rels"):
                        z.write(f, arc)
        if output_path.exists():
            output_path.unlink()
        tmp.rename(output_path)

    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(f"✓  Інвойс збережено: {output_path}")
    return output_path


# ── PLACEHOLDER GENERATOR (для демо — ставить назви змінних) ─────────────────
def _item_row_text(pos, part_no, desc, qty_s, price_s, amount_s):
    """Текстова версія item_row — всі значення вже як рядки."""
    return (
        f'<w:tr>'
        f'<w:tc><w:tcPr><w:tcW w:w="585" w:type="dxa"/>{_b(left=False)}</w:tcPr>'
        f'<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:t>{pos}</w:t></w:r></w:p></w:tc>'
        f'<w:tc><w:tcPr><w:tcW w:w="4609" w:type="dxa"/>{_b()}</w:tcPr>'
        f'<w:p><w:r><w:rPr><w:b/><w:bCs/></w:rPr><w:t>{part_no}</w:t></w:r></w:p>'
        f'<w:p><w:r><w:t>{desc}</w:t></w:r></w:p></w:tc>'
        f'<w:tc><w:tcPr><w:tcW w:w="1276" w:type="dxa"/>{_b()}<w:vAlign w:val="center"/></w:tcPr>'
        f'<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:t>{qty_s}</w:t></w:r></w:p></w:tc>'
        f'<w:tc><w:tcPr><w:tcW w:w="1276" w:type="dxa"/>{_b()}<w:vAlign w:val="center"/></w:tcPr>'
        f'<w:p><w:pPr><w:jc w:val="right"/></w:pPr><w:r><w:t>{price_s}</w:t></w:r></w:p></w:tc>'
        f'<w:tc><w:tcPr><w:tcW w:w="1275" w:type="dxa"/>{_b(right=False)}<w:vAlign w:val="center"/></w:tcPr>'
        f'<w:p><w:pPr><w:jc w:val="right"/></w:pPr><w:r><w:t>{amount_s}</w:t></w:r></w:p></w:tc>'
        f'</w:tr>'
    )


def generate_placeholder(output_path: Path, template: Path = TEMPLATE_PATH) -> Path:
    """Генерує .docx з шаблону, підставляючи назви змінних замість реальних значень."""
    work = Path(tempfile.mkdtemp(prefix="inv_placeholder_"))
    try:
        with zipfile.ZipFile(template, 'r') as z:
            z.extractall(work)

        # ── Header: Invoice No. ───────────────────────────────────────────────
        hdr_path = work / "word" / "header1.xml"
        hdr = hdr_path.read_text(encoding="utf-8")
        hdr = re.sub(
            r'<w:t>Invoice No\.: 102</w:t></w:r>'
            r'<w:r w:rsidR="00FF4223"><w:rPr>.*?</w:rPr><w:t>34</w:t></w:r>',
            '<w:t>Invoice No.: {{invoice_number}}</w:t></w:r>',
            hdr, flags=re.DOTALL
        )
        hdr_path.write_text(hdr, encoding="utf-8")

        # ── Document ──────────────────────────────────────────────────────────
        doc_path = work / "word" / "document.xml"
        doc = doc_path.read_text(encoding="utf-8")

        doc = doc.replace(">99674401<", ">{{digikey_order_no}}<")

        doc = re.sub(
            r'Your Order Date: \d</w:t>.*?/2026</w:t>',
            'Your Order Date: {{order_date}}</w:t>',
            doc, flags=re.DOTALL, count=1
        )

        # Invoice number in body (two-run split)
        doc = doc.replace(
            '<w:t>102</w:t></w:r><w:r w:rsidR="00FF4223">',
            '<w:t>{{invoice_number}}</w:t></w:r><w:r w:rsidR="00FF4223_DEL">'
        )
        doc = re.sub(
            r'<w:r w:rsidR="00FF4223_DEL">[^<]*<w:rPr>.*?</w:rPr><w:t>34</w:t></w:r>',
            '', doc, flags=re.DOTALL
        )
        doc = re.sub(r'<w:t>102</w:t>.*?<w:t>34</w:t>',
                     '<w:t>{{invoice_number}}</w:t>', doc, count=1, flags=re.DOTALL)

        doc = re.sub(
            r'Our Invoice Date: 0</w:t>.*?/2026</w:t>',
            'Our Invoice Date: {{invoice_date}}</w:t>',
            doc, flags=re.DOTALL
        )

        doc = re.sub(
            r'Date of shipment: </w:t>.*?/2026</w:t>',
            'Date of shipment: {{shipment_date}}</w:t>',
            doc, flags=re.DOTALL
        )
        doc = re.sub(r'Date of shipment: \d{2}/\d{2}/\d{4}',
                     'Date of shipment: {{shipment_date}}', doc)

        doc = doc.replace("LOG.IN SRL",             "{{shipped_to.company}}")
        doc = doc.replace("FILIBERTO\xa0LANCIOTTI", "{{shipped_to.contact}}")
        doc = doc.replace("VIA AURELIA, 714",        "{{shipped_to.address1}}")
        doc = doc.replace("ROMA,\xa000165\xa0ITA",   "{{shipped_to.city_zip}} {{shipped_to.country}}")
        doc = doc.replace("IT01520721000",            "{{shipped_to.vat_id}}")

        # ── Table rows ────────────────────────────────────────────────────────
        rows  = _item_row_text(1,
                               "{{items[i].part_no}}",
                               "{{items[i].description}}",
                               "{{items[i].qty}}",
                               "{{items[i].unit_price}}",
                               "{{items[i].amount}}")
        rows += simple_row("Discount {{discount_pct}}%",   "{{discount_amount}}")
        rows += simple_row("Shipping Charges",              "{{shipping_charges}}")
        rows += simple_row("Total Amount without VAT",      "{{subtotal}}")
        rows += simple_row("VAT 19% ",                      "{{vat_amount}}")
        rows += simple_row("Total Amount with VAT",         "{{total_amount}}", bold=True)

        doc = re.sub(
            r'(<w:t>Amount, USD</w:t>.*?</w:tr>)(.*?)(<\s*/w:tbl>)',
            lambda m: m.group(1) + rows + m.group(3),
            doc, flags=re.DOTALL, count=1
        )

        doc_path.write_text(doc, encoding="utf-8")

        # ── Repack ────────────────────────────────────────────────────────────
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = output_path.with_suffix(".tmp.docx")
        with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as z:
            for p in ("[Content_Types].xml", "_rels/.rels"):
                f = work / p
                if f.exists():
                    z.write(f, p)
            for f in work.rglob("*"):
                if f.is_file():
                    arc = f.relative_to(work).as_posix()
                    if arc not in ("[Content_Types].xml", "_rels/.rels"):
                        z.write(f, arc)
        if output_path.exists():
            output_path.unlink()
        tmp.rename(output_path)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    return output_path


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Sevskiy Invoice Generator")
    ap.add_argument("--json",   help="JSON файл із даними замовлення")
    ap.add_argument("--sample", action="store_true", help="Тест із SAMPLE_ORDER")
    ap.add_argument("--out",    help="Шлях вихідного .docx")
    args = ap.parse_args()

    if args.json:
        order = json.loads(Path(args.json).read_text(encoding="utf-8"))
    else:
        order = SAMPLE_ORDER
        print("INFO: використовуються тестові дані (SAMPLE_ORDER)")

    num = order.get("invoice_number", "00000")
    out = Path(args.out) if args.out else OUTPUT_DIR / f"Invoice_{num}.docx"
    generate(order, out)


if __name__ == "__main__":
    main()
