from django.contrib import admin
from crm.utils import sync_customer_from_order
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.db.models import Sum
from .models import SalesOrder, SalesOrderLine
from .forms import SalesImportForm
import openpyxl
from decimal import Decimal
import re
from datetime import date


class SalesOrderLineInline(admin.TabularInline):
    model = SalesOrderLine
    extra = 0
    readonly_fields = ("stock_status",)
    fields = ("product", "sku_raw", "qty", "unit_price", "currency", "total_price", "stock_status")
    
    # Темний фон для inline
    class Media:
        css = {
            'all': ('admin/css/dark-inline.css',)
        }

    def stock_status(self, obj):
        if not obj.product:
            if obj.sku_raw:
                return format_html('<span style="color:#ff9800">⚠️ {} не в базі</span>', obj.sku_raw)
            return "—"
        from inventory.models import InventoryTransaction
        result = InventoryTransaction.objects.filter(product=obj.product).aggregate(total=Sum('qty'))
        stock = float(result['total'] or 0)
        needed = float(obj.qty or 0)
        if stock <= 0:
            return format_html('<span style="background:#f44336;color:#fff;padding:2px 8px;border-radius:8px;font-size:11px">🚫 Немає (0)</span>')
        elif stock < needed:
            return format_html('<span style="background:#ff9800;color:#fff;padding:2px 8px;border-radius:8px;font-size:11px">⚠️ {} / {}</span>', int(stock), int(needed))
        return format_html('<span style="background:#4caf50;color:#fff;padding:2px 8px;border-radius:8px;font-size:11px">✅ {} шт.</span>', int(stock))
    stock_status.short_description = "На складі"


class UnshippedFilter(admin.SimpleListFilter):
    title = "Відправлено"
    parameter_name = "shipped"
    def lookups(self, request, model_admin):
        return [("no", "⏳ Не відправлено"), ("yes", "✅ Відправлено")]
    def queryset(self, request, queryset):
        if self.value() == "no": return queryset.filter(shipped_at__isnull=True)
        if self.value() == "yes": return queryset.filter(shipped_at__isnull=False)
        return queryset


def export_sales_excel(modeladmin, request, queryset):
    try:
        from exports import export_sales
        return export_sales(queryset)
    except Exception as e:
        from django.contrib import messages
        messages.error(request, f"Помилка: {e}")
export_sales_excel.short_description = "📥 Експортувати в Excel"


