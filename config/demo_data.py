"""
Demo data seeder for Minerva BI.
Realistic acoustic-panel business data. Modular — seed only what you need.
All generated records are marked with [demo] or DEMO- prefix for safe cleanup.
"""
import hashlib
import datetime
import random
from decimal import Decimal

_RNG = random.Random(42)  # reproducible


def _sha(email, name):
    return hashlib.sha256(f"{email}:{name}".encode()).hexdigest()[:32]


# ─── Marker constants (used for identification + cleanup) ─────────────────────
DEMO_ORDER_PREFIXES  = ("EBDE-", "AMDE-", "WS-", "TEL-", "DEMO-")
DEMO_TRANSACTION_REF = "DEMO-INIT"
DEMO_CUSTOMER_SOURCE = "demo"
DEMO_MARKER          = "[demo]"          # appended to expenses, tasks, carrier
DEMO_BULK_SKU_PREFIX = "DEMO-P-"
DEMO_BULK_ORDER_PFX  = "DEMO-"


def _demo_orders_q():
    from django.db.models import Q
    q = Q()
    for p in DEMO_ORDER_PREFIXES:
        q |= Q(order_number__startswith=p)
    return q


# ─── Static datasets ──────────────────────────────────────────────────────────
_SOURCES = [
    {"slug": "ebay-de",   "name": "eBay DE",          "color": "#f5a623", "order": 1},
    {"slug": "amazon-de", "name": "Amazon DE",         "color": "#ff9900", "order": 2},
    {"slug": "webshop",   "name": "Webshop",           "color": "#4caf50", "order": 3},
    {"slug": "telefon",   "name": "Телефон/Особисто",  "color": "#607d8b", "order": 4},
]

_CATEGORIES = [
    {"slug": "acoustic-panels", "name": "Акустичні панелі", "color": "#2196f3", "order": 1},
    {"slug": "accessories",     "name": "Аксесуари",        "color": "#9c27b0", "order": 2},
    {"slug": "raw-materials",   "name": "Сировина",         "color": "#795548", "order": 3},
]

_PRODUCTS = [
    # sku, name, category_slug, purchase_price, sale_price, reorder_point, unit_type
    ("AK-6060-GR",  "Акустична панель 60×60 (сіра)",   "acoustic-panels", "4.50",  "12.90", 20, "piece"),
    ("AK-6060-BK",  "Акустична панель 60×60 (чорна)",  "acoustic-panels", "4.50",  "12.90", 20, "piece"),
    ("AK-6060-BG",  "Акустична панель 60×60 (бежева)", "acoustic-panels", "4.50",  "13.50", 15, "piece"),
    ("AK-12060-GR", "Акустична панель 120×60 (сіра)",  "acoustic-panels", "8.90",  "24.90", 10, "piece"),
    ("AK-12060-BK", "Акустична панель 120×60 (чорна)", "acoustic-panels", "8.90",  "24.90", 10, "piece"),
    ("AK-3030-WH",  "Акустична панель 30×30 (біла)",   "acoustic-panels", "2.20",   "6.50", 30, "piece"),
    ("AK-CORNER",   "Кутовий поглинач",                "acoustic-panels", "6.80",  "18.90",  8, "piece"),
    ("AK-BASS-TRP", "Басова пастка 60×60×30",          "acoustic-panels", "14.20", "39.90",  5, "piece"),
    ("ACC-MOUNT",   "Монтажний комплект (12 шт)",      "accessories",     "1.80",   "4.90", 25, "set"),
    ("ACC-FRAME",   "Рамка алюмінієва 60×60",          "accessories",     "3.10",   "8.50", 15, "piece"),
    ("RAW-FOAM-50", "Акустична піна 50мм (рулон 10м²)","raw-materials",  "18.00",  "0.00",  3, "piece"),
    ("RAW-FABRIC",  "Тканина акустична (5м погонних)", "raw-materials",  "12.50",  "0.00",  5, "meter"),
]

