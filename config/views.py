from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_http_methods
from django.contrib import messages

from config.models import SystemSettings
from config.demo_data import (
    DEMO_CUSTOMER_SOURCE, DEMO_MARKER, DEMO_BULK_SKU_PREFIX, _demo_orders_q,
)

CURRENCIES = ["EUR", "USD", "UAH", "GBP", "PLN", "CZK", "CHF"]
TIMEZONES = [
    "Europe/Kyiv", "Europe/Berlin", "Europe/Warsaw", "Europe/Prague",
    "Europe/London", "Europe/Paris", "UTC",
]
MODULES = [
    ("crm",         "👥 CRM",              "Клієнти, RFM-аналіз"),
    ("accounting",  "💰 Бухгалтерія",      "Рахунки, платежі, витрати"),
    ("sales",       "🛒 Продажі",          "Замовлення, джерела"),
    ("shipping",    "🚚 Доставка",         "Перевізники, відправки"),
    ("inventory",   "📦 Склад",            "Товари, залишки, постачальники"),
    ("bots",        "🤖 Боти та AI",       "Автоматизація, API"),
]
TOTAL_STEPS = 4


@login_required
def onboarding(request):
    """Єдиний view для 4-кrokового wizard (крок передається через GET/POST параметр step)."""
    cfg = SystemSettings.get()

    # Якщо вже завершено — редірект
    if cfg.is_onboarding_complete and request.method == "GET" and request.GET.get("step") != "reset":
        return redirect("/admin/")

    session_key = "onboarding_data"
    data = request.session.get(session_key, {})

    step = int(request.GET.get("step", request.POST.get("step", 1)))

    if request.method == "POST":
        action = request.POST.get("action", "next")

        if step == 1:
            data["company_name"] = request.POST.get("company_name", "").strip() or "Моя компанія"
            data["default_currency"] = request.POST.get("default_currency", "EUR")
            data["timezone"] = request.POST.get("timezone", "Europe/Kyiv")
            # Logo upload handled separately
            if "logo" in request.FILES:
                logo_file = request.FILES["logo"]
                cfg.logo.save(logo_file.name, logo_file, save=False)
                data["logo_name"] = logo_file.name

        elif step == 2:
            data["enabled_modules"] = request.POST.getlist("modules") or [m[0] for m in MODULES]
            data["accounting_level"] = int(request.POST.get("accounting_level", 2))

        elif step == 3:
            # Перше джерело продажів (опційно)
            source_name = request.POST.get("source_name", "").strip()
            data["source_name"] = source_name

        elif step == 4:
            # Перший товар (опційно)
            data["product_sku"] = request.POST.get("product_sku", "").strip()
            data["product_name"] = request.POST.get("product_name", "").strip()
            data["product_unit"] = request.POST.get("product_unit", "шт").strip()

        request.session[session_key] = data
        request.session.modified = True

        if action == "finish" or step == TOTAL_STEPS:
            return _finish_onboarding(request, data, cfg)

        if action == "skip":
            next_step = step + 1
        else:
            next_step = step + 1

        return redirect(f"/onboarding/?step={next_step}")

    # GET — показати крок
    context = {
        "step": step,
        "total_steps": TOTAL_STEPS,
        "data": data,
        "currencies": CURRENCIES,
        "timezones": TIMEZONES,
        "modules": MODULES,
        "level_choices": [(1, "Базовий"), (2, "Стандарт"), (3, "Розширений")],
        "has_sales": "sales" in data.get("enabled_modules", [m[0] for m in MODULES]),
        "has_inventory": "inventory" in data.get("enabled_modules", [m[0] for m in MODULES]),
    }
    return render(request, "config/onboarding.html", context)


