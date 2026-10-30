"""💱 Базові ціни → валюта Minerva: сторінка переведення вибраних (або всіх) товарів, фільтр «Валюта базових цін».

Логіка — `inventory/services/base_prices.py` (`convert`, `not_home`, `rate_to_home`). Курс за замовчуванням — той,
яким ціни Minerva стають цінами DigiKey, тож ціни на DigiKey після переведення не зрушать; його можна змінити.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.urls import reverse

from .models import Product
from .services import base_prices as bp

PREVIEW_ROWS = 40


class BaseCurrencyFilter(admin.SimpleListFilter):
    """Валюта базових цін: усі не у валюті Minerva / кожна валюта / без базових цін."""
    title = "Валюта базових цін"
    parameter_name = "base_cur"

    def lookups(self, request, model_admin):
        home = bp.home_currency()
        used = sorted({(c or "").upper() for c in Product.objects.values_list("base_price_currency", flat=True)
                       .distinct()} - {"", home})
        return ([("not_home", f"Не в {home} — перевести"), (home, home)]
                + [(c, c) for c in used] + [("none", "Без базових цін")])

    def queryset(self, request, qs):
        v = self.value()
        if not v or not (v in ("none", "not_home") or re.fullmatch(r"[A-Za-z]{3}", v)):
            return qs
        home = bp.home_currency()
        keep = []
        # «є базові ціни» — JSON, тож перевіряємо в Python (values_list — без select_related списку)
        for pk, prices, cur in qs.values_list("pk", "base_prices", "base_price_currency"):
            has, cur = bool(bp.normalize(prices)), (cur or "").upper()
            if (v == "none" and not has) or (v == "not_home" and has and cur != home)                     or (has and cur == v.upper()):
                keep.append(pk)
        return qs.filter(pk__in=keep)


def page_url(ids=None) -> str:
    url = reverse("admin:inventory_product_base_currency")
    return url + ("?ids=" + ",".join(str(i) for i in ids) if ids else "?all=1")


def _rate(raw) -> Decimal | None:
    try:
        v = Decimal(str(raw or "").strip().replace(",", "."))
    except InvalidOperation:
        return None
    return v if v > 0 else None


def _products(request):
    ids = [int(x) for x in re.findall(r"\d+", request.GET.get("ids") or request.POST.get("ids") or "")][:5000]
    qs = Product.objects.filter(pk__in=ids) if ids else Product.objects.all()
    return bp.not_home(qs)


def _hints(cur: str, home: str) -> list[str]:
    """Інші курси для порівняння: «Курси валют» Minerva й онлайн ЄЦБ."""
    out = []
    try:
        from offers.pricing import fx
        r = fx(cur, home)
        if r:
            out.append(f"«Курси валют» Minerva: {bp.rate_text(r)}")
    except Exception:
        pass
    try:
        from core import fx_online
        data = fx_online.latest()
        r = fx_online.cross(data, cur, home) if data else None
        if r:
            y, m, d = str(data["date"])[:10].split("-")
            out.append(f"ЄЦБ на {d}.{m}.{y}: {bp.rate_text(r)}")
    except Exception:
        pass
    return out


def currency_view(model_admin, request):
    """GET ?ids= / ?all=1 — що й за яким курсом стане у валюті Minerva; POST — перевести з історією."""
    if not model_admin.has_change_permission(request):
        raise PermissionDenied
    home = bp.home_currency()
    products = _products(request)
    groups = {}
    for p in products:
        groups.setdefault((p.base_price_currency or "").upper(), []).append(p)
    cents = request.method == "POST" and bool(request.POST.get("cents"))
    rates = {}
    for cur in groups:
        posted = _rate(request.POST.get(f"rate_{cur}")) if request.method == "POST" else None
        rates[cur] = posted or _rate(request.GET.get(f"rate_{cur}")) or bp.rate_to_home(cur, home)
    if request.method == "POST" and request.POST.get("action") != "preview":
        if not request.POST.get("confirm"):
            messages.warning(request, "Поставте галочку «Так, перевести» — нічого не змінено.")
        else:
            done, skipped = 0, []
            note = (request.POST.get("note") or "").strip()[:120]
            for cur, items in groups.items():
                if not rates.get(cur):
                    skipped += [p.sku for p in items]
                    continue
                for p in items:
                    if bp.convert(p, home, rates[cur], user=request.user, note=note, cents=cents):
                        done += 1
            messages.success(request, f"Базові ціни переведено в {home}: {done} товарів (записано в історію)."
                             if done else "Нічого не переведено.")
            if skipped:
                messages.warning(request, f"Без курсу — не переведено ({len(skipped)}): " + ", ".join(skipped[:20]))
            return redirect(reverse("admin:inventory_product_changelist") + f"?base_cur={home}")
    rows = []
    for p in products[:PREVIEW_ROWS]:
        cur = (p.base_price_currency or "").upper()
        tiers = bp.normalize(p.base_prices)
        rate = rates.get(cur)
        rows.append({
            "pk": p.pk, "sku": p.sku, "name": (p.name or "")[:70], "cur": cur, "tiers": len(tiers),
            "url": reverse("admin:inventory_product_change", args=[p.pk]),
            "was": tiers[0]["unit_price"],
            "will": format(bp.converted(tiers[0]["unit_price"], rate, cents), "f") if rate else None,
        })
    ids = request.GET.get("ids") or request.POST.get("ids") or ""
    return render(request, "admin/inventory/product/base_currency.html", {
        **model_admin.admin_site.each_context(request),
        "title": f"💱 Базові ціни → {home}", "opts": Product._meta, "home": home,
        "groups": [{"cur": cur, "count": len(items), "rate": bp.rate_text(rates[cur]) if rates.get(cur) else "",
                    "hints": _hints(cur, home)} for cur, items in sorted(groups.items())],
        "rows": rows, "total": len(products), "more": max(0, len(products) - PREVIEW_ROWS),
        "ids": ids, "dk_rate_source": _dk_rate_source(), "cents": cents,
        "back_url": reverse("admin:inventory_product_changelist"),
    })


def _dk_rate_source() -> str:
    try:
        from bots.services.dk_prices import rate_source_text
        return rate_source_text()
    except Exception:
        return "«Курси валют» Minerva"