_CUSTOMERS = [
    # email, name, company, country, city, segment, status
    ("mueller.thomas@gmail.com",  "Thomas Müller",   "Müller GmbH",            "DE", "München",   "b2b", "active"),
    ("schmidt.anna@web.de",       "Anna Schmidt",    "",                       "DE", "Hamburg",   "b2c", "active"),
    ("bauer.studios@outlook.com", "Bauer Studios",   "Bauer Recording Studio", "DE", "Berlin",    "b2b", "vip"),
    ("weber.j@t-online.de",       "Jürgen Weber",    "",                       "DE", "Frankfurt", "b2c", "active"),
    ("hochmann@protonmail.com",   "Klaus Hochmann",  "Hochmann Audio",         "AT", "Wien",      "b2b", "active"),
    ("kowalski.p@gmail.com",      "Piotr Kowalski",  "",                       "PL", "Warszawa",  "b2c", "active"),
    ("novak.studio@seznam.cz",    "Radek Novák",     "Novák Studio",           "CZ", "Praha",     "b2b", "active"),
    ("de.bruijn@gmail.com",       "Erik de Bruijn",  "",                       "NL", "Amsterdam", "b2c", "active"),
    ("akustik.pro@gmail.com",     "Petra Gruber",    "AkustikPro GmbH",        "DE", "Köln",      "b2b", "active"),
    ("martin.lehner@gmx.de",      "Martin Lehner",   "",                       "DE", "Stuttgart", "b2c", "active"),
]

