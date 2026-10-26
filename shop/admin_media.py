"""Адмінка «🖼 Фото й даташити»: що сайт отримує з Minerva, звідки взято, стан індексації (логіка — shop/media.py)."""
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.views.decorators.http import require_POST

from inventory.models import Product

from . import media
from .models import ProductMedia, Shop, ShopMediaSettings, ShopMediaSite

RECHECK_CHOICES = [0, 1, 3, 7, 14, 30]


def _size(n):
    if not n:
        return "—"
    return f"{n / 1024:.0f} КБ" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} МБ"


def status_text(row: ProductMedia) -> tuple[str, str]:
    """(колір, текст) стану рядка — однаково в списку й у картці товару."""
    if row.status == ProductMedia.OK:
        return "#81c784", "✅ з посилання" if row.source == "url" else "✅ файл Minerva"
    if row.status == ProductMedia.FALLBACK:
        return "#ffb74d", "⚠ посилання недоступне → файл Minerva"
    if row.status == ProductMedia.NONE:
        return "#9e9e9e", "— у Minerva немає → сайт показує свій файл"
    if row.status == ProductMedia.ERROR and row.stale:
        when = timezone.localtime(row.changed_at or row.checked_at).strftime("%d.%m.%Y") if (row.changed_at or row.checked_at) else ""
        return "#ffb74d", f"⚠ зараз недоступно → остання перевірена копія {when}".strip()
    if row.status == ProductMedia.ERROR:
        return "#e57373", "❌ недоступно → сайт показує свій файл"
    return "#90a4ae", "⏳ чекає перевірки"


def _problems(row):
    parts = []
    if row.url_error:
        parts.append(f"посилання: {row.url_error}")
    if row.file_error and row.file_error != "файл не завантажено":
        parts.append(f"файл: {row.file_error}")
    return " · ".join(parts)


def sites_state() -> tuple[bool, dict]:
    """(увімкнено, {магазин: SHA-256, що сайт уже має}) — для позначки 🌐 у списку «Асортимент і ціни»."""
    return (media.settings_obj().enabled,
            {s.shop_id: set(s.have or []) for s in ShopMediaSite.objects.all()})


def listing_marker(listing, enabled, have_by_shop) -> str:
    """🌐✓ — сайт магазину вже показує фото й даташит з Minerva, 🌐⏳ — ще не забрав; без позначки — свої файли сайту."""
    if not enabled or not listing.shop.media_enabled:
        return ""
    rows = {r.kind: r for r in listing.product.shop_media.all()}
    have, lines, states = have_by_shop.get(listing.shop_id, set()), [], []
    for kind, label in media.KIND_LABEL.items():
        r = rows.get(kind)
        if r is None or not r.usable:
            lines.append(f"{label}: свій файл сайту" if r and r.status != ProductMedia.PENDING else f"{label}: ще не перевірено")
            continue
        on = r.sha256 in have
        states.append(on)
        lines.append(f"{label}: з Minerva — {'✅ на сайті' if on else '⏳ сайт ще не забрав'}")
    if not listing.is_visible:
        lines.append("позицію сховано — сайт її не отримує")
    title = f"Сайт «{listing.shop}»: " + " · ".join(lines)
    if not states or not listing.is_visible:
        return format_html('<span title="{}" style="font-size:11px;opacity:.35">🌐</span>', title)
    ok = all(states)
    return format_html('<span title="{}" style="font-size:11px;color:{}">🌐{}</span>', title,
                       "#81c784" if ok else "#ffb74d", "✓" if ok else "⏳")