def _finish_onboarding(request, data, cfg):
    """Зберегти SystemSettings і опційні об'єкти, завершити wizard."""
    cfg.company_name = data.get("company_name", "Моя компанія")
    cfg.default_currency = data.get("default_currency", "EUR")
    cfg.timezone = data.get("timezone", "Europe/Kyiv")
    cfg.enabled_modules = data.get("enabled_modules", ["crm", "accounting", "sales", "shipping", "inventory", "bots"])
    cfg.accounting_level = data.get("accounting_level", 2)
    cfg.is_onboarding_complete = True
    cfg.save()

    # Перше джерело продажів
    source_name = data.get("source_name", "").strip()
    if source_name and "sales" in cfg.enabled_modules:
        try:
            from sales.models import SalesSource
            SalesSource.objects.get_or_create(name=source_name)
        except Exception:
            pass

    # Перший товар
    sku = data.get("product_sku", "").strip()
    name = data.get("product_name", "").strip()
    if sku and name and "inventory" in cfg.enabled_modules:
        try:
            from inventory.models import Product
            Product.objects.get_or_create(sku=sku, defaults={
                "name": name,
                "unit": data.get("product_unit", "шт"),
            })
        except Exception:
            pass

    # Очистити session
    request.session.pop("onboarding_data", None)

    return redirect("/admin/")


@login_required
def demo(request):
    """Confirmation page + trigger for demo data seeding."""
    if request.method == "POST":
        from config.demo_data import seed_demo_data

        # Collect selected scenarios
        all_scenarios = {"products", "customers", "inventory", "orders",
                         "accounting", "shipping", "tasks"}
        selected = set(request.POST.getlist("scenarios")) & all_scenarios
        if not selected:
            selected = all_scenarios  # fallback: all

        try:
            bulk_products = max(0, min(int(request.POST.get("bulk_products", 0) or 0), 10000))
            bulk_orders   = max(0, min(int(request.POST.get("bulk_orders",   0) or 0), 10000))
        except (ValueError, TypeError):
            bulk_products = bulk_orders = 0

        result = seed_demo_data(scenarios=selected,
                                bulk_products=bulk_products,
                                bulk_orders=bulk_orders)

        errors = {k: v for k, v in result.items() if k.endswith("_error")}
        _LABELS = {
            "products": "товарів", "bulk_products": "bulk-товарів",
            "customers": "клієнтів", "orders": "замовлень", "order_lines": "рядків",
            "bulk_orders": "bulk-замовлень", "bulk_order_lines": "bulk-рядків",
            "transactions_in": "прихідних транз.", "transactions_out": "видаткових транз.",
            "invoices": "рахунків", "payments": "платежів", "expenses": "витрат",
            "shipments": "відправлень", "tasks": "задач",
        }
        parts = [f"{v} {_LABELS.get(k, k)}" for k, v in result.items()
                 if not k.endswith("_error") and v]
        if errors:
            messages.warning(request, f"Частина даних з помилками: {errors}")
        if parts:
            messages.success(request, "Демо-дані завантажено: " + ", ".join(parts) + ".")
        else:
            messages.info(request, "Дані вже існують — нічого нового не створено.")
        return redirect("/dashboard/system/")

    return render(request, "config/demo_confirm.html")


# ─────────────────────────────────────────────────────────────────────────────
# DELETE DEMO DATA
# ─────────────────────────────────────────────────────────────────────────────


def _demo_counts():
    """Повертає dict з кількістю demo-записів для відображення."""
    from sales.models import SalesOrder
    from crm.models import Customer
    from inventory.models import InventoryTransaction, Product
    counts = {
        "orders":        SalesOrder.objects.filter(_demo_orders_q()).count(),
        "customers":     Customer.objects.filter(source=DEMO_CUSTOMER_SOURCE).count(),
        "transactions":  InventoryTransaction.objects.filter(
            ref_doc__startswith="DEMO").count(),
        "bulk_products": Product.objects.filter(
            sku__startswith=DEMO_BULK_SKU_PREFIX).count(),
    }
    try:
        from accounting.models import Invoice, Expense
        counts["invoices"] = Invoice.objects.filter(
            order__in=SalesOrder.objects.filter(_demo_orders_q())
        ).count()
        counts["expenses"] = Expense.objects.filter(
            description__contains=DEMO_MARKER).count()
    except Exception:
        counts["invoices"] = counts["expenses"] = 0
    try:
        from shipping.models import Shipment
        counts["shipments"] = Shipment.objects.filter(
            order__in=SalesOrder.objects.filter(_demo_orders_q())
        ).count()
    except Exception:
        counts["shipments"] = 0
    try:
        from tasks.models import Task
        counts["tasks"] = Task.objects.filter(
            title__contains=DEMO_MARKER).count()
    except Exception:
        counts["tasks"] = 0
    return counts