_FIXED_ORDERS = [
    # (source, order_number, customer_email, days_ago, status, lines)
    # eBay DE
    ("ebay-de","EBDE-0001","mueller.thomas@gmail.com", 85,"shipped",   [("AK-6060-GR",4,"12.90"),("ACC-MOUNT",1,"4.90")]),
    ("ebay-de","EBDE-0002","schmidt.anna@web.de",       78,"shipped",   [("AK-6060-BK",6,"12.90"),("AK-6060-BG",2,"13.50")]),
    ("ebay-de","EBDE-0003","weber.j@t-online.de",       71,"shipped",   [("AK-3030-WH",12,"6.50"),("ACC-MOUNT",1,"4.90")]),
    ("ebay-de","EBDE-0004","de.bruijn@gmail.com",        64,"shipped",   [("AK-12060-GR",4,"24.90"),("ACC-FRAME",4,"8.50")]),
    ("ebay-de","EBDE-0005","schmidt.anna@web.de",        55,"shipped",   [("AK-6060-BK",8,"12.90"),("AK-CORNER",2,"18.90")]),
    ("ebay-de","EBDE-0006","kowalski.p@gmail.com",       48,"shipped",   [("AK-6060-GR",6,"12.90"),("ACC-MOUNT",2,"4.90")]),
    ("ebay-de","EBDE-0007","martin.lehner@gmx.de",       41,"shipped",   [("AK-3030-WH",20,"6.50")]),
    ("ebay-de","EBDE-0008","weber.j@t-online.de",        34,"shipped",   [("AK-6060-BG",4,"13.50"),("ACC-FRAME",2,"8.50")]),
    ("ebay-de","EBDE-0009","de.bruijn@gmail.com",         25,"shipped",   [("AK-12060-BK",3,"24.90"),("ACC-MOUNT",1,"4.90")]),
    ("ebay-de","EBDE-0010","schmidt.anna@web.de",         18,"processing",[("AK-6060-GR",10,"12.90"),("AK-6060-BK",5,"12.90")]),
    ("ebay-de","EBDE-0011","kowalski.p@gmail.com",        10,"received",  [("AK-CORNER",4,"18.90"),("ACC-MOUNT",1,"4.90")]),
    ("ebay-de","EBDE-0012","martin.lehner@gmx.de",         3,"received",  [("AK-6060-BG",6,"13.50")]),
    # Amazon DE
    ("amazon-de","AMDE-0001","bauer.studios@outlook.com", 80,"shipped",  [("AK-12060-GR",8,"24.90"),("AK-12060-BK",8,"24.90"),("ACC-FRAME",8,"8.50")]),
    ("amazon-de","AMDE-0002","akustik.pro@gmail.com",      73,"shipped",  [("AK-BASS-TRP",4,"39.90"),("AK-CORNER",6,"18.90")]),
    ("amazon-de","AMDE-0003","hochmann@protonmail.com",    66,"shipped",  [("AK-6060-GR",12,"12.90"),("AK-6060-BK",12,"12.90"),("ACC-MOUNT",3,"4.90")]),
    ("amazon-de","AMDE-0004","mueller.thomas@gmail.com",   59,"shipped",  [("AK-3030-WH",30,"6.50"),("ACC-FRAME",4,"8.50")]),
    ("amazon-de","AMDE-0005","bauer.studios@outlook.com",  44,"shipped",  [("AK-12060-GR",6,"24.90"),("AK-BASS-TRP",2,"39.90")]),
    ("amazon-de","AMDE-0006","novak.studio@seznam.cz",     30,"shipped",  [("AK-6060-GR",8,"12.90"),("AK-CORNER",4,"18.90")]),
    ("amazon-de","AMDE-0007","akustik.pro@gmail.com",      15,"processing",[("AK-12060-BK",10,"24.90"),("ACC-MOUNT",2,"4.90")]),
    ("amazon-de","AMDE-0008","hochmann@protonmail.com",     5,"received", [("AK-BASS-TRP",6,"39.90")]),
    # Webshop
    ("webshop","WS-0001","bauer.studios@outlook.com",  77,"shipped",  [("AK-12060-GR",12,"24.90"),("AK-12060-BK",12,"24.90"),("AK-BASS-TRP",4,"39.90"),("ACC-FRAME",12,"8.50")]),
    ("webshop","WS-0002","akustik.pro@gmail.com",       60,"shipped",  [("AK-6060-GR",24,"12.90"),("AK-6060-BK",12,"12.90"),("ACC-MOUNT",4,"4.90")]),
    ("webshop","WS-0003","hochmann@protonmail.com",     45,"shipped",  [("AK-CORNER",8,"18.90"),("AK-BASS-TRP",4,"39.90")]),
    ("webshop","WS-0004","novak.studio@seznam.cz",      32,"shipped",  [("AK-6060-GR",16,"12.90"),("ACC-FRAME",6,"8.50")]),
    ("webshop","WS-0005","bauer.studios@outlook.com",   20,"shipped",  [("AK-12060-GR",8,"24.90"),("AK-12060-BK",8,"24.90")]),
    ("webshop","WS-0006","akustik.pro@gmail.com",       12,"processing",[("AK-6060-BG",20,"13.50"),("ACC-MOUNT",3,"4.90")]),
    ("webshop","WS-0007","hochmann@protonmail.com",      4,"received", [("AK-BASS-TRP",8,"39.90"),("AK-CORNER",4,"18.90")]),
    # Телефон
    ("telefon","TEL-0001","mueller.thomas@gmail.com",   50,"shipped",  [("AK-12060-GR",20,"24.90"),("AK-12060-BK",20,"24.90"),("ACC-FRAME",20,"8.50")]),
    ("telefon","TEL-0002","novak.studio@seznam.cz",     28,"shipped",  [("AK-6060-GR",30,"12.90"),("AK-6060-BK",30,"12.90"),("ACC-MOUNT",6,"4.90")]),
    ("telefon","TEL-0003","bauer.studios@outlook.com",   8,"processing",[("AK-BASS-TRP",10,"39.90"),("AK-CORNER",10,"18.90"),("ACC-FRAME",10,"8.50")]),
]


# ─── Internal seeders ─────────────────────────────────────────────────────────

def _seed_sources():
    from sales.models import SalesSource
    for s in _SOURCES:
        SalesSource.objects.get_or_create(
            slug=s["slug"],
            defaults={"name": s["name"], "color": s["color"], "order": s["order"]},
        )


def _seed_categories():
    from inventory.models import ProductCategory
    for c in _CATEGORIES:
        ProductCategory.objects.get_or_create(
            slug=c["slug"],
            defaults={"name": c["name"], "color": c["color"], "order": c["order"]},
        )


