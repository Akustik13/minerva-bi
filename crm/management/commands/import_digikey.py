
"""
DigiKey → Django імпортер
=========================
Використовує ГОТОВИЙ парсер бота (02_run_bot_FINAL.py) але замість
запису в Excel записує напряму в базу даних Django.

ЗАПУСК:
    python manage.py import_digikey            # нові замовлення
    python manage.py import_digikey --headless # без відкриття браузера

ЗАЛЕЖНОСТІ:
    pip install playwright python-dateutil
    playwright install chromium

НАЛАШТУВАННЯ (.env):
    DIGIKEY_LOGIN=sergey@sevskiy.de
    DIGIKEY_PASSWORD=oaj(/&-%%olfjwe23958
"""
import os
import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

from dateutil import parser as dtparser
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction

from inventory.models import Product, ProductAlias, Location, InventoryTransaction
from sales.models import SalesOrder, SalesOrderLine


# ===== Константи =====
MARKETPLACE_URL = "https://supplier.digikey.com/marketplace/"
BASE_URL        = "https://supplier.digikey.com"


# ===== Helpers (взято з бота, без змін) =====

def safe_goto(page, url: str, timeout_ms: int = 180000):
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

def absolutize(href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    return BASE_URL + ("/" if not href.startswith("/") else "") + href.lstrip("/")

def parse_order_number(page) -> str:
    h1 = page.locator("h1", has_text=re.compile(r"Order\s*#", re.I)).first
    if h1.count() == 0:
        return ""
    txt = h1.inner_text() or ""
    m = re.search(r"\b(\d{6,})\b", txt)
    return m.group(1) if m else ""

def parse_order_date(page) -> date | None:
    el = page.locator(
        "xpath=//h1[contains(., 'Order') and contains(., '#')]"
        "/following::div[contains(@class,'flex')][1]"
    ).first
    if el.count() == 0:
        return None
    txt = re.sub(r"^\s*From\s*", "", (el.inner_text() or "").replace("\n", " ").strip(), flags=re.I)
    try:
        return dtparser.parse(txt).date()
    except Exception:
        return None

def parse_shipping_deadline(page) -> date | None:
    h4 = page.locator("h4", has_text=re.compile(r"Shipping Deadline", re.I)).first
    if h4.count() == 0:
        return None
    i_el = h4.locator("xpath=following::i[1]").first
    if i_el.count() == 0:
        return None
    try:
        return dtparser.parse((i_el.inner_text() or "").strip()).date()
    except Exception:
        return None

def parse_shipping_address(page) -> tuple[str, str, str]:
    container = page.locator(
        "xpath=//h4[contains(., 'Shipping Address')]/ancestor::div[1]"
    ).first
    if container.count() == 0:
        return "", "", ""
    ps = container.locator("p")
    raw = []
    for i in range(ps.count()):
        t = (ps.nth(i).inner_text() or "").strip()
        if t:
            raw.extend([x.strip().strip('"') for x in t.splitlines() if x.strip()])
    disclaimer_markers = [
        "digikey customer email address should only be used",
        "any interaction with the digikey customer",
        "marketplace messaging",
        "clear customs",
    ]
    lines = [x for x in raw if x and not any(m in x.lower() for m in disclaimer_markers)]
    email = next(
        (re.search(r"[\w\.-]+@[\w\.-]+\.\w+", ln).group(0)
         for ln in lines if re.search(r"[\w\.-]+@[\w\.-]+\.\w+", ln)),
        ""
    )
    non_email = [ln for ln in lines if "@" not in ln and not ln.lower().startswith("email:")]
    country = ""
    for ln in reversed(non_email):
        cand = ln.strip().upper()
        if re.fullmatch(r"[A-Z]{3}", cand):
            country = cand
            break
    if not country:
        for ln in reversed(non_email):
            up = ln.strip().upper()
            if "." in up:
                continue
            m = re.search(r"\b([A-Z]{3})\b\s*$", up)
            if m:
                country = m.group(1)
                break
    address = "\n".join(x.strip().strip('"').strip() for x in lines if x.strip().strip('"').strip())
    return address, email, country

def parse_line_items(page) -> list[dict]:
    cards = page.locator(
        "xpath=//div[contains(@class,'MuiCard-root')]"
        "[.//text()[contains(.,'Supplier Part Number')]]"
    )
    if cards.count() == 0:
        cards = page.locator(
            "xpath=//div[.//text()[contains(.,'Supplier Part Number')]]"
        )
    items = []
    for i in range(cards.count()):
        txt = (cards.nth(i).inner_text() or "").replace("\u00a0", " ").strip()
        spn = ""
        m = re.search(r"Supplier Part Number:\s*([A-Z0-9\-\._/]+)", txt, flags=re.I)
        if m:
            spn = m.group(1).strip()
        else:
            m2 = re.search(r"Supplier Part Number\s+([A-Z0-9\-\._/]+)", txt, flags=re.I)
            if m2:
                spn = m2.group(1).strip()
        qty = None
        m = re.search(r"\bQty:\s*([0-9]+)\b", txt, flags=re.I)
        if m:
            try:
                qty = int(m.group(1))
            except Exception:
                pass
        unit_price = ""
        m = re.search(r"Unit product price:\s*([$€£]?\s*[0-9]+(?:\.[0-9]+)?)", txt, flags=re.I)
        if m:
            unit_price = m.group(1).replace(" ", "").strip()
        total_price = ""
        m = re.search(r"Total product price:\s*([$€£]?\s*[0-9]+(?:\.[0-9]+)?)", txt, flags=re.I)
        if m:
            total_price = m.group(1).replace(" ", "").strip()
        if spn:
            items.append({
                "supplier_part_number": spn,
                "qty": qty,
                "unit_price": unit_price,
                "total_price": total_price,
            })
    return items

def auto_login(page, login: str, password: str) -> bool:
    safe_goto(page, MARKETPLACE_URL)
    if "supplier.digikey.com/marketplace" in page.url:
        return True
    try:
        page.wait_for_selector("#username", timeout=30000)
        page.fill("#username", login)
        page.fill("#password", password)
        page.click("button:has-text('Anmelden'), button[type='submit']", timeout=15000)
        page.wait_for_url("**/marketplace/**", timeout=180000)
        return True
    except Exception:
        return False

def open_need_shipping(page) -> int:
    page.wait_for_timeout(1200)
    card = page.locator(
        "xpath=//h6[contains(., 'Need Shipping Information')]/ancestor::div[1]"
    ).first
    card.wait_for(timeout=30000)
    num = card.locator("h4[class*='DashboardOrderDetailsStatusCard_Number']").first
    num.wait_for(timeout=15000)
    txt = (num.inner_text() or "").strip()
    m = re.search(r"\d+", txt)
    cnt = int(m.group(0)) if m else 0
    if cnt > 0:
        try:
            num.click(timeout=5000)
        except Exception:
            card.click(timeout=5000)
        page.locator("tbody[data-testid='orders_manage_table_body_test_id']").wait_for(timeout=60000)
    return cnt

def get_orders_from_table(page) -> list[dict]:
    tbody = page.locator("tbody[data-testid='orders_manage_table_body_test_id']").first
    rows = tbody.locator("tr")
    orders = []
    for i in range(rows.count()):
        row = rows.nth(i)
        a = row.locator("th[scope='row'] a").first
        if a.count() == 0:
            continue
        order_id = (a.inner_text() or "").strip()
        href = a.get_attribute("href") or ""
        if re.fullmatch(r"\d+", order_id):
            orders.append({"order_id": order_id, "href": href})
    return orders


# ===== Django запис =====

def _parse_price(raw: str) -> Decimal:
    """'$12.50' → Decimal('12.50')"""
    if not raw:
        return Decimal("0")
    cleaned = re.sub(r"[^\d.]", "", str(raw))
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return Decimal("0")

def _find_product_by_sku(sku: str) -> Product | None:
    """Шукає товар по SKU або alias."""
    if not sku:
        return None
    product = Product.objects.filter(sku__iexact=sku).first()
    if product:
        return product
    alias = ProductAlias.objects.filter(alias__iexact=sku).select_related("product").first()
    return alias.product if alias else None

@transaction.atomic
def save_order_to_db(order_no: str, order_date, deadline,
                     address: str, email: str, country: str,
                     items: list[dict]) -> tuple[bool, int]:
    """
    Зберігає замовлення DigiKey в базу даних Django.
    Повертає (is_new, items_added).
    """
    # Створити або знайти SalesOrder
    order, created = SalesOrder.objects.get_or_create(
        source="digikey",
        order_number=order_no,
        defaults={
            "order_date": timezone.make_aware(
                timezone.datetime.combine(order_date, timezone.datetime.min.time())
            ) if order_date else None,
            "shipping_deadline": deadline,
            "shipping_address": address,
            "email": email,
            "shipping_region": country,
            "document_type": "SALE",
            "affects_stock": True,
        },
    )

    if not created:
        # Оновити deadline якщо ще не заповнений
        if deadline and not order.shipping_deadline:
            order.shipping_deadline = deadline
            order.save(update_fields=["shipping_deadline"])

    location, _ = Location.objects.get_or_create(
        code="MAIN", defaults={"name": "Основний склад"}
    )

    added = 0
    for item in items:
        sku = item["supplier_part_number"]
        qty = item.get("qty") or 0

        # Перевірка дубліката
        if SalesOrderLine.objects.filter(order=order, sku_raw=sku).exists():
            continue

        product = _find_product_by_sku(sku)

        # Створити рядок замовлення
        line = SalesOrderLine.objects.create(
            order=order,
            product=product,              # може бути None якщо SKU не знайдено
            sku_raw=sku,
            qty=Decimal(str(qty)),
            unit_price=_parse_price(item.get("unit_price", "")),
            total_price=_parse_price(item.get("total_price", "")),
        )

        # Списати зі складу якщо товар знайдений
        if product and qty and order.affects_stock:
            # Сигнал вже підключений - але для нових рядків після збереження
            # замовлення (created=False) треба вручну
            if not created:
                InventoryTransaction.objects.create(
                    external_key=f"digikey:{order_no}:{sku}:{uuid.uuid4()}",
                    tx_type=InventoryTransaction.TxType.OUTGOING,
                    qty=-abs(Decimal(str(qty))),
                    product=product,
                    location=location,
                    ref_doc=f"DigiKey-{order_no}",
                    tx_date=order.order_date or timezone.now(),
                )

        added += 1

    return created, added


# ===== Django Management Command =====

class Command(BaseCommand):
    help = "Імпортувати нові замовлення DigiKey напряму в базу даних"

    def add_arguments(self, parser):
        parser.add_argument(
            "--headless",
            action="store_true",
            help="Запустити браузер у фоновому режимі (без вікна)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Тільки показати замовлення без збереження в БД",
        )

    def handle(self, *args, **options):
        # Завантаження dotenv
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

        # Імпорт playwright
        from playwright.sync_api import sync_playwright

        login    = (os.getenv("DIGIKEY_LOGIN") or "").strip()
        password = (os.getenv("DIGIKEY_PASSWORD") or "").strip()

        if not login or not password:
            self.stderr.write(
                self.style.ERROR("❌ Немає DIGIKEY_LOGIN / DIGIKEY_PASSWORD в .env")
            )
            return

        headless = options["headless"]
        dry_run  = options["dry_run"]

        self.stdout.write("🌐 Запуск браузера...")

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.stderr.write(self.style.ERROR(
                "❌ playwright не встановлений.\n"
                "   Виконайте:\n"
                "   pip install playwright\n"
                "   playwright install chromium"
            ))
            return

        total_added = 0
        total_orders = 0

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context()
            page    = context.new_page()

            if not auto_login(page, login, password):
                self.stdout.write(self.style.WARNING(
                    "⚠️ Автологін не вдався. Увійдіть вручну та натисніть Enter."
                ))
                input()

            cnt = open_need_shipping(page)
            self.stdout.write(f"📦 Need Shipping Information: {cnt}")

            if cnt == 0:
                self.stdout.write(self.style.SUCCESS("✅ Нових замовлень немає."))
                browser.close()
                return

            orders = get_orders_from_table(page)
            self.stdout.write(f"📋 Замовлень в списку: {len(orders)}")

            for it in orders:
                order_id = it["order_id"]
                href     = it["href"]
                self.stdout.write(f"\n→ Обробляю Order {order_id}...")

                try:
                    page.locator(
                        "tbody[data-testid='orders_manage_table_body_test_id'] a",
                        has_text=order_id
                    ).first.click(timeout=10000)
                except Exception:
                    safe_goto(page, absolutize(href))

                page.wait_for_timeout(1200)

                order_no = parse_order_number(page) or order_id
                order_date = parse_order_date(page)
                deadline   = parse_shipping_deadline(page)
                address, email, country = parse_shipping_address(page)
                items = parse_line_items(page)

                if not items:
                    self.stdout.write(self.style.WARNING(f"  ⚠️ Позицій не знайдено в {order_no}"))
                else:
                    self.stdout.write(f"  Позицій: {len(items)}")
                    for item in items:
                        self.stdout.write(
                            f"    • {item['supplier_part_number']} "
                            f"× {item['qty']} @ {item['unit_price']}"
                        )

                if not dry_run and items:
                    is_new, added = save_order_to_db(
                        order_no, order_date, deadline,
                        address, email, country, items
                    )
                    total_added += added
                    total_orders += 1
                    status = "🆕 Нове" if is_new else "🔄 Оновлено"
                    self.stdout.write(
                        self.style.SUCCESS(f"  {status}: {order_no} (+{added} позицій)")
                    )
                elif dry_run:
                    self.stdout.write(self.style.WARNING(f"  [DRY RUN] Не збережено"))

                # Повернутись до списку
                page.go_back(wait_until="domcontentloaded", timeout=180000)
                page.locator(
                    "tbody[data-testid='orders_manage_table_body_test_id']"
                ).wait_for(timeout=60000)
                page.wait_for_timeout(500)

            browser.close()

        if not dry_run:
            self.stdout.write(self.style.SUCCESS(
                f"\n✅ Готово! Оброблено замовлень: {total_orders}, "
                f"додано позицій: {total_added}"
            ))
        else:
            self.stdout.write(self.style.WARNING("\n[DRY RUN] Нічого не збережено в БД"))