@login_required
def delete_demo(request):
    """Видалення тестових (demo) даних."""
    if request.method == "POST":
        from django.db import transaction as db_transaction
        from sales.models import SalesOrder
        from crm.models import Customer
        from inventory.models import InventoryTransaction, Product

        with db_transaction.atomic():
            # 1. Invoices (SET_NULL on order — delete before orders)
            n_invoices = 0
            try:
                from accounting.models import Invoice
                inv_qs = Invoice.objects.filter(
                    order__in=SalesOrder.objects.filter(_demo_orders_q())
                )
                n_invoices = inv_qs.count()
                inv_qs.delete()
            except Exception:
                pass

            # 2. Shipments (PROTECT on order — delete before orders)
            n_shipments = 0
            try:
                from shipping.models import Shipment
                sh_qs = Shipment.objects.filter(
                    order__in=SalesOrder.objects.filter(_demo_orders_q())
                )
                n_shipments = sh_qs.count()
                sh_qs.delete()
            except Exception:
                pass

            # 3. Tasks
            n_tasks = 0
            try:
                from tasks.models import Task
                n_tasks = Task.objects.filter(
                    title__contains=DEMO_MARKER).delete()[0]
            except Exception:
                pass

            # 4. Orders (cascade → SalesOrderLine, OrderPackaging)
            orders_qs = SalesOrder.objects.filter(_demo_orders_q())
            n_orders = orders_qs.count()
            orders_qs.delete()

            # 5. Customers
            n_customers = Customer.objects.filter(
                source=DEMO_CUSTOMER_SOURCE).delete()[0]

            # 6. Inventory transactions
            n_trans = InventoryTransaction.objects.filter(
                ref_doc__startswith="DEMO").delete()[0]

            # 7. Bulk products (DEMO-P-xxxxx)
            n_bulk = Product.objects.filter(
                sku__startswith=DEMO_BULK_SKU_PREFIX).delete()[0]

            # 8. Demo expenses + expense categories
            n_expenses = 0
            try:
                from accounting.models import Expense, ExpenseCategory
                n_expenses = Expense.objects.filter(
                    description__contains=DEMO_MARKER).delete()[0]
                ExpenseCategory.objects.filter(
                    name__contains=DEMO_MARKER).delete()
            except Exception:
                pass

            # 9. Demo carrier
            try:
                from shipping.models import Carrier
                Carrier.objects.filter(name__contains=DEMO_MARKER).delete()
            except Exception:
                pass

        parts = []
        if n_orders:    parts.append(f"{n_orders} замовлень")
        if n_customers: parts.append(f"{n_customers} клієнтів")
        if n_trans:     parts.append(f"{n_trans} транзакцій")
        if n_invoices:  parts.append(f"{n_invoices} рахунків")
        if n_shipments: parts.append(f"{n_shipments} відправлень")
        if n_tasks:     parts.append(f"{n_tasks} задач")
        if n_bulk:      parts.append(f"{n_bulk} bulk-товарів")
        if n_expenses:  parts.append(f"{n_expenses} витрат")
        msg = "Демо-дані видалено: " + (", ".join(parts) or "нічого не знайдено") + "."
        messages.success(request, msg)
        return redirect("/dashboard/system/")

    counts = _demo_counts()
    return render(request, "config/delete_demo_confirm.html", {"counts": counts})


# ─────────────────────────────────────────────────────────────────────────────
# CLEAR SYSTEM
# ─────────────────────────────────────────────────────────────────────────────