def _seed_products():
    from inventory.models import Product
    product_map = {}
    for sku, name, cat, pp, sp, rp, unit in _PRODUCTS:
        obj, _ = Product.objects.get_or_create(
            sku=sku,
            defaults={
                "name": name,
                "category": cat,
                "purchase_price": Decimal(pp),
                "sale_price": Decimal(sp),
                "reorder_point": rp,
                "unit_type": unit,
                "kind": "component" if cat == "raw-materials" else "finished",
            },
        )
        product_map[sku] = obj
    return product_map


def _seed_bulk_products(count):
    from inventory.models import Product
    _COLORS = ["сірий", "чорний", "бежевий", "білий", "синій", "зелений", "бордо"]
    _SIZES  = [(30, 30), (60, 60), (120, 60), (60, 30), (90, 60), (120, 120), (30, 60)]
    created = 0
    for i in range(1, count + 1):
        sku = f"{DEMO_BULK_SKU_PREFIX}{i:05d}"
        if Product.objects.filter(sku=sku).exists():
            continue
        w, h  = _RNG.choice(_SIZES)
        color = _RNG.choice(_COLORS)
        pp = Decimal(str(round(_RNG.uniform(2.0, 25.0), 2)))
        sp = Decimal(str(round(float(pp) * _RNG.uniform(1.8, 3.5), 2)))
        Product.objects.create(
            sku=sku,
            name=f"Тест-панель {w}×{h} ({color}) #{i}",
            purchase_price=pp,
            sale_price=sp,
            reorder_point=_RNG.randint(5, 30),
            unit_type="piece",
            kind="finished",
        )
        created += 1
    return created


def _seed_customers():
    from crm.models import Customer
    customer_map = {}
    for email, name, company, country, city, seg, status in _CUSTOMERS:
        ext_key = _sha(email, name)
        obj, _ = Customer.objects.get_or_create(
            external_key=ext_key,
            defaults={
                "name": name, "email": email, "company": company,
                "country": country, "addr_city": city,
                "segment": seg, "status": status, "source": DEMO_CUSTOMER_SOURCE,
            },
        )
        customer_map[email] = obj
    return customer_map


def _seed_inventory_incoming(product_map, location, today):
    from inventory.models import InventoryTransaction
    stock_data = [
        ("AK-6060-GR", 50), ("AK-6060-BK", 50), ("AK-6060-BG", 40),
        ("AK-12060-GR", 25), ("AK-12060-BK", 25), ("AK-3030-WH", 80),
        ("AK-CORNER", 20), ("AK-BASS-TRP", 15), ("ACC-MOUNT", 60),
        ("ACC-FRAME", 35), ("RAW-FOAM-50", 8), ("RAW-FABRIC", 10),
    ]
    created = 0
    for sku, qty in stock_data:
        if sku not in product_map:
            continue
        _, c = InventoryTransaction.objects.get_or_create(
            external_key=f"demo-init-{sku}",
            defaults={
                "tx_type": "Incoming",
                "qty": Decimal(qty),
                "product": product_map[sku],
                "location": location,
                "ref_doc": DEMO_TRANSACTION_REF,
                "tx_date": today - datetime.timedelta(days=100),
            },
        )
        if c:
            created += 1
    return created


def _seed_inventory_outgoing(product_map, location, today):
    """Create outgoing stock transactions for shipped demo orders."""
    from inventory.models import InventoryTransaction
    from sales.models import SalesOrder
    shipped = SalesOrder.objects.filter(
        status="shipped", affects_stock=True
    ).filter(_demo_orders_q()).prefetch_related("lines__product")
    created = 0
    for order in shipped:
        for line in order.lines.all():
            product = line.product
            if not product or product.sku not in product_map:
                continue
            ext_key = f"demo-out-{order.order_number}-{product.sku}"
            _, c = InventoryTransaction.objects.get_or_create(
                external_key=ext_key,
                defaults={
                    "tx_type": "Outgoing",
                    "qty": -line.qty,
                    "product": product,
                    "location": location,
                    "ref_doc": order.order_number,
                    "tx_date": (order.shipped_at.date() if isinstance(order.shipped_at, datetime.datetime) else order.shipped_at) if order.shipped_at else order.order_date,
                },
            )
            if c:
                created += 1
    return created