def product_panel(product) -> str:
    """Блок «Для сайту» в картці товару (🔗 Медіа та документи)."""
    if not product or not product.pk:
        return "Спершу збережіть товар."
    rows = {r.kind: r for r in ProductMedia.objects.filter(product=product)}
    visible = product.shop_listings.filter(is_visible=True, shop__is_active=True).exists()
    lines = []
    for kind, label in ProductMedia.KINDS:
        row = rows.get(kind)
        if row is None:
            lines.append(format_html('<div><b>{}</b>: <span style="color:#90a4ae">ще не перевірено</span></div>', label))
            continue
        color, text = status_text(row)
        extra = []
        if row.usable:
            extra.append(_size(row.size))
        if row.checked_at:
            extra.append("перевірено " + timezone.localtime(row.checked_at).strftime("%d.%m.%Y %H:%M"))
        problem = _problems(row)
        sites = ", ".join(f"{name}: {state}" for name, state in media.on_site(row))
        lines.append(format_html(
            '<div style="margin-bottom:4px"><b>{}</b>: <span style="color:{}">{}</span>'
            '<span style="color:var(--text-muted,#888)"> · {}</span>{}{}</div>',
            label, color, text, " · ".join(extra) or "—",
            format_html('<div style="color:#e57373;font-size:11px">{}</div>', problem) if problem else "",
            format_html('<div style="font-size:11px;color:var(--text-muted,#888)">{}</div>', sites) if sites else ""))
    note = "" if visible else format_html(
        '<div style="color:#ffb74d;font-size:11px;margin-top:4px">Товар не показується в жодному магазині — '
        'сайт його не отримує.</div>')
    return format_html(
        '{}{}<div style="margin-top:6px"><button type="submit" class="button" formaction="{}" formnovalidate>'
        '🔄 Перевірити зараз</button> <a href="{}?q={}">усі фото й даташити →</a>'
        '<div class="help">Порядок: посилання → файл у цій картці → файл сайту. Незбережені зміни на сторінці '
        'не зберігаються — спершу «Зберегти», потім перевірка.</div></div>',
        format_html_join("", "{}", ((x,) for x in lines)), note,
        reverse("admin:shop_productmedia_check_product", args=[product.pk]),
        reverse("admin:shop_productmedia_changelist"), product.sku)


class ShopMediaFilter(admin.SimpleListFilter):
    """Товари, видимі в асортименті вибраного магазину."""
    title = "Магазин"
    parameter_name = "shop"

    def lookups(self, request, model_admin):
        return [(str(s.pk), f"{s.name} ({s.slug})" + (" 🧪" if s.is_test else ""))
                for s in Shop.objects.filter(is_active=True).order_by("-is_default", "name")]

    def queryset(self, request, queryset):
        if self.value() and self.value().isdigit():
            return queryset.filter(product__shop_listings__shop_id=int(self.value()),
                                   product__shop_listings__is_visible=True).distinct()
        return queryset


class OnSiteFilter(admin.SimpleListFilter):
    """Чи забрав сайт копію: для вибраного магазину (фільтр «Магазин») або для будь-якого сайту."""
    title = "На сайті магазину"
    parameter_name = "onsite"

    def lookups(self, request, model_admin):
        return [("have", "✅ сайт забрав"), ("missing", "⏳ ще не забрав"), ("failed", "❌ не вдалося завантажити"),
                ("none", "— сайту нічого віддавати (немає копії)")]

    def queryset(self, request, queryset):
        value = self.value()
        if not value:
            return queryset
        if value == "none":
            return queryset.filter(stored="")
        sites = ShopMediaSite.objects.filter(shop__is_active=True)
        shop = request.GET.get("shop", "")
        if shop.isdigit():
            sites = sites.filter(shop_id=int(shop))
        have, failed = set(), set()
        for site in sites:
            have |= set(site.have or [])
            failed |= set((site.failed or {}).keys())
        usable = queryset.exclude(stored="")
        if value == "have":
            return usable.filter(sha256__in=have)
        if value == "failed":
            return usable.filter(sha256__in=failed)
        return usable.exclude(sha256__in=have | failed)