@admin.register(SalesOrder)
class SalesOrderAdmin(admin.ModelAdmin):
    # ── Прибрано: phone, tracking_number, customer_link (без лінку) ───────────
    list_display = (
        "order_number", "source_badge", "status_badge", "order_date_fmt", 'deadline_display',
        "customer_link_display", "shipping_region",
        "shipped_badge",
        "items_count", "order_total",
        "stock_warning",
        "label_buttons_list",
        # "customer_link",  # disabled - no FK
    )
    search_fields = ("order_number", "tracking_number", "client", "email", "phone")
    list_filter   = ("source", "status", "shipping_region", UnshippedFilter)
    date_hierarchy = "order_date"
    actions        = [export_sales_excel]
    preserve_filters = True
    inlines        = [SalesOrderLineInline]
    readonly_fields = ("stock_summary", "label_buttons_detail", "label_upload_widget")

    fieldsets = (
        ("📦 Замовлення", {
            "fields": ("source", "status", "document_type", "affects_stock",
                       "order_number", "order_date")
        }),
        ("👤 Клієнт", {
            "fields": ("client", "email", "phone")
        }),
        ("🚚 Доставка", {
            "fields": (
                "shipping_region", "shipping_address", "shipping_deadline",
                "shipped_at", "shipping_courier", "tracking_number", "lieferschein_nr",
                ("shipping_cost", "shipping_currency")  # Вартість доставки + валюта
            )
        }),
        ("📦 Залишки на складі", {"fields": ("stock_summary",)}),
        ("🏷️ Етикетки", {
            "fields": ("label_buttons_detail", "label_upload_widget"),
            "description": "Натисніть кнопку — файл завантажиться і відкриється в DYMO Label Software",
        }),
    )

    # ── Колонки ────────────────────────────────────────────────────────────────


    def deadline_display(self, obj):
        """Дедлайн відправки з підсвіткою та днями залишилось"""
        if not obj.shipping_deadline:
            return format_html('<span style="color:#7d8590">—</span>')
        
        # Якщо вже відправлено - показати тільки дату
        if obj.shipped_at:
            return format_html(
                '<div style="font-size:11px;color:#7d8590">{}</div>',
                obj.shipping_deadline.strftime('%d.%m.%Y')
            )
        
        # Розрахувати дні до дедлайну
        today = date.today()
        delta = (obj.shipping_deadline - today).days
        
        # Кольори залежно від термінів
        if delta < 0:
            # Прострочено
            color = '#f85149'
            bg = 'rgba(248,81,73,0.1)'
            icon = '🔴'
            text = f'Прострочено {abs(delta)}д'
        elif delta == 0:
            # Сьогодні
            color = '#e3b341'
            bg = 'rgba(227,179,65,0.15)'
            icon = '⚠️'
            text = 'Сьогодні!'
        elif delta <= 2:
            # Критично (1-2 дні)
            color = '#e3b341'
            bg = 'rgba(227,179,65,0.1)'
            icon = '⏰'
            text = f'Залишилось {delta}д'
        elif delta <= 5:
            # Увага (3-5 днів)
            color = '#58a6ff'
            bg = 'rgba(88,166,255,0.08)'
            icon = '📅'
            text = f'Залишилось {delta}д'
        else:
            # Нормально (>5 днів)
            color = '#3fb950'
            bg = 'rgba(63,185,80,0.08)'
            icon = '✅'
            text = f'Залишилось {delta}д'
        
        return format_html(
            '''<div style="font-size:11px;line-height:1.4">
                <div style="color:#e6edf3;font-weight:600;font-family:monospace">
                    {}
                </div>
                <div style="
                    margin-top:3px;
                    padding:2px 6px;
                    border-radius:4px;
                    background:{};
                    color:{};
                    font-size:10px;
                    font-weight:700;
                    display:inline-block;
                ">
                    {} {}
                </div>
            </div>''',
            obj.shipping_deadline.strftime('%d.%m.%Y'),
            bg,
            color,
            icon,
            text
        )

    deadline_display.short_description = '📦 Дедлайн'
    deadline_display.admin_order_field = 'shipping_deadline'


    def customer_link_display(self, obj):
        """Посилання на клієнта в CRM з RFM статусом."""
        from django.utils.html import format_html
        from django.urls import reverse
        
        if not obj.email and not obj.client:
            return "—"
        
        from crm.models import Customer
        from django.db.models import Q
        
        # Шукаємо клієнта
        customer = None
        if obj.customer_key:
            customer = Customer.objects.filter(external_key=obj.customer_key).first()
        if not customer and obj.email:
            customer = Customer.objects.filter(email=obj.email).first()
        if not customer and obj.client:
            customer = Customer.objects.filter(name__iexact=obj.client).first()
        
        if not customer:
            return format_html(
                '<div style="opacity:0.6">{}<br><small>⚠️ Не в CRM</small></div>',
                obj.client or obj.email
            )
        
        # Отримуємо RFM з обробкою помилок
        try:
            rfm = customer.rfm_score()
            segment = rfm.get('segment', '👤 Customer')
        except Exception as e:
            # Якщо rfm_score() падає - показуємо просто ім'я
            return format_html(
                '<a href="{}" style="text-decoration:none;color:#64b5f6">'
                '<div style="font-weight:600">{}</div>'
                '<div style="font-size:10px;color:#999">⚠️ RFM error</div></a>',
                reverse('admin:crm_customer_change', args=[customer.pk]),
                customer.name
            )
        
        icons = {
            '🏆 Champions': '🏆', '💎 Loyal': '💎', '⭐ Potential': '⭐',
            '🔄 Regular': '🔄', '😴 At Risk': '😴', '💤 Hibernating': '💤', '🆕 New': '🆕',
        }
        colors = {
            '🏆 Champions': '#4caf50', '💎 Loyal': '#2196f3', '⭐ Potential': '#ff9800',
            '🔄 Regular': '#9c27b0', '😴 At Risk': '#f44336', '💤 Hibernating': '#757575', '🆕 New': '#00bcd4',
        }
        
        url = reverse('admin:crm_customer_change', args=[customer.pk])
        
        return format_html(
            '<a href="{}" style="text-decoration:none;color:#64b5f6">'
            '<div style="font-weight:600">{}</div>'
            '<div style="font-size:10px;color:{}">{}</div></a>',
            url, customer.name, colors.get(segment, '#616161'), segment
        )
    customer_link_display.short_description = "Клієнт"

    def order_date_fmt(self, obj):
        if not obj.order_date:
            return format_html('<span style="color:#999">—</span>')
        return format_html('<span style="white-space:nowrap">{}</span>',
                           obj.order_date.strftime("%d.%m.%Y"))
    order_date_fmt.short_description = "Дата"
    order_date_fmt.admin_order_field = "order_date"

    def source_badge(self, obj):
        colors = {"digikey": "#e91e63", "nova_post": "#ff9800", "other": "#607d8b", "webshop": "#9c27b0"}
        color = colors.get(obj.source, "#607d8b")
        label = obj.get_source_display() if hasattr(obj, "get_source_display") else obj.source
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;border-radius:10px;'
            'font-size:11px;white-space:nowrap">{}</span>', color, label)
    source_badge.short_description = "Джерело"

    def status_badge(self, obj):
        colors = {"received": "#2196f3", "processing": "#ff9800",
                  "shipped": "#4caf50", "cancelled": "#f44336"}
        color = colors.get(obj.status, "#757575")
        return format_html(
            '<span style="background:{};color:#fff;padding:3px 10px;border-radius:12px;'
            'font-size:11px;font-weight:bold;white-space:nowrap">{}</span>',
            color, obj.get_status_display())
    status_badge.short_description = "Статус"
    status_badge.admin_order_field = "status"

    def items_count(self, obj):
        return obj.lines.count()
    items_count.short_description = "Поз."

    def order_total(self, obj):
        """Показує суму з валютою."""
        try:
            total = obj.lines.aggregate(t=Sum("total_price"))["t"]
        except Exception:
            total = None
        
        if not total:
            return "—"
        
        # Визначаємо символ валюти
        currency_symbols = {
            'USD': '$',
            'EUR': '€',
            'GBP': '£',
        }
        symbol = currency_symbols.get(obj.currency, obj.currency or '€')
        
        return format_html('<b>{}{}</b>', symbol, f"{float(total):.2f}")
    order_total.short_description = "Сума"

    def shipped_badge(self, obj):
        if obj.shipped_at:
            return format_html('<span style="color:#4caf50;white-space:nowrap">✅ {}</span>',
                               obj.shipped_at.strftime("%d.%m.%Y"))
        return format_html('<span style="color:#ff9800;font-weight:bold;white-space:nowrap">⏳ Очікує</span>')
    shipped_badge.short_description = "Відправлено"
    shipped_badge.admin_order_field = "shipped_at"

    def stock_warning(self, obj):
        if obj.shipped_at:
            return format_html('<span style="color:#999">—</span>')
        from inventory.models import InventoryTransaction
        problems, oks = [], 0
        for line in obj.lines.filter(product__isnull=False):
            result = InventoryTransaction.objects.filter(
                product=line.product).aggregate(total=Sum('qty'))
            stock = float(result['total'] or 0)
            needed = float(line.qty or 0)
            if stock <= 0:
                problems.append(f"🚫 {line.product.sku}: 0")
            elif stock < needed:
                problems.append(f"⚠️ {line.product.sku}: {int(stock)}/{int(needed)}")
            else:
                oks += 1
        if problems:
            tip = " | ".join(problems)
            label = problems[0] if len(problems) == 1 else f"{len(problems)} проблем"
            return format_html(
                '<span style="color:#f44336;font-size:11px;font-weight:bold" title="{}">'
                '🚫 {}</span>', tip, label)
        if oks:
            return format_html('<span style="color:#4caf50;font-size:11px">✅ OK</span>')
        return format_html('<span style="color:#999;font-size:11px">—</span>')
    stock_warning.short_description = "Склад"

    def customer_link(self, obj):
        if not obj.customer:
            return "—"
        from django.urls import reverse
        url = reverse("admin:crm_customer_change", args=[obj.customer.pk])
        return format_html('<a href="{}">👤 {}</a>', url, obj.customer.name)
    customer_link.short_description = "Клієнт CRM"

    # ── Кнопки етикеток у списку ───────────────────────────────────────────────

    def label_buttons_list(self, obj):
        from pathlib import Path
        from django.conf import settings
        labels_dir = Path(getattr(settings, 'LABELS_DIR', Path(settings.BASE_DIR) / 'labels'))
        buttons = []
        for line in obj.lines.all():
            sku = line.product.sku if line.product else line.sku_raw
            qty = int(line.qty or 1)
            if not sku:
                continue
            found = False
            for f in labels_dir.glob('*.dymo'):
                if f.stem.upper() == sku.upper():
                    found = True
                    break
            if found:
                url = f"/labels/serve/{sku}/?qty={qty}"
                buttons.append(
                    f'<a href="{url}" '
                    f'style="display:inline-block;margin:2px;background:#1976d2;color:#fff;'
                    f'padding:3px 8px;border-radius:6px;font-size:11px;text-decoration:none;'
                    f'white-space:nowrap">🖨️ {sku}</a>')
            else:
                buttons.append(
                    f'<span style="display:inline-block;margin:2px;background:#bdbdbd;color:#fff;'
                    f'padding:3px 8px;border-radius:6px;font-size:11px;white-space:nowrap" '
                    f'title="Немає {sku}.dymo">❌ {sku}</span>')
        return mark_safe("".join(buttons)) if buttons else format_html('<span style="color:#999">—</span>')
    label_buttons_list.short_description = "🏷️ Етикетки"

    # ── Детальний блок етикеток ────────────────────────────────────────────────

    def label_buttons_detail(self, obj):
        from pathlib import Path
        from django.conf import settings
        labels_dir = Path(getattr(settings, 'LABELS_DIR', Path(settings.BASE_DIR) / 'labels'))
        rows = []
        for line in obj.lines.all():
            sku = line.product.sku if line.product else line.sku_raw
            name = line.product.name if line.product else (line.sku_raw or '—')
            qty = int(line.qty or 1)
            if not sku:
                continue
            label_path = None
            for f in labels_dir.glob('*.dymo'):
                if f.stem.upper() == sku.upper():
                    label_path = f
                    break
            if label_path:
                url = f"/labels/serve/{sku}/?qty={qty}"
                btn = (f'<a href="{url}" target="_blank" '
                       f'style="background:#1976d2;color:#fff;padding:8px 16px;'
                       f'border-radius:8px;text-decoration:none;font-weight:bold">'
                       f'🖨️ Друкувати ({qty} шт.)</a>')
                st = f'<span style="color:#4caf50;margin-left:10px">✅ {label_path.name}</span>'
            else:
                btn = '<span style="color:rgba(150,150,150,0.9);font-style:italic">Файл не завантажено</span>'
                st = (f'<span style="color:#f44336;margin-left:10px">'
                      f'❌ {sku}.dymo — завантажте нижче</span>')
            rows.append(
                f'<tr style="border-bottom:1px solid rgba(128,128,128,0.15)">'
                f'<td style="padding:10px;font-weight:bold">{sku}</td>'
                f'<td style="padding:10px;opacity:0.85">{name}</td>'
                f'<td style="padding:10px;text-align:center;font-weight:bold">{qty}</td>'
                f'<td style="padding:10px">{btn}{st}</td></tr>')
        if not rows:
            return mark_safe('<p style="color:#999">Немає позицій</p>')
        return mark_safe(
            '<table style="border-collapse:collapse;width:100%;'
            'border-radius:8px;overflow:hidden;border:1px solid rgba(128,128,128,0.2)">'
            '<thead><tr style="background:rgba(21,101,192,0.85);color:#e3f2fd">'
            '<th style="padding:10px;text-align:left">SKU</th>'
            '<th style="padding:10px;text-align:left">Назва</th>'
            '<th style="padding:10px;text-align:center">К-сть</th>'
            '<th style="padding:10px;text-align:left">Дія</th>'
            '</tr></thead><tbody>' + "".join(rows) + '</tbody></table>')
    label_buttons_detail.short_description = "Друк етикеток"

    # ── Завантаження ───────────────────────────────────────────────────────────

    def label_upload_widget(self, obj):
        # CSRF через hidden input Django, не через cookie
        return mark_safe('''
        <div style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);
                    padding:16px;border-radius:8px">
          <p style="margin:0 0 10px;font-weight:bold;opacity:0.9">
            📤 Завантажити або оновити .dymo файли</p>
          <input type="file" id="dymoFiles" multiple accept=".dymo"
                 style="margin-bottom:10px;display:block;opacity:0.85">
          <button type="button" id="dymoUploadBtn"
                  style="background:#4caf50;color:#fff;border:none;padding:8px 20px;
                         border-radius:6px;cursor:pointer;font-size:14px;font-weight:bold">
            ⬆️ Завантажити на сервер
          </button>
          <div id="dymoResult" style="margin-top:10px;font-size:13px"></div>
        </div>
        <script>
        document.getElementById("dymoUploadBtn").addEventListener("click", function() {
            var input = document.getElementById("dymoFiles");
            var result = document.getElementById("dymoResult");
            if (!input.files.length) {
                result.innerHTML = "<span style=\'color:#f44336\'>Оберіть файли!</span>";
                return;
            }
            var formData = new FormData();
            for (var i = 0; i < input.files.length; i++) {
                formData.append("labels", input.files[i]);
            }
            // CSRF з Django hidden input
            var csrfEl = document.querySelector("[name=csrfmiddlewaretoken]");
            var csrf = csrfEl ? csrfEl.value : "";
            result.innerHTML = "⏳ Завантаження...";
            fetch("/labels/upload/", {
                method: "POST",
                headers: {"X-CSRFToken": csrf},
                body: formData
            })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                var html = "<ul style=\'margin:5px 0;padding-left:20px\'>";
                data.results.forEach(function(r) {
                    var icon = r.status === "created" ? "✅" : r.status === "updated" ? "🔄" : "❌";
                    var color = r.status === "error" ? "#f44336" : "#4caf50";
                    html += "<li style=\'color:" + color + "\'>" + icon + " " + r.name + ": " + r.status;
                    if (r.size) html += " (" + (r.size/1024).toFixed(1) + " KB)";
                    html += "</li>";
                });
                html += "</ul><button onclick=\'location.reload()\' "
                     + "style=\'background:#1976d2;color:#fff;border:none;padding:6px 14px;"
                     + "border-radius:4px;cursor:pointer;margin-top:6px\'>🔄 Оновити</button>";
                result.innerHTML = html;
            })
            .catch(function(e) {
                result.innerHTML = "<span style=\'color:#f44336\'>Помилка: " + e + "</span>";
            });
        });
        </script>
        ''')
    label_upload_widget.short_description = "Завантажити етикетки"

    # ── Склад ──────────────────────────────────────────────────────────────────

    def stock_summary(self, obj):
        from inventory.models import InventoryTransaction
        lines = obj.lines.all()
        if not lines:
            return "Немає позицій"
        rows, has_problems = [], False
        for line in lines:
            sku = line.product.sku if line.product else line.sku_raw
            name = line.product.name if line.product else "— SKU не знайдено —"
            needed = float(line.qty or 0)
            if line.product:
                result = InventoryTransaction.objects.filter(
                    product=line.product).aggregate(total=Sum('qty'))
                stock = float(result['total'] or 0)
                if stock <= 0:
                    s = "🚫 НЕМАЄ"
                    row_style = "border-left:4px solid #f44336"
                    tc = "#f44336"
                    has_problems = True
                elif stock < needed:
                    s = f"⚠️ {int(stock)} / треба {int(needed)}"
                    row_style = "border-left:4px solid #ff9800"
                    tc = "#ff9800"
                    has_problems = True
                else:
                    s = f"✅ {int(stock)} шт."
                    row_style = "border-left:4px solid #4caf50"
                    tc = "#4caf50"
            else:
                s = "⚠️ Не в базі"
                row_style = "border-left:4px solid #ff9800"
                tc = "#ff9800"

            rows.append(
                f"<tr style='{row_style}'>"
                f"<td style='padding:10px;font-weight:bold'>{sku}</td>"
                f"<td style='padding:10px'>{name}</td>"
                f"<td style='padding:10px;text-align:center'>{int(needed)}</td>"
                f"<td style='padding:10px;color:{tc};font-weight:bold'>{s}</td></tr>"
            )

        summary_icon = "⚠️" if has_problems else "✅"
        summary_text = "Є проблеми з наявністю!" if has_problems else "Всі товари є на складі"
        summary_color = "#f44336" if has_problems else "#4caf50"

        return mark_safe(
            f'<div style="border-left:4px solid {summary_color};padding:10px 16px;'
            f'margin-bottom:12px;border-radius:4px;font-weight:bold;'
            f'color:{summary_color}">'
            f'{summary_icon} {summary_text}</div>'
            '<table style="border-collapse:collapse;width:100%;'
            'border-radius:8px;overflow:hidden;border:1px solid rgba(128,128,128,0.2)">'
            '<thead><tr style="background:rgba(55,71,79,0.9);color:#eceff1">'
            '<th style="padding:10px;text-align:left">SKU</th>'
            '<th style="padding:10px;text-align:left">Назва</th>'
            '<th style="padding:10px;text-align:center">Потрібно</th>'
            '<th style="padding:10px;text-align:left">Склад</th>'
            '</tr></thead>'
            '<tbody style="background:transparent">'
            + "".join(rows) +
            '</tbody></table>'
        )
    stock_summary.short_description = "📦 Залишки на складі"
    
    # ══════════════════════════════════════════════════════════════════════════
    # MANUAL IMPORT EXCEL
    # ══════════════════════════════════════════════════════════════════════════
    
    change_list_template = 'admin/sales/salesorder_changelist.html'
    
    def get_urls(self):
        """Додаємо URL для manual import."""
        from django.urls import path
        urls = super().get_urls()
        custom_urls = [
            path('import-excel/', 
                 self.admin_site.admin_view(self.import_excel_view), 
                 name='sales_salesorder_import'),
        ]
        return custom_urls + urls
    
    def import_excel_view(self, request):
        """View для manual import Excel з створенням товарів."""
        from django.shortcuts import render, redirect
        from django.contrib import messages
        from .forms import SalesImportForm
        import openpyxl
        from decimal import Decimal
        import re
        from collections import defaultdict
        
        if request.method == 'POST':
            form = SalesImportForm(request.POST, request.FILES)
            if form.is_valid():
                excel_file = request.FILES['excel_file']
                sheet_name = form.cleaned_data['sheet_name']
                import_mode = form.cleaned_data['import_mode']
                update_fields = form.cleaned_data.get('update_fields', [])
                dry_run = form.cleaned_data['dry_run']
                
                def parse_price(value):
                    if not value:
                        return None, None
                    s = str(value).strip()
                    currency = 'USD' if '$' in s else 'EUR' if '€' in s else 'USD'
                    price_str = re.sub(r'[^\d\.]', '', s)
                    try:
                        return Decimal(price_str) if price_str else None, currency
                    except:
                        return None, currency
                
                def convert_date(value):
                    if not value:
                        return None
                    from datetime import datetime, date
                    if isinstance(value, datetime):
                        return value.date()
                    if isinstance(value, date):
                        return value
                    if isinstance(value, str):
                        value = value.strip()
                        for fmt in ['%d.%m.%Y', '%d/%m/%Y', '%Y-%m-%d']:
                            try:
                                return datetime.strptime(value, fmt).date()
                            except:
                                continue
                    return None
                
                try:
                    wb = openpyxl.load_workbook(excel_file)
                    sheet = wb[sheet_name]
                    
                    stats = {
                        'processed': 0,
                        'created': 0,
                        'updated': 0,
                        'skipped': 0,
                        'lines_created': 0,
                        'errors': []
                    }
                    
                    # Групуємо рядки по order_number
                    orders_data = defaultdict(lambda: {'lines': [], 'header': None})
                    
                    for row_idx, row in enumerate(sheet.iter_rows(min_row=2, values_only=True), 2):
                        try:
                            order_number = row[15]  # Sales Order
                            if not order_number:
                                continue
                            
                            order_number = str(order_number).strip()
                            
                            # Header data (береться з першого рядка замовлення)
                            if orders_data[order_number]['header'] is None:
                                unit_price, unit_curr = parse_price(row[11])
                                total_price, total_curr = parse_price(row[12])
                                shipping_cost, ship_curr = parse_price(row[13])
                                
                                orders_data[order_number]['header'] = {
                                    'order_number': order_number,
                                    'source': row[14] or 'manual',
                                    'order_date': convert_date(row[0]),
                                    'shipped_at': convert_date(row[1]),
                                    'shipping_deadline': convert_date(row[17]),
                                    'shipping_courier': row[2] or '',
                                    'tracking_number': row[3] or '',
                                    'lieferschein_nr': row[4] or '',
                                    'shipping_region': row[5] or '',
                                    'shipping_address': row[7] or '',
                                    'client': row[18] or row[6] or '',
                                    'email': row[16] or '',
                                    'phone': row[20] or '',
                                    'contact_name': row[19] or '',
                                    'currency': total_curr or 'USD',
                                    'shipping_cost': shipping_cost or Decimal('0'),
                                    'shipping_currency': ship_curr or 'EUR',
                                }
                            
                            # Line item (товар)
                            sku = (row[8] or '').strip()  # product number
                            qty_raw = row[10]  # QTY
                            
                            if sku and qty_raw:
                                try:
                                    qty = Decimal(str(qty_raw))
                                except:
                                    qty = Decimal('1')
                                
                                unit_price, unit_curr = parse_price(row[11])
                                total_price, total_curr = parse_price(row[12])
                                
                                orders_data[order_number]['lines'].append({
                                    'sku': sku,
                                    'qty': qty,
                                    'unit_price': unit_price,
                                    'total_price': total_price,
                                    'currency': unit_curr or 'USD',
                                })
                        
                        except Exception as e:
                            stats['errors'].append(f"Рядок {row_idx}: {str(e)}")
                    
                    # Створюємо/оновлюємо замовлення
                    from sales.models import SalesOrder, SalesOrderLine
                    from inventory.models import Product
                    from crm.models import Customer
                    
                    for order_number, data in orders_data.items():
                        try:
                            stats['processed'] += 1
                            header = data['header']
                            
                            existing = SalesOrder.objects.filter(order_number=order_number).first()
                            
                            if import_mode == 'create' and existing:
                                stats['skipped'] += 1
                                continue
                            
                            # Генеруємо customer_key
                            if header['email'] or header['client']:
                                customer_key = Customer.generate_key(
                                    header['email'] or header['client'],
                                    header['client'] or header['email']
                                )
                                header['customer_key'] = customer_key
                                
                                # Створюємо Customer якщо немає
                                if not dry_run:
                                    Customer.objects.get_or_create(
                                        external_key=customer_key,
                                        defaults={
                                            'name': header['client'] or header['email'].split('@')[0],
                                            'email': header['email'],
                                            'phone': header['phone'],
                                            'country': header['shipping_region'][:100] if header['shipping_region'] else '',
                                            'source': header['source'],
                                        }
                                    )
                            
                            if not dry_run:
                                if existing:
                                    # Оновлюємо header
                                    for key, val in header.items():
                                        setattr(existing, key, val)
                                    existing.save()
                                    
                                    # Видаляємо старі lines
                                    existing.lines.all().delete()
                                    order = existing
                                    stats['updated'] += 1
                                else:
                                    # Створюємо нове
                                    order = SalesOrder.objects.create(**header)
                                    stats['created'] += 1
                                
                                # Створюємо lines
                                for line_data in data['lines']:
                                    # Шукаємо Product по SKU
                                    product = Product.objects.filter(sku=line_data['sku']).first()
                                    
                                    if product:
                                        SalesOrderLine.objects.create(
                                            order=order,
                                            product=product,
                                            sku_raw=line_data['sku'],
                                            qty=line_data['qty'],
                                            unit_price=line_data['unit_price'],
                                            total_price=line_data['total_price'],
                                            currency=line_data['currency'],
                                        )
                                        stats['lines_created'] += 1
                                    else:
                                        stats['errors'].append(f"SKU {line_data['sku']} не знайдено в інвентарі")
                            else:
                                stats['updated' if existing else 'created'] += 1
                                stats['lines_created'] += len(data['lines'])
                        
                        except Exception as e:
                            stats['errors'].append(f"Замовлення {order_number}: {str(e)}")
                    
                    # Повідомлення
                    mode_names = {'create': 'Тільки нові', 'update': 'Оновлення', 'replace': 'Заміна'}
                    msg_parts = []
                    if dry_run:
                        msg_parts.append("🧪 ТЕСТОВИЙ РЕЖИМ")
                    msg_parts.extend([
                        f"📊 Режим: {mode_names.get(import_mode, import_mode)}",
                        f"✅ Оброблено: {stats['processed']}",
                        f"🆕 Створено: {stats['created']}",
                        f"🔄 Оновлено: {stats['updated']}",
                        f"📦 Товарів додано: {stats['lines_created']}",
                        f"⏭️ Пропущено: {stats['skipped']}"
                    ])
                    msg = "\n".join(msg_parts)
                    
                    if stats['errors']:
                        msg += f"\n⚠️ Помилок: {len(stats['errors'])}"
                        for err in stats['errors'][:5]:
                            messages.warning(request, err)
                    
                    messages.success(request, msg)
                    return redirect('admin:sales_salesorder_changelist')
                
                except Exception as e:
                    messages.error(request, f"❌ Критична помилка: {str(e)}")
            else:
                for field, errors in form.errors.items():
                    for error in errors:
                        messages.error(request, f"{field}: {error}")
        else:
            form = SalesImportForm()
        
        context = {
            **self.admin_site.each_context(request),
            'form': form,
            'title': '📥 Імпорт замовлень з Excel',
            'opts': self.model._meta,
        }
        return render(request, 'admin/sales/import_excel.html', context)