def _seed_orders_fixed(product_map, customer_map, today):
    from sales.models import SalesOrder, SalesOrderLine
    created_orders = created_lines = 0
    for src, order_num, cust_email, days_ago, status, lines in _FIXED_ORDERS:
        order_date = today - datetime.timedelta(days=days_ago)
        shipped_at = order_date + datetime.timedelta(days=3) if status == "shipped" else None
        customer   = customer_map.get(cust_email)
        total      = sum(Decimal(str(qty)) * Decimal(up) for _, qty, up in lines)
        order, created = SalesOrder.objects.get_or_create(
            source=src, order_number=order_num,
            defaults={
                "order_date": order_date, "status": status, "shipped_at": shipped_at,
                "affects_stock": True,
                "client": customer.name if customer else "",
                "customer_key": customer.external_key if customer else "",
                "currency": "EUR", "total_price": total,
                "shipping_currency": "EUR", "shipping_cost": Decimal("4.90"),
                "addr_country": customer.country if customer else "DE",
                "document_type": "SALE",
            },
        )
        if created:
            created_orders += 1
            for sku, qty, up in lines:
                if sku not in product_map:
                    continue
                SalesOrderLine.objects.create(
                    order=order, product=product_map.get(sku), sku_raw=sku,
                    qty=Decimal(str(qty)), unit_price=Decimal(up),
                    total_price=Decimal(str(qty)) * Decimal(up), currency="EUR",
                )
                created_lines += 1
    return created_orders, created_lines


def _seed_orders_bulk(count, product_map, customer_map, today):
    from sales.models import SalesOrder, SalesOrderLine
    all_skus      = list(product_map.keys())
    all_customers = list(customer_map.values())
    sources = ["ebay-de", "amazon-de", "webshop", "telefon"]
    statuses = (["shipped"] * 60 + ["processing"] * 20 + ["received"] * 15 + ["cancelled"] * 5)
    created_orders = created_lines = 0
    for i in range(1, count + 1):
        order_num = f"{DEMO_BULK_ORDER_PFX}{i:05d}"
        if SalesOrder.objects.filter(order_number=order_num).exists():
            continue
        days_ago   = _RNG.randint(1, 365)
        order_date = today - datetime.timedelta(days=days_ago)
        status     = _RNG.choice(statuses)
        shipped_at = order_date + datetime.timedelta(days=_RNG.randint(1, 5)) if status == "shipped" else None
        customer   = _RNG.choice(all_customers) if all_customers else None
        line_skus  = _RNG.sample(all_skus, min(_RNG.randint(1, 3), len(all_skus)))
        total = Decimal("0")
        lines_data = []
        for sku in line_skus:
            p   = product_map[sku]
            qty = _RNG.randint(1, 10)
            up  = p.sale_price or Decimal("9.90")
            lt  = Decimal(str(qty)) * up
            total += lt
            lines_data.append((sku, qty, up, lt))
        order = SalesOrder.objects.create(
            source=_RNG.choice(sources), order_number=order_num,
            order_date=order_date, status=status, shipped_at=shipped_at,
            affects_stock=True,
            client=customer.name if customer else "Demo Customer",
            customer_key=customer.external_key if customer else "",
            currency="EUR", total_price=total,
            shipping_currency="EUR", shipping_cost=Decimal("4.90"),
            addr_country=customer.country if customer else "DE",
            document_type="SALE",
        )
        created_orders += 1
        for sku, qty, up, lt in lines_data:
            SalesOrderLine.objects.create(
                order=order, product=product_map.get(sku), sku_raw=sku,
                qty=Decimal(str(qty)), unit_price=up, total_price=lt, currency="EUR",
            )
            created_lines += 1
    return created_orders, created_lines