def _system_counts(full=False):
    """Кількість записів що будуть видалені."""
    from sales.models import SalesOrder, SalesOrderLine
    from crm.models import Customer
    from inventory.models import InventoryTransaction
    counts = {
        "orders":       SalesOrder.objects.count(),
        "order_lines":  SalesOrderLine.objects.count(),
        "customers":    Customer.objects.count(),
        "transactions": InventoryTransaction.objects.count(),
    }
    try:
        from accounting.models import Invoice, Payment, Expense
        counts["invoices"] = Invoice.objects.count()
        counts["payments"] = Payment.objects.count()
        counts["expenses"] = Expense.objects.count()
    except Exception:
        pass
    try:
        from shipping.models import Shipment
        counts["shipments"] = Shipment.objects.count()
    except Exception:
        pass
    try:
        from tasks.models import Task
        counts["tasks"] = Task.objects.count()
    except Exception:
        pass
    if full:
        from inventory.models import Product, ProductCategory, Location
        from sales.models import SalesSource
        counts["products"]   = Product.objects.count()
        counts["categories"] = ProductCategory.objects.count()
        counts["locations"]  = Location.objects.count()
        counts["sources"]    = SalesSource.objects.count()
    return counts


@login_required
def clear_system(request):
    """Очищення системи: транзакційні дані або повний factory reset."""
    mode = request.POST.get("mode") or request.GET.get("mode", "partial")
    full = (mode == "full")

    CONFIRM_PARTIAL = "ОЧИСТИТИ"
    CONFIRM_FULL    = "ПОВНЕ ОЧИЩЕННЯ"

    if request.method == "POST":
        confirm = request.POST.get("confirm", "").strip()
        expected = CONFIRM_FULL if full else CONFIRM_PARTIAL

        if confirm != expected:
            messages.error(request, f'Введіть рядок підтвердження: «{expected}»')
            counts = _system_counts(full)
            return render(request, "config/clear_system_confirm.html", {
                "counts": counts, "mode": mode, "full": full,
                "confirm_word": expected,
            })

        from django.db import transaction as db_transaction

        with db_transaction.atomic():
            # --- Завжди видаляємо транзакційні дані ---
            try:
                from tasks.models import Task
                Task.objects.all().delete()
            except Exception:
                pass
            try:
                from shipping.models import Shipment
                Shipment.objects.all().delete()
            except Exception:
                pass
            try:
                from accounting.models import Invoice, Payment, Expense
                Payment.objects.all().delete()
                Invoice.objects.all().delete()
                Expense.objects.all().delete()
            except Exception:
                pass
            from sales.models import SalesOrderLine, SalesOrder
            SalesOrderLine.objects.all().delete()
            SalesOrder.objects.all().delete()
            from crm.models import Customer
            Customer.objects.all().delete()
            from inventory.models import InventoryTransaction
            InventoryTransaction.objects.all().delete()

            if full:
                # --- Factory reset: також каталог і налаштування ---
                try:
                    from inventory.models import PurchaseOrderLine, PurchaseOrder
                    PurchaseOrderLine.objects.all().delete()
                    PurchaseOrder.objects.all().delete()
                except Exception:
                    pass
                from inventory.models import Product, ProductCategory, Location, ProductAlias
                ProductAlias.objects.all().delete()
                Product.objects.all().delete()
                ProductCategory.objects.all().delete()
                Location.objects.all().delete()
                try:
                    from inventory.models import Supplier
                    Supplier.objects.all().delete()
                except Exception:
                    pass
                from sales.models import SalesSource
                SalesSource.objects.all().delete()
                try:
                    from shipping.models import Carrier
                    Carrier.objects.all().delete()
                except Exception:
                    pass
                try:
                    from accounting.models import CompanySettings, ExpenseCategory
                    ExpenseCategory.objects.all().delete()
                    CompanySettings.objects.filter(pk=1).update(
                        name="Моя компанія", legal_name="", iban="", vat_id="",
                        addr_street="", addr_city="", addr_zip="", addr_country="",
                    )
                except Exception:
                    pass
                # Онбординг знову
                cfg = SystemSettings.get()
                cfg.is_onboarding_complete = False
                cfg.company_name = "Моя компанія"
                cfg.save()

        label = "Повне очищення" if full else "Транзакційні дані"
        messages.success(request, f"✅ {label} — система очищена.")
        return redirect("/dashboard/system/")

    counts = _system_counts(full)
    confirm_word = CONFIRM_FULL if full else CONFIRM_PARTIAL
    return render(request, "config/clear_system_confirm.html", {
        "counts": counts, "mode": mode, "full": full,
        "confirm_word": confirm_word,
    })
