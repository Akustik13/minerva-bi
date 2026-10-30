"""bots/admin_prices.py — «💲 Ціни DigiKey ← склад Minerva»: порівняння, оновлення, історія, налаштування.

Сторінки (логіка — bots/services/dk_prices.py):
  /admin/bots/digikeypricerun/compare/        порівняння «DigiKey зараз → стане з цін Minerva», вибір, оновлення
  /admin/bots/digikeypricerun/<pk>/change/    звіт оновлення: було → стало, стан на DigiKey, підтвердження
  /admin/bots/digikeypricesettings/           правила за замовчуванням, автооновлення, сповіщення
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib import admin, messages
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from .models import DigiKeyListing, DigiKeyPriceChange, DigiKeyPriceRun, DigiKeyPriceSettings
from .services import dk_prices as svc

RULE_FIELDS = ("adjust_pct", "adjust_fixed", "rounding", "tiers_mode", "min_margin_pct", "source")
QUICK_PCT = ("-10", "-5", "-2", "+2", "+5", "+10")
STATUS_COLORS = {
    "queued": "var(--text-muted)", "sending": "var(--accent)", "sent": "var(--accent)",
    "verified": "var(--ok)", "partial": "var(--warn)", "failed": "var(--err)", "held": "var(--warn)",
    "nothing": "var(--text-muted)", "mismatch": "var(--err)", "error": "var(--err)",
    "skipped": "var(--text-muted)", "rejected": "var(--text-muted)",
}


def _can_view(request):
    return request.user.has_perm("bots.view_digikeylisting") or request.user.has_perm("bots.change_digikeylisting")


def _can_change(request):
    return request.user.has_perm("bots.change_digikeylisting")


def _compare_url(query: str = "") -> str:
    url = reverse("admin:bots_digikeypricerun_compare")
    return f"{url}?{query}" if query else url


def _pks(raw) -> list[int]:
    return [int(x) for x in str(raw or "").replace(" ", "").split(",") if x.isdigit()][:2000]


def _selected(request):
    """?ids= (лістинги) / ?products= (товари) — лише вибрані; (queryset або None, опис фільтра)."""
    ids, products = _pks(request.GET.get("ids")), _pks(request.GET.get("products"))
    if not ids and not products:
        return None, ""
    qs = svc.listings_qs()
    qs = qs.filter(pk__in=ids) if ids else qs.filter(product_id__in=products)
    return qs, ("ids", ",".join(map(str, ids))) if ids else ("products", ",".join(map(str, products)))


REFRESH_FAILED = "dk-prices-refresh-failed"


def _auto_refresh(request, listings, force: bool) -> str:
    """Свіжі ціни DigiKey перед показом: для вибраних — завжди, для всіх — коли старші за STALE_MINUTES.
    Збій — повідомлення й 5 хв без повторів (сторінка не чекає на DigiKey знову й знову)."""
    listings = list(listings)
    if not listings or (not force and not svc.stale(listings)) or cache.get(REFRESH_FAILED):
        return ""
    try:
        res = svc.refresh(listings)
    except Exception as exc:
        cache.set(REFRESH_FAILED, 1, 300)
        messages.warning(request, f"Свіжі ціни з DigiKey не прочитано ({exc}) — показано останні збережені.")
        return ""
    return "щойно" + (f" · офера не знайдено: {', '.join(res['not_found'][:5])}" if res["not_found"] else "")


# ── Відображення рядків ───────────────────────────────────────────────────────

def _cells(dk, new):
    """Ступені «зараз на DigiKey» і «стане» з % для кожної кількості — як svc.tier_compare (його ж бачить запобіжник
    автооновлення): новий ступінь — % до ціни DigiKey, що зараз діє на цю кількість, прибраний — підказка, скільки
    стане. new None — нових цін немає."""
    eff = {c["qty"]: c for c in svc.tier_compare(dk, new)}
    old, new_q = dict(dk), {q for q, _ in new or []}

    def pct(c):
        return f"{svc.fmt_signed(c['pct'])} %" if c and c["pct"] is not None and c["new"] != c["old"] else ""

    cells = []
    for q, p in new or []:
        c, was = eff.get(q), old.get(q)
        cell = {"qty": q, "price": svc.fmt_price(p), "pct": pct(c), "dir": "", "title": ""}
        if was is not None:
            cell["cls"] = "same" if was == p else ("up" if p > was else "down")
        else:
            cell["cls"] = "added"
            if cell["pct"]:
                cell["dir"] = "up" if c["new"] > c["old"] else "down"
                cell["title"] = f"новий ступінь; зараз на DigiKey {q} шт — по {svc.fmt_price(c['old'])}"
        cells.append(cell)
    dk_cells = []
    for q, p in dk:
        gone = new is not None and q not in new_q
        title = ""
        if gone:
            title = "цього ступеня не буде"
            if pct(eff.get(q)):
                title += f" — {q} шт стане по {svc.fmt_price(eff[q]['new'])} ({pct(eff[q])})"
        dk_cells.append({"qty": q, "price": svc.fmt_price(p), "gone": gone, "title": title})
    return {"dk": dk_cells, "new": cells}


def _tier_cells(row):
    """Для шаблону: ступені «зараз», «Minerva», «стане» з % для кожної кількості."""
    return {**_cells(row.dk, row.new), "minerva": [{"qty": q, "price": svc.fmt_price(p)} for q, p in row.minerva]}


def _categories():
    """slug → назва з довідника категорій товарів."""
    from inventory.models import ProductCategory
    return dict(ProductCategory.objects.values_list("slug", "name"))


def _cat_options(rows_views):
    """Варіанти фільтра: тип DigiKey і категорія Minerva — лише ті, що є на сторінці, з кількістю."""
    def count(key):
        out = {}
        for v in rows_views:
            if v[key]:
                out[v[key]] = out.get(v[key], 0) + 1
        return sorted(out.items())
    return {"dk": count("cat"), "minerva": count("pcat")}


def _row_view(row, cur, cats=None):
    kind = "diff" if row.differs else ("same" if row.state == "ok" else "problem")
    listing = row.listing
    slug = listing.product.category if listing.product_id else ""
    return {
        "cat": listing.get_category_type_display(),
        "pcat": (cats or {}).get(slug, slug) if slug else "",
        "row": row, "kind": kind, "dir": row.direction, "cells": _tier_cells(row),
        "pk": listing.pk,
        "product_url": reverse("admin:inventory_product_change", args=[listing.product_id]) if listing.product_id else "",
        "listing_url": reverse("admin:bots_digikeylisting_change", args=[listing.pk]),
        "change": "" if not row.change_pct else f"{svc.fmt_signed(row.change_pct)} %",     # 1-й ступінь
        # найбільша зміна (і на новому / прибраному ступені) — коли вона більша, ніж на 1-му ступені
        "max_change": "" if row.max_change_pct is None or (
            row.change_pct is not None and row.max_change_pct <= abs(row.change_pct))
        else f"{svc.fmt_num(row.max_change_pct)} % ({row.max_change_qty} шт)",
        "rate": "" if row.rate in (None, Decimal(1)) else f"1 {row.minerva_currency} = {svc.fmt_num(row.rate) if row.rate >= 1 else format(row.rate.normalize(), 'f')} {cur}",
        "search": f"{row.sku} {row.title} {listing.product.sku if listing.product_id else ''}".lower(),
        "own_pct": "" if listing.price_adjust_pct is None else svc.fmt_num(listing.price_adjust_pct),
        "own_fixed": "" if listing.price_adjust_fixed is None else format(Decimal(listing.price_adjust_fixed).normalize(), "f"),
    }


def _rates(rows, cur):
    seen = {}
    for r in rows:
        if r.minerva_currency and r.minerva_currency != cur and r.minerva_currency not in seen:
            seen[r.minerva_currency] = svc.fx(r.minerva_currency, cur)
    return [{"cur": c, "rate": None if v is None else format(v.quantize(Decimal("0.000001")).normalize(), "f")}
            for c, v in sorted(seen.items())]


# ── Налаштування ──────────────────────────────────────────────────────────────

@admin.register(DigiKeyPriceSettings)
class DigiKeyPriceSettingsAdmin(admin.ModelAdmin):
    readonly_fields = ("tools", "auto_state")
    fieldsets = (
        (None, {"fields": ("tools",)}),
        ("💱 Валюта й ціни Minerva", {"fields": ("currency", "source")}),
        ("🧮 Правила за замовчуванням", {
            "fields": (("adjust_pct", "adjust_fixed"), ("rounding", "tiers_mode"), "min_margin_pct"),
            "description": "Ціна DigiKey = ціна Minerva × курс × (100 + надбавка) % + фіксована добавка → "
                           "округлення → не нижче закупівельної + %. Для окремого товару — своя надбавка "
                           "(⚙ у рядку порівняння або в картці лістингу)."}),
        ("🔁 Автооновлення", {
            "fields": ("auto_mode", ("auto_interval_minutes", "auto_delay_minutes"),
                       ("max_change_pct", "auto_max_items"), "auto_state"),
            "description": "Minerva звіряє ціни з cron (manage.py dk_price_sync --if-due). Самостійно надсилаються "
                           "лише невеликі зміни; великі й масові — на підтвердження, з повідомленням."}),
        ("🔔 Сповіщення", {"fields": (("notify_telegram", "notify_email"), "notify_email_to", "notify_manual")}),
    )

    def has_add_permission(self, request):
        return not DigiKeyPriceSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = DigiKeyPriceSettings.get()
        return redirect(reverse("admin:bots_digikeypricesettings_change", args=[obj.pk]))

    @admin.display(description="Сторінки")
    def tools(self, obj):
        cur = svc.currency(obj)
        return format_html(
            '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
            '<a class="button" href="{}">💲 Порівняти й оновити ціни</a>'
            '<a class="button" href="{}">📜 Історія оновлень</a>'
            '<span style="color:var(--text-muted)">Ціни на DigiKey зараз рахуються в <b>{}</b> ({}) · курси — '
            '<a href="{}">Налаштування цін магазину → Курси валют</a></span></div>',
            _compare_url(), reverse("admin:bots_digikeypricerun_changelist"), cur, svc.currency_source(obj),
            reverse("admin:shop_shopsettings_changelist"))

    @admin.display(description="Стан")
    def auto_state(self, obj):
        parts = []
        if obj.last_auto_at:
            parts.append(f"остання перевірка {timezone.localtime(obj.last_auto_at):%d.%m.%Y %H:%M}"
                         + (f" — {obj.last_auto_result}" if obj.last_auto_result else ""))
        if obj.auto_mode != DigiKeyPriceSettings.AUTO_OFF:
            if not obj.cron_seen_at:
                parts.append("⚠ cron ще не звертався (cron_runner.sh → dk_price_sync)")
            elif timezone.now() - obj.cron_seen_at > timedelta(minutes=15):
                parts.append(f"⚠ cron не звертався з {timezone.localtime(obj.cron_seen_at):%d.%m %H:%M} "
                             "(docker-compose restart cron)")
        return " · ".join(parts) or "—"


# ── Історія + сторінки ────────────────────────────────────────────────────────

@admin.register(DigiKeyPriceRun)
class DigiKeyPriceRunAdmin(admin.ModelAdmin):
    change_list_template = "admin/bots/digikeypricerun/change_list.html"
    list_display = ("number", "created_at", "kind", "status_badge", "counts", "created_by", "rules_text")
    list_filter = ("status", "kind")
    search_fields = ("lines__sku", "created_by")
    list_per_page = 50

    actions = None

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False                     # журнал змін цін — не видаляється

    def has_change_permission(self, request, obj=None):
        return _can_view(request)

    def changelist_view(self, request, extra_context=None):
        return super().changelist_view(request, {"title": "💲 Ціни DigiKey — історія оновлень", **(extra_context or {})})

    def has_view_permission(self, request, obj=None):
        return _can_view(request)

    @admin.display(description="№", ordering="pk")
    def number(self, obj):
        return f"#{obj.pk}"

    @admin.display(description="Стан", ordering="status")
    def status_badge(self, obj):
        return format_html('<b style="color:{}">{}</b>', STATUS_COLORS.get(obj.status, "inherit"),
                           obj.get_status_display())

    @admin.display(description="Позиції")
    def counts(self, obj):
        parts = [f"{obj.total}"]
        if obj.sent:
            parts.append(f"надіслано {obj.sent}")
        if obj.verified:
            parts.append(f"діє {obj.verified}")
        if obj.held:
            parts.append(f"✋ чекає {obj.held}")
        if obj.errors:
            parts.append(f"❌ {obj.errors}")
        return " · ".join(parts)

    @admin.display(description="Правила")
    def rules_text(self, obj):
        try:
            return svc.Rules.from_dict(obj.rules or {}).describe(obj.currency)
        except ValueError:
            return "—"

    def get_urls(self):
        view = self.admin_site.admin_view
        return [
            path("compare/", view(self.compare_view), name="bots_digikeypricerun_compare"),
            path("compare/refresh/", view(self.refresh_view), name="bots_digikeypricerun_refresh"),
            path("compare/apply/", view(self.apply_view), name="bots_digikeypricerun_apply"),
            path("compare/save-rules/", view(self.save_rules_view), name="bots_digikeypricerun_save_rules"),
            path("compare/listing/<int:pk>/", view(self.listing_view), name="bots_digikeypricerun_listing"),
            path("compare/auto-now/", view(self.auto_now_view), name="bots_digikeypricerun_auto_now"),
            path("offers/", view(self.offers_view), name="bots_digikeypricerun_offers"),
            path("offers/store/", view(self.offers_store_view), name="bots_digikeypricerun_offers_store"),
            path("offers/link/<int:pk>/", view(self.offers_link_view), name="bots_digikeypricerun_offers_link"),
            path("<int:pk>/status/", view(self.status_view), name="bots_digikeypricerun_status"),
            path("<int:pk>/verify/", view(self.verify_view), name="bots_digikeypricerun_verify"),
            path("<int:pk>/decide/", view(self.decide_view), name="bots_digikeypricerun_decide"),
        ] + super().get_urls()

    # ── Порівняння ──

    def _rules(self, request, s):
        base = svc.Rules.from_settings(s)
        data = request.POST if request.method == "POST" else request.GET
        if not any(k in data for k in RULE_FIELDS):
            return base, ""
        try:
            return svc.Rules.from_dict(data, base), ""
        except ValueError as exc:
            return base, str(exc)

    def compare_view(self, request):
        if not _can_view(request):
            raise PermissionDenied
        s = DigiKeyPriceSettings.get()
        rules, error = self._rules(request, s)
        cur = svc.currency(s)
        selected, flt = _selected(request)
        listings = list(selected if selected is not None else svc.listings_qs())
        fresh = _auto_refresh(request, listings, force=selected is not None)
        rows = svc.plan(listings, rules=rules, s=s)
        cats = _categories()
        views = [_row_view(r, cur, cats) for r in rows]
        checked = [r.listing.price_checked_at for r in rows if r.listing.price_checked_at and r.state != "no_offer"]
        custom = rules.as_dict() != svc.Rules.from_settings(s).as_dict()
        held = DigiKeyPriceChange.objects.filter(status=DigiKeyPriceChange.HELD).values("run_id").distinct().count()
        context = {
            **self.admin_site.each_context(request),
            "title": "💲 Ціни DigiKey ← склад Minerva", "opts": self.model._meta,
            "s": s, "rules": rules, "rules_dict": rules.as_dict(), "rules_error": error, "custom": custom,
            "rules_text": rules.describe(cur), "cur": cur, "currency_source": svc.currency_source(s),
            "rows": views, "sum": svc.summary(rows), "rates": _rates(rows, cur),
            "checked_min": min(checked) if checked else None, "checked_max": max(checked) if checked else None,
            "rounding_choices": DigiKeyPriceSettings.ROUNDING_CHOICES,
            "tiers_choices": DigiKeyPriceSettings.TIERS_CHOICES,
            "source_choices": DigiKeyPriceSettings.SOURCE_CHOICES,
            "auto_choices": dict(DigiKeyPriceSettings.AUTO_CHOICES), "quick_pct": QUICK_PCT,
            "can_change": _can_change(request), "held_runs": held,
            "query": request.GET.urlencode(), "flt": flt, "fresh": fresh, "stale_minutes": svc.STALE_MINUTES,
            "cat_options": _cat_options(views), "offers_url": reverse("admin:bots_digikeypricerun_offers"),
            "settings_url": reverse("admin:bots_digikeypricesettings_changelist"),
            "history_url": reverse("admin:bots_digikeypricerun_changelist"),
            "fx_url": reverse("admin:shop_shopsettings_changelist"),
        }
        return TemplateResponse(request, "admin/bots/digikeypricerun/compare.html", context)

    def _back(self, request):
        query = request.POST.get("query", "")
        return redirect(_compare_url(query))

    def refresh_view(self, request):
        if request.method != "POST" or not _can_view(request):
            raise PermissionDenied
        try:
            res = svc.refresh()
        except Exception as exc:
            messages.error(request, f"Не вдалося прочитати офери з DigiKey: {exc}")
            return self._back(request)
        text = f"🔄 Ціни прочитано з DigiKey: змінились у {res['updated']}, без змін {res['unchanged']}"
        if res["not_found"]:
            text += f"; офера не знайдено: {len(res['not_found'])} ({', '.join(res['not_found'][:8])}…)"
        messages.success(request, text + ".")
        return self._back(request)

    def apply_view(self, request):
        if request.method != "POST" or not _can_change(request):
            raise PermissionDenied
        s = DigiKeyPriceSettings.get()
        rules, error = self._rules(request, s)
        if error:
            messages.error(request, error)
            return self._back(request)
        ids = [int(i) for i in request.POST.getlist("ids") if str(i).isdigit()]
        listings = svc.listings_qs().filter(pk__in=ids)
        rows = svc.plan(listings, rules, s)
        try:
            run = svc.create_run(rows, request.user, rules)
        except svc.PriceError as exc:
            messages.warning(request, str(exc))
            return self._back(request)
        svc.start(run)
        messages.success(request, f"📤 Оновлення #{run.pk}: {run.total} товарів надсилається на DigiKey. "
                                  "Тут видно хід, потім — перевірка, що нові ціни діють.")
        return redirect(reverse("admin:bots_digikeypricerun_change", args=[run.pk]))

    def save_rules_view(self, request):
        if request.method != "POST" or not _can_change(request):
            raise PermissionDenied
        s = DigiKeyPriceSettings.get()
        rules, error = self._rules(request, s)
        if error:
            messages.error(request, error)
            return self._back(request)
        s.adjust_pct, s.adjust_fixed, s.rounding = rules.adjust_pct, rules.adjust_fixed, rules.rounding
        s.tiers_mode, s.min_margin_pct, s.source = rules.tiers_mode, rules.min_margin_pct, rules.source
        s.save()
        messages.success(request, f"💾 Правила за замовчуванням: {rules.describe(svc.currency(s))} — "
                                  "діють і для автооновлення.")
        return redirect(_compare_url())

    def listing_view(self, request, pk):
        """⚙ у рядку: автооновлення й своя надбавка лістингу."""
        if request.method != "POST" or not _can_change(request):
            raise PermissionDenied
        listing = get_object_or_404(DigiKeyListing, pk=pk)
        pct, fixed = request.POST.get("price_adjust_pct", "").strip(), request.POST.get("price_adjust_fixed", "").strip()
        values = {}
        for name, raw, label in (("price_adjust_pct", pct, "надбавка, %"), ("price_adjust_fixed", fixed, "добавка")):
            if raw == "":
                values[name] = None
                continue
            num = svc._d(raw)
            if num is None or (name == "price_adjust_pct" and num <= -100):
                return JsonResponse({"ok": False, "error": f"«{label}»: потрібне число."}, status=400)
            values[name] = num
        listing.price_adjust_pct = values["price_adjust_pct"]
        listing.price_adjust_fixed = values["price_adjust_fixed"]
        listing.price_auto = request.POST.get("price_auto") in ("1", "on", "true")
        listing.save(update_fields=["price_adjust_pct", "price_adjust_fixed", "price_auto"])
        return JsonResponse({"ok": True})

    def auto_now_view(self, request):
        if request.method != "POST" or not _can_change(request):
            raise PermissionDenied
        try:
            result = svc.auto(force=True)
        except Exception as exc:
            messages.error(request, f"Перевірка не вдалась: {exc}")
            return self._back(request)
        messages.info(request, f"🔁 Перевірка як автооновлення: {result}.")
        last = DigiKeyPriceRun.objects.filter(kind=DigiKeyPriceRun.KIND_AUTO).first()
        if last and timezone.now() - last.created_at < timedelta(minutes=1):
            return redirect(reverse("admin:bots_digikeypricerun_change", args=[last.pk]))
        return self._back(request)

    # ── 🔍 Перевірити офери: живі дані DigiKey ↔ Minerva ──

    def offers_view(self, request):
        if not _can_view(request):
            raise PermissionDenied
        import json
        s = DigiKeyPriceSettings.get()
        cur = svc.currency(s)
        try:
            offers = svc._fetch_offers()
            data = svc.reconcile(offers)
            error = ""
        except Exception as exc:
            offers, data, error = [], {"rows": [], "summary": {}, "checked_at": None}, str(exc)
        cats = _categories()
        rows = []
        for r in data["rows"]:
            l, o = r["listing"], r["offer"] or {}
            slug = l.product.category if l and l.product_id else ""
            rows.append({**r, "sku_dk": o.get("supplierSku") or "", "title": o.get("title") or (l.dk_title if l else ""),
                         "offer_id": o.get("id") or (l.dk_offer_id if l else ""),
                         "live_f": [(q, svc.fmt_price(v)) for q, v in r["live"]],
                         "local_f": [(q, svc.fmt_price(v)) for q, v in r["local"]],
                         "cat": l.get_category_type_display() if l else "", "pcat": cats.get(slug, slug) if slug else "",
                         "listing_url": reverse("admin:bots_digikeylisting_change", args=[l.pk]) if l else "",
                         "search": f"{o.get('supplierSku', '')} {o.get('title', '')} {l.product.sku if l and l.product_id else ''}".lower()})
        raw_id = request.GET.get("raw", "")
        raw = next((o for o in offers if o.get("id") == raw_id), None)
        context = {
            **self.admin_site.each_context(request),
            "title": "🔍 Офери DigiKey ↔ Minerva", "opts": self.model._meta, "rows": rows, "sum": data["summary"],
            "checked_at": data["checked_at"], "error": error, "cur": cur, "currency_source": svc.currency_source(s),
            "raw": json.dumps(raw, ensure_ascii=False, indent=2) if raw else "", "raw_id": raw_id,
            "cat_options": _cat_options(rows), "can_change": _can_change(request),
            "compare_url": _compare_url(), "settings_url": reverse("admin:bots_digikeypricesettings_changelist"),
        }
        return TemplateResponse(request, "admin/bots/digikeypricerun/offers.html", context)

    def offers_store_view(self, request):
        """«🔄 Записати в Minerva»: ціни й кількість з DigiKey → лістинги (як «Прочитати ціни з DigiKey»)."""
        if request.method != "POST" or not _can_change(request):
            raise PermissionDenied
        try:
            res = svc.refresh()
        except Exception as exc:
            messages.error(request, f"Не вдалося прочитати офери з DigiKey: {exc}")
        else:
            messages.success(request, f"Записано з DigiKey: змінились ціни у {res['updated']}, без змін {res['unchanged']}.")
        return redirect(reverse("admin:bots_digikeypricerun_offers"))

    def offers_link_view(self, request, pk):
        if request.method != "POST" or not _can_change(request):
            raise PermissionDenied
        listing = get_object_or_404(DigiKeyListing, pk=pk)
        offer_id = request.POST.get("offer_id", "").strip()
        try:
            ok = svc.link_offer(listing, offer_id)
        except Exception as exc:
            messages.error(request, f"DigiKey: {exc}")
        else:
            if ok:
                messages.success(request, f"🔗 {listing.get_supplier_sku()}: записано ID офера {offer_id}.")
            else:
                messages.warning(request, f"Офер {offer_id} не знайдено на DigiKey — нічого не змінено.")
        return redirect(reverse("admin:bots_digikeypricerun_offers"))

    # ── Звіт оновлення ──

    def change_view(self, request, object_id, form_url="", extra_context=None):
        if not _can_view(request):
            raise PermissionDenied
        run = get_object_or_404(DigiKeyPriceRun, pk=object_id)
        lines = list(run.lines.select_related("listing__product"))
        rows = []
        for line in lines:
            cells = _cells(svc.tiers_of(line.old_prices), svc.tiers_of(line.new_prices))
            rows.append({
                "line": line, "new": cells["new"], "old": cells["dk"],
                "minerva": svc.fmt_tiers(svc.tiers_of(line.minerva_prices), line.minerva_currency),
                "change": "" if line.change_pct is None else f"{svc.fmt_signed(line.change_pct)} %",
                "color": STATUS_COLORS.get(line.status, "inherit"),
                "listing_url": reverse("admin:bots_digikeylisting_change", args=[line.listing_id]) if line.listing_id else "",
            })
        try:
            rules_text = svc.Rules.from_dict(run.rules or {}).describe(run.currency)
        except ValueError:
            rules_text = ""
        context = {
            **self.admin_site.each_context(request),
            "title": f"💲 Оновлення цін DigiKey #{run.pk}", "opts": self.model._meta, "run": run, "rows": rows,
            "rules_text": rules_text, "color": STATUS_COLORS.get(run.status, "inherit"),
            "live": run.status in (DigiKeyPriceRun.QUEUED, DigiKeyPriceRun.SENDING, DigiKeyPriceRun.SENT),
            "can_change": _can_change(request),
            "any_sent": any(l.status == DigiKeyPriceChange.SENT for l in lines),
            "any_held": any(l.status == DigiKeyPriceChange.HELD for l in lines),
            "compare_url": _compare_url(), "history_url": reverse("admin:bots_digikeypricerun_changelist"),
            "verify_after": svc.VERIFY_FIRST_SECONDS, "verify_hours": svc.VERIFY_TIMEOUT_HOURS,
        }
        return TemplateResponse(request, "admin/bots/digikeypricerun/run.html", context)

    def status_view(self, request, pk):
        if not _can_view(request):
            raise PermissionDenied
        run = get_object_or_404(DigiKeyPriceRun, pk=pk)
        return JsonResponse({"status": run.status, "status_display": run.get_status_display(),
                             "progress": run.progress, "total": run.total, "sent": run.sent,
                             "verified": run.verified, "held": run.held, "errors": run.errors})

    def verify_view(self, request, pk):
        if request.method != "POST" or not _can_view(request):
            raise PermissionDenied
        run = get_object_or_404(DigiKeyPriceRun, pk=pk)
        try:
            res = svc.verify(run, force=True)
        except Exception as exc:
            messages.error(request, f"Не вдалося прочитати офери з DigiKey: {exc}")
        else:
            if not res["checked"]:
                messages.info(request, "Немає позицій, що чекають перевірки.")
            else:
                left = res["checked"] - res["verified"] - res["mismatch"]
                messages.success(request, f"🔍 Перевірено на DigiKey: діє {res['verified']}"
                                          + (f", ще не видно {left}" if left else "")
                                          + (f", інші ціни {res['mismatch']}" if res["mismatch"] else "") + ".")
        return redirect(reverse("admin:bots_digikeypricerun_change", args=[pk]))

    def decide_view(self, request, pk):
        if request.method != "POST" or not _can_change(request):
            raise PermissionDenied
        run = get_object_or_404(DigiKeyPriceRun, pk=pk)
        ids = [int(i) for i in request.POST.getlist("ids") if str(i).isdigit()]
        if not ids:
            messages.warning(request, "Не вибрано жодної позиції.")
        elif request.POST.get("decision") == "reject":
            n = svc.reject(run, ids, request.user)
            messages.info(request, f"✕ Відхилено: {n}. Ці самі ціни Minerva більше не пропонуватиме.")
        else:
            n = svc.confirm(run, ids, request.user)
            messages.success(request, f"📤 Надсилається на DigiKey: {n}." if n else
                             "Різниці вже немає — нічого не надіслано.")
        return redirect(reverse("admin:bots_digikeypricerun_change", args=[pk]))


# ── Картка товару (inventory/admin.py → ProductAdmin) ────────────────────────

def product_panel(product) -> str:
    """Блок «💲 DigiKey» картки товару: зараз на DigiKey → стане з цін Minerva, «оновити зараз»."""
    listing = DigiKeyListing.objects.filter(product=product).select_related("product").first() if product.pk else None
    if listing is None:
        if not product.pk:
            return format_html('<span style="color:var(--text-muted)">Товару немає на DigiKey (лістинг не створено).</span>')
        from bots.admin_from_stock import product_link
        return format_html('<span style="color:var(--text-muted)">Товару немає на DigiKey (лістинг не створено).</span>'
                           ' <a class="button" href="{}">🔷 Створити картку DigiKey зі складу</a>', product_link(product))
    s = DigiKeyPriceSettings.get()
    cur = svc.currency(s)
    row = svc.row_for(listing, svc.Rules.from_settings(s), cur)
    compare = _compare_url(f"products={product.pk}")
    checked = (f" · прочитано {timezone.localtime(listing.price_checked_at):%d.%m %H:%M}"
               if listing.price_checked_at else " · ще не читались")
    lines = [format_html('Зараз на DigiKey: <b>{}</b><small style="color:var(--text-muted)">{}</small>',
                         svc.fmt_tiers(row.dk, cur), checked)]
    if row.state != "ok":
        lines.append(format_html('<span style="color:var(--warn);font-weight:600">{}</span>', row.state_text))
    elif row.differs:
        change = ""
        if row.change_pct is not None:
            bigger = row.max_change_pct is not None and row.max_change_pct > abs(row.change_pct)
            change = (f" ({svc.fmt_signed(row.change_pct)} %"
                      + (f", до ±{svc.fmt_num(row.max_change_pct)} % для {row.max_change_qty} шт" if bigger else "") + ")")
        lines.append(format_html('З цін Minerva стане: <b>{}</b>{}', svc.fmt_tiers(row.new, cur), change))
        lines += [format_html('<small style="color:var(--text-muted)">{}</small>', n) for n in row.notes]
    else:
        lines.append(format_html('<span style="color:var(--ok);font-weight:600">✓ На DigiKey ціни як у Minerva.</span>'))
    last = listing.price_changes.select_related("run").order_by("-run__created_at").first()
    if last:
        lines.append(format_html('<small>Останнє оновлення <a href="{}">#{}</a>: {} · {}</small>',
                                 reverse("admin:bots_digikeypricerun_change", args=[last.run_id]), last.run_id,
                                 last.get_status_display(), timezone.localtime(last.run.created_at).strftime("%d.%m %H:%M")))
    buttons = [format_html('<a class="button" href="{}">🔄 Звірити з DigiKey (свіжі ціни)</a>', compare)]
    if row.differs:
        buttons.append(format_html(
            '<button type="button" class="button" data-dkp-push="{}" data-dkp-id="{}" data-dkp-text="{}">'
            '✓ Оновити на DigiKey зараз</button>',
            reverse("admin:bots_digikeypricerun_apply"), listing.pk,
            f"Оновити ціни {row.sku} на DigiKey: {svc.fmt_tiers(row.new, cur)}?\n"
            "Беруться вже збережені ціни; незбережені зміни форми — через галочку нижче й «Зберегти»."))
    after = ""
    if row.state not in ("staged", "no_offer"):
        after = format_html(
            '<label style="display:flex;gap:6px;align-items:center;margin-top:8px;width:auto;float:none;padding:0;'
            'min-width:0;font-weight:400;color:var(--text)">'
            '<input type="checkbox" name="dk_push_after_save" value="1"> Після «Зберегти» — одразу оновити ціну на '
            'DigiKey (з новими базовими цінами, правила — «💲 Налаштування цін DigiKey»)</label>')
    script = format_html(
        "<script>document.querySelectorAll('[data-dkp-push]').forEach(function (b) {{"
        "b.addEventListener('click', function () {{ if (!confirm(b.dataset.dkpText)) return;"
        "var f = document.createElement('form'); f.method = 'post'; f.action = b.dataset.dkpPush;"
        "[['csrfmiddlewaretoken', document.querySelector('[name=csrfmiddlewaretoken]').value],"
        "['ids', b.dataset.dkpId], ['query', 'products={}']].forEach(function (kv) {{"
        "var i = document.createElement('input'); i.type = 'hidden'; i.name = kv[0]; i.value = kv[1]; f.appendChild(i); }});"
        "document.body.appendChild(f); f.submit(); }}); }});</script>", product.pk)
    return format_html('<div style="display:flex;flex-direction:column;gap:4px">{}'
                       '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:6px">{}</div>{}</div>{}',
                       format_html_join("", "<div>{}</div>", ((l,) for l in lines)),
                       format_html_join("", "{}", ((b,) for b in buttons)), after, script)


def apply_after_save(request, product):
    """Галочка «Після «Зберегти» — одразу на DigiKey» у картці товару (ProductAdmin.save_model)."""
    if request.POST.get("dk_push_after_save") != "1":
        return
    if not _can_change(request):
        messages.warning(request, "DigiKey: немає права змінювати лістинги — ціни не надіслано.")
        return
    listing = DigiKeyListing.objects.filter(product=product).select_related("product").first()
    if listing is None:
        return
    s = DigiKeyPriceSettings.get()
    cur = svc.currency(s)
    row = svc.row_for(listing, svc.Rules.from_settings(s), cur)
    if not row.differs:
        messages.info(request, f"DigiKey: {row.state_text or 'ціни вже як у Minerva'} — нічого не надіслано.")
        return
    run = svc.create_run([row], request.user)
    svc.start(run)
    messages.success(request, format_html(
        '💲 DigiKey: {} → <b>{}</b> надсилається — <a href="{}">оновлення #{}</a> (там же перевірка, що ціни діють).',
        row.sku, svc.fmt_tiers(row.new, cur), reverse("admin:bots_digikeypricerun_change", args=[run.pk]), run.pk))