def _seed_accounting(today):
    from accounting.models import Invoice, InvoiceLine, Payment, Expense, ExpenseCategory

    # ExpenseCategory (marked with [demo])
    exp_cat_names = [
        "Витрати на доставку [demo]",
        "Пакувальні матеріали [demo]",
        "Оренда складу [demo]",
        "Маркетинг та реклама [demo]",
    ]
    cat_map = {}
    for name in exp_cat_names:
        cat, _ = ExpenseCategory.objects.get_or_create(name=name)
        cat_map[name] = cat

    # Invoices for shipped demo orders
    from sales.models import SalesOrder
    shipped_qs = SalesOrder.objects.filter(
        status="shipped", affects_stock=True
    ).filter(_demo_orders_q())
    created_invoices = created_payments = 0
    for order in shipped_qs:
        is_old = order.order_date < today - datetime.timedelta(days=14)
        inv, created = Invoice.objects.get_or_create(
            order=order,
            defaults={
                "status":     "paid" if is_old else "sent",
                "currency":   order.currency or "EUR",
                "issue_date": order.order_date or today,
                "due_date":   (order.order_date or today) + datetime.timedelta(days=14),
                "vat_rate":   Decimal("19.00"),
                "client_name": order.client or "Demo Client",
                "client_addr": ", ".join(filter(None, [
                    getattr(order, "addr_street", "") or "",
                    getattr(order, "addr_city", "") or "",
                ])),
                "notes": f"Demo invoice {DEMO_MARKER}",
            },
        )
        if created:
            created_invoices += 1
            for line in order.lines.all():
                InvoiceLine.objects.create(
                    invoice=inv,
                    description=line.sku_raw or (line.product.name if line.product else "Item"),
                    quantity=line.qty,
                    unit_price=line.unit_price or Decimal("0"),
                )
            if inv.status == "paid":
                total = inv.subtotal + inv.vat_amount
                Payment.objects.get_or_create(
                    invoice=inv,
                    defaults={"amount": total, "date": inv.due_date, "method": "bank"},
                )
                created_payments += 1

    # Demo expenses
    expense_data = [
        ("Витрати на доставку [demo]",  f"DHL щомісячна доставка {DEMO_MARKER}",       Decimal("245.00"), 55),
        ("Витрати на доставку [demo]",  f"UPS доставка Польща {DEMO_MARKER}",           Decimal("89.50"),  45),
        ("Витрати на доставку [demo]",  f"Jumingo комісія за місяць {DEMO_MARKER}",     Decimal("38.00"),  25),
        ("Пакувальні матеріали [demo]", f"Коробки партія 500 шт {DEMO_MARKER}",         Decimal("156.80"), 60),
        ("Пакувальні матеріали [demo]", f"Скотч, бульбашкова плівка {DEMO_MARKER}",    Decimal("34.20"),  20),
        ("Оренда складу [demo]",        f"Оренда складу — лютий 2026 {DEMO_MARKER}",    Decimal("800.00"), 55),
        ("Оренда складу [demo]",        f"Оренда складу — березень 2026 {DEMO_MARKER}", Decimal("800.00"), 25),
        ("Маркетинг та реклама [demo]", f"Google Ads Q1 2026 {DEMO_MARKER}",            Decimal("320.00"), 20),
        ("Маркетинг та реклама [demo]", f"Фотосесія товарів {DEMO_MARKER}",             Decimal("180.00"), 40),
        ("Маркетинг та реклама [demo]", f"eBay комісія за лютий {DEMO_MARKER}",         Decimal("215.40"), 30),
    ]
    created_expenses = 0
    for cat_name, desc, amount, days_ago in expense_data:
        _, c = Expense.objects.get_or_create(
            description=desc,
            defaults={
                "category": cat_map.get(cat_name),
                "amount":   amount,
                "currency": "EUR",
                "date":     today - datetime.timedelta(days=days_ago),
                "is_vat_deductible": True,
            },
        )
        if c:
            created_expenses += 1
    return created_invoices, created_payments, created_expenses


