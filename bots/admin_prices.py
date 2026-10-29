"""bots/admin_prices.py — «💲 Ціни DigiKey ← склад Minerva»: порівняння, оновлення, історія, налаштування.

Сторінки (логіка — bots/services/dk_prices.py):
  /admin/bots/digikeypricerun/compare/        порівняння «DigiKey зараз → стане з цін Minerva», вибір, оновлення
  /admin/bots/digikeypricerun/<pk>/change/    звіт оновлення: було → стало, стан на DigiKey, підтвердження
  /admin/bots/digikeypricesettings/           правила за замовчуванням, автооновлення, сповіщення
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

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


# ── Відображення рядків ───────────────────────────────────────────────────────

def _tier_cells(row):
    """Для шаблону: ступені «зараз», «Minerva», «стане» з % для кожної кількості."""
    old = dict(row.dk)
    new = []
    for q, p in row.new or []:
        was = old.get(q)
        pct = None if not was else (p / was - 1) * 100
        cls = "same" if was == p else ("up" if was is not None and p > was else
                                       "down" if was is not None else "added")
        new.append({"qty": q, "price": svc.fmt_price(p),
                    "pct": "" if pct is None or was == p else f"{svc.fmt_signed(pct)} %", "cls": cls})
    gone = [q for q in old if row.new is not None and q not in dict(row.new)]
    return {
        "dk": [{"qty": q, "price": svc.fmt_price(p), "gone": q in gone} for q, p in row.dk],
        "minerva": [{"qty": q, "price": svc.fmt_price(p)} for q, p in row.minerva],
        "new": new,
    }


def _row_view(row, cur):
    kind = "diff" if row.differs else ("same" if row.state == "ok" else "problem")
    listing = row.listing
    return {
        "row": row, "kind": kind, "dir": row.direction, "cells": _tier_cells(row),
        "pk": listing.pk,
        "product_url": reverse("admin:inventory_product_change", args=[listing.product_id]) if listing.product_id else "",
        "listing_url": reverse("admin:bots_digikeylisting_change", args=[listing.pk]),
        "change": "" if row.change_pct is None else f"{svc.fmt_signed(row.change_pct)} %",
        "max_change": "" if row.max_change_pct is None else f"{svc.fmt_num(row.max_change_pct)} %",
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
        rows = svc.plan(rules=rules, s=s)
        views = [_row_view(r, cur) for r in rows]
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
            "query": request.GET.urlencode(),
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

    # ── Звіт оновлення ──

    def change_view(self, request, object_id, form_url="", extra_context=None):
        if not _can_view(request):
            raise PermissionDenied
        run = get_object_or_404(DigiKeyPriceRun, pk=object_id)
        lines = list(run.lines.select_related("listing__product"))
        rows = []
        for line in lines:
            old = dict(svc.tiers_of(line.old_prices))
            new = []
            for q, p in svc.tiers_of(line.new_prices):
                was = old.get(q)
                new.append({"qty": q, "price": svc.fmt_price(p),
                            "pct": f"{svc.fmt_signed((p / was - 1) * 100)} %" if was and was != p else "",
                            "cls": "same" if was == p else ("up" if was and p > was else "down" if was else "added")})
            rows.append({
                "line": line, "new": new,
                "old": [{"qty": q, "price": svc.fmt_price(p)} for q, p in svc.tiers_of(line.old_prices)],
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