@admin.register(ProductMedia)
class ProductMediaAdmin(admin.ModelAdmin):
    change_list_template = "admin/shop/productmedia/change_list.html"
    list_display = ["product_link", "kind", "status_col", "size_col", "checked_at", "site_col", "problem_col"]
    list_filter = [ShopMediaFilter, OnSiteFilter, "kind", "status", "source", "stale"]
    search_fields = ["product__sku", "product__name"]
    list_select_related = ["product"]
    actions = ["action_check"]
    readonly_fields = ["product_link", "kind", "status_col", "source", "stale", "url_error", "file_error",
                       "sha256", "size_col", "content_type", "stored", "checked_at", "changed_at", "inputs", "site_col"]
    fields = readonly_fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False   # лише перегляд: стан змінює індексація

    def _selected_shop(self, request):
        value = request.GET.get("shop", "")
        return Shop.objects.filter(pk=int(value), is_active=True).first() if value.isdigit() else None

    def changelist_view(self, request, extra_context=None):
        self._sites = None   # кеш звітів сайтів на один запит
        shop = self._selected_shop(request)
        extra_context = {**(extra_context or {}), "media_recheck_choices": RECHECK_CHOICES,
                         "media_shops": Shop.objects.filter(is_active=True).order_by("-is_default", "name"),
                         "media_shop": shop,
                         "media_progress": media.progress(shop),
                         "media_can_manage": request.user.has_perm("shop.change_shop")}
        return super().changelist_view(request, extra_context)

    @admin.display(description="Товар", ordering="product__sku")
    def product_link(self, obj):
        return format_html('<a href="{}">{}</a> <span style="color:var(--text-muted,#888)">{}</span>',
                           reverse("admin:inventory_product_change", args=[obj.product_id]),
                           obj.product.sku, (obj.product.name or "")[:50])

    @admin.display(description="Стан", ordering="status")
    def status_col(self, obj):
        color, text = status_text(obj)
        return format_html('<span style="color:{}">{}</span>', color, text)

    @admin.display(description="Розмір", ordering="size")
    def size_col(self, obj):
        return _size(obj.size) if obj.usable else "—"

    def _site_rows(self):
        if getattr(self, "_sites", None) is None:
            self._sites = list(ShopMediaSite.objects.filter(shop__is_active=True).select_related("shop"))
        return self._sites

    @admin.display(description="На сайті")
    def site_col(self, obj):
        states = media.on_site(obj, self._site_rows())
        if not states:
            return "—"
        if len(states) == 1:
            return states[0][1]
        return format_html_join(format_html("<br>"), "{}: {}", states)

    @admin.display(description="Помилка")
    def problem_col(self, obj):
        text = _problems(obj)
        return format_html('<span style="color:#e57373;font-size:11px">{}</span>', text) if text else ""

    @admin.action(description="🔄 Перевірити вибрані зараз")
    def action_check(self, request, queryset):
        ids = list(queryset.values_list("product_id", flat=True).distinct())
        if media.start("all", product_ids=ids, by=request.user.get_username()):
            self.message_user(request, f"Перевірка {len(ids)} товарів запущена — прогрес угорі сторінки.")
        else:
            self.message_user(request, "Уже йде інша індексація — дочекайтесь її завершення.", messages.WARNING)

    # ── Кнопки панелі ──────────────────────────────────────────────────────────
    def get_urls(self):
        view = self.admin_site.admin_view
        return [
            path("status/", view(self._status_view), name="shop_productmedia_status"),
            path("run/", view(require_POST(self._run_view)), name="shop_productmedia_run"),
            path("settings/", view(require_POST(self._settings_view)), name="shop_productmedia_settings"),
            path("site-sync/<int:shop_id>/", view(require_POST(self._site_sync_view)), name="shop_productmedia_site_sync"),
            path("site-toggle/<int:shop_id>/", view(require_POST(self._site_toggle_view)),
                 name="shop_productmedia_site_toggle"),
            path("check-product/<int:pk>/", view(require_POST(self._check_product_view)),
                 name="shop_productmedia_check_product"),
        ] + super().get_urls()

    def _manage(self, request):
        if not request.user.has_perm("shop.change_shop"):
            raise PermissionDenied

    def _status_view(self, request):
        if not self.has_view_permission(request):
            raise PermissionDenied
        return JsonResponse(media.progress(self._selected_shop(request)))

    def _run_view(self, request):
        self._manage(request)
        mode = "all" if request.POST.get("mode") == "all" else "changed"
        shop_id = request.POST.get("shop", "")
        shop = Shop.objects.filter(pk=shop_id).first() if shop_id.isdigit() else None
        if media.start(mode, by=request.user.get_username(), shop_id=shop.pk if shop else None):
            self.message_user(request, "Індексацію запущено" + (f" для «{shop}»" if shop else "") + " — прогрес на панелі.")
        else:
            self.message_user(request, "Уже йде індексація — дочекайтесь її завершення.", messages.WARNING)
        return redirect("admin:shop_productmedia_changelist")

    def _settings_view(self, request):
        self._manage(request)
        media.settings_obj()
        changes = {}
        if "enabled" in request.POST:
            changes["enabled"] = request.POST["enabled"] == "1"
        if request.POST.get("recheck_days", "").isdigit() and int(request.POST["recheck_days"]) in RECHECK_CHOICES:
            changes["recheck_days"] = int(request.POST["recheck_days"])
        if changes:
            ShopMediaSettings.objects.filter(pk=1).update(**changes)
        if changes.get("enabled") is True:
            self.message_user(request, "Увімкнено: сайт отримує перевірені фото й даташити з Minerva "
                                       "(після оновлення каталогу сайту, зазвичай за 1 хв).")
            media.ping_sites()
        elif changes.get("enabled") is False:
            self.message_user(request, "Вимкнено: сайт знову показує лише власні файли.")
        return redirect("admin:shop_productmedia_changelist")

    def _site_sync_view(self, request, shop_id):
        self._manage(request)
        shop = get_object_or_404(Shop, pk=shop_id)
        if not getattr(shop, "media_site", None) or not shop.media_site.sync_url:
            self.message_user(request, f"«{shop}»: сайт ще не повідомив адресу синхронізації — оновіть файли сайту "
                                       "і відкрийте будь-яку сторінку магазину.", messages.WARNING)
        else:
            import threading
            threading.Thread(target=media.ping_site, args=(shop,), daemon=True).start()
            self.message_user(request, f"«{shop}»: сайт отримав запит забрати нові файли — стан оновиться за хвилину.")
        return redirect("admin:shop_productmedia_changelist")

    def _site_toggle_view(self, request, shop_id):
        """Перемикач одного магазину: його сайт отримує файли з Minerva або показує власні."""
        self._manage(request)
        shop = get_object_or_404(Shop, pk=shop_id)
        on = request.POST.get("enabled") == "1"
        Shop.objects.filter(pk=shop.pk).update(media_enabled=on)
        if on:
            shop.media_enabled = True
            import threading
            threading.Thread(target=media.ping_site, args=(shop,), daemon=True).start()
        self.message_user(request, f"«{shop}»: " + ("сайт знову отримує фото й даташити з Minerva." if on
                                                     else "вимкнено — сайт показує власні файли."))
        return redirect("admin:shop_productmedia_changelist")

    def _check_product_view(self, request, pk):
        product = get_object_or_404(Product, pk=pk)
        if not request.user.has_perm("inventory.change_product"):
            raise PermissionDenied
        rows = media.index_product(product)
        text = " · ".join(f"{dict(ProductMedia.KINDS)[r.kind]}: {status_text(r)[1]}" for r in rows)
        self.message_user(request, f"{product.sku}: {text}")
        if any(r.usable for r in rows):
            media.ping_sites()
        return redirect(reverse("admin:inventory_product_change", args=[pk]))