def _seed_shipping(today):
    from shipping.models import Carrier, Shipment
    from sales.models import SalesOrder

    carrier_name = f"DHL Demo {DEMO_MARKER}"
    carrier, _ = Carrier.objects.get_or_create(
        name=carrier_name,
        defaults={
            "carrier_type": "dhl",
            "is_active":    True,
            "is_default":   False,
            "sender_name":  "Akustik Demo GmbH",
            "sender_street":"Musterstraße 42",
            "sender_city":  "Berlin",
            "sender_zip":   "10115",
            "sender_country":"DE",
        },
    )

    shipped_qs = SalesOrder.objects.filter(
        status="shipped", affects_stock=True
    ).filter(_demo_orders_q())
    created = 0
    for order in shipped_qs:
        if Shipment.objects.filter(order=order).exists():
            continue
        shipped_date = (order.shipped_at.date() if isinstance(order.shipped_at, datetime.datetime) else order.shipped_at) if order.shipped_at else order.order_date
        days_since   = (today - shipped_date).days if shipped_date else 10
        if days_since > 14:
            status = Shipment.Status.DELIVERED
        elif days_since > 3:
            status = Shipment.Status.IN_TRANSIT
        else:
            status = Shipment.Status.LABEL_READY

        shipment = Shipment(order=order, carrier=carrier, status=status)
        shipment.copy_from_order()
        tracking = f"JD{_RNG.randint(10_000_000_000, 99_999_999_999)}"
        shipment.tracking_number     = tracking
        shipment.carrier_shipment_id = tracking
        shipment.weight_kg           = Decimal(str(round(_RNG.uniform(0.3, 5.0), 3)))
        shipment.carrier_price       = Decimal(str(round(_RNG.uniform(4.0, 18.0), 2)))
        shipment.carrier_currency    = "EUR"
        shipment.carrier_service     = "DHL Paket"
        if status == Shipment.Status.DELIVERED:
            import django.utils.timezone as _tz
            shipment.submitted_at = _tz.now() - datetime.timedelta(days=days_since)
        shipment.save()
        created += 1
    return created


def _seed_tasks(today):
    try:
        from tasks.models import Task
    except ImportError:
        return 0
    tasks_data = [
        (f"Перевірити залишок AK-6060-GR {DEMO_MARKER}", "Рівень нижче точки перезамовлення", "high",   "stock_alert",    today + datetime.timedelta(days=2)),
        (f"Відправити замовлення EBDE-0010 {DEMO_MARKER}","Замовлення в обробці більше 3 днів","high",   "deadline_alert", today + datetime.timedelta(days=1)),
        (f"Зателефонувати Bauer Studios {DEMO_MARKER}",   "VIP-клієнт, уточнити замовлення",   "medium", "manual",         today + datetime.timedelta(days=5)),
        (f"Оновити прайс-лист Q2 2026 {DEMO_MARKER}",     "Переглянути та оновити ціни",        "low",    "manual",         today + datetime.timedelta(days=14)),
        (f"Замовити пакувальні матеріали {DEMO_MARKER}",  "Залишок коробок < 50 шт",            "medium", "stock_alert",    today + datetime.timedelta(days=3)),
    ]
    created = 0
    for title, desc, priority, task_type, due_date in tasks_data:
        _, c = Task.objects.get_or_create(
            title=title,
            defaults={
                "description": desc, "priority": priority,
                "task_type": task_type, "status": "pending", "due_date": due_date,
            },
        )
        if c:
            created += 1
    return created


# ─── Main entry point ─────────────────────────────────────────────────────────

def seed_demo_data(scenarios=None, bulk_products=0, bulk_orders=0):
    """
    Seed demo data for the selected scenarios.

    scenarios: set of strings — modules to seed.
               None = all: {'products','customers','inventory','orders',
                            'accounting','shipping','tasks'}
    bulk_products: int — generate N extra random products (sku: DEMO-P-xxxxx)
    bulk_orders:   int — generate N extra random orders   (num: DEMO-xxxxx)
    """
    ALL = {"products", "customers", "inventory", "orders", "accounting", "shipping", "tasks"}
    if scenarios is None:
        scenarios = ALL
    today   = datetime.date.today()
    summary = {}
    product_map  = {}
    customer_map = {}

    # Base data (always: sources, categories, company settings)
    try:
        _seed_sources()
        _seed_categories()
    except Exception as e:
        summary["base_error"] = str(e)

    try:
        from accounting.models import CompanySettings
        CompanySettings.objects.get_or_create(pk=1, defaults={
            "name": "Akustik Demo GmbH", "legal_name": "Akustik Demo GmbH",
            "addr_street": "Musterstraße 42", "addr_city": "Berlin",
            "addr_zip": "10115", "addr_country": "DE",
            "vat_id": "DE123456789", "iban": "DE89370400440532013000",
            "bank_name": "Deutsche Bank", "swift": "DEUTDEDB",
            "email": "info@akustik-demo.de", "phone": "+49 30 12345678",
            "invoice_prefix": "INV",
        })
    except Exception:
        pass

    try:
        from config.models import SystemSettings
        cfg = SystemSettings.get()
        if cfg.company_name in ("Моя компанія", "My Company", ""):
            cfg.company_name   = "Akustik Demo GmbH"
            cfg.default_currency = "EUR"
            cfg.save()
    except Exception:
        pass

    # Products
    if "products" in scenarios:
        try:
            product_map = _seed_products()
            summary["products"] = len(product_map)
        except Exception as e:
            summary["products_error"] = str(e)

    # Bulk products
    if bulk_products > 0:
        try:
            n = _seed_bulk_products(bulk_products)
            summary["bulk_products"] = n
        except Exception as e:
            summary["bulk_products_error"] = str(e)

    # Reload full product map for downstream steps
    if not product_map or bulk_products > 0:
        try:
            from inventory.models import Product
            for p in Product.objects.all():
                product_map[p.sku] = p
        except Exception:
            pass

    # Location
    location = None
    try:
        from inventory.models import Location
        location, _ = Location.objects.get_or_create(
            code="MAIN", defaults={"name": "Основний склад"},
        )
    except Exception as e:
        summary["location_error"] = str(e)

    # Inventory incoming (purchase stock)
    if "inventory" in scenarios and product_map and location:
        try:
            n = _seed_inventory_incoming(product_map, location, today)
            summary["transactions_in"] = n
        except Exception as e:
            summary["transactions_error"] = str(e)

    # Customers
    if "customers" in scenarios:
        try:
            customer_map = _seed_customers()
            summary["customers"] = len(customer_map)
        except Exception as e:
            summary["customers_error"] = str(e)

    if not customer_map:
        try:
            from crm.models import Customer
            for c in Customer.objects.filter(source=DEMO_CUSTOMER_SOURCE):
                customer_map[c.email] = c
        except Exception:
            pass

    # Fixed orders (~30)
    if "orders" in scenarios:
        try:
            n_o, n_l = _seed_orders_fixed(product_map, customer_map, today)
            summary["orders"] = n_o
            summary["order_lines"] = n_l
        except Exception as e:
            summary["orders_error"] = str(e)

    # Bulk orders
    if bulk_orders > 0:
        try:
            n_o, n_l = _seed_orders_bulk(bulk_orders, product_map, customer_map, today)
            summary["bulk_orders"] = n_o
            summary["bulk_order_lines"] = n_l
        except Exception as e:
            summary["bulk_orders_error"] = str(e)

    # Inventory outgoing (stock written off for shipped orders)
    if "inventory" in scenarios and product_map and location:
        try:
            n = _seed_inventory_outgoing(product_map, location, today)
            summary["transactions_out"] = n
        except Exception as e:
            summary["transactions_out_error"] = str(e)

    # Accounting (invoices, payments, expenses)
    if "accounting" in scenarios:
        try:
            n_inv, n_pay, n_exp = _seed_accounting(today)
            summary["invoices"]  = n_inv
            summary["payments"]  = n_pay
            summary["expenses"]  = n_exp
        except Exception as e:
            summary["accounting_error"] = str(e)

    # Shipping (carrier + shipments)
    if "shipping" in scenarios:
        try:
            n = _seed_shipping(today)
            summary["shipments"] = n
        except Exception as e:
            summary["shipping_error"] = str(e)

    # Tasks
    if "tasks" in scenarios:
        try:
            n = _seed_tasks(today)
            summary["tasks"] = n
        except Exception as e:
            summary["tasks_error"] = str(e)

    return summary
