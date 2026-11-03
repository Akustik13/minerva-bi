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


def _hints(cur: str, home: str) -> list[dict]:
    """Інші курси для порівняння (клік — підставити): «Курси валют» Minerva й онлайн ЄЦБ."""
    out = []
    try:
        from offers.pricing import fx
        r = fx(cur, home)
        if r:
            out.append({"origin": "«Курси валют» Minerva", "rate": bp.rate_text(r)})
    except Exception:
        pass
    try:
        from core import fx_online
        data = fx_online.latest()
        r = fx_online.cross(data, cur, home) if data else None
        if r:
            y, m, d = str(data["date"])[:10].split("-")
            out.append({"origin": f"ЄЦБ на {d}.{m}.{y}", "rate": bp.rate_text(r)})
    except Exception:
        pass
    return out


def _origin(raw: str, rate, default_rate, home: str) -> str:
    """Звідки курс — для історії: підказка, яку клікнули, або «як для DigiKey», або «вписано вручну»."""
    raw = re.sub(r"[\x00-\x1f<>]", "", (raw or "").strip())[:60]
    if raw:
        return raw
    if default_rate and rate == default_rate:
        return f"як для DigiKey: {_dk_rate_source()}"[:60]
    return "вписано вручну"


def _shift(rate, default) -> str:
    """На скільки зміняться ціни на DigiKey, якщо перевести не за курсом DigiKey («+3,5 %»); той самий — ''."""
    if not rate or not default:
        return ""
    pct = (Decimal(str(rate)) / Decimal(str(default)) - 1) * 100
    if abs(pct) < Decimal("0.1"):
        return ""
    return ("+" if pct > 0 else "−") + format(abs(pct).quantize(Decimal("0.1")), "f").replace(".", ",") + " %"


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
    rates, defaults, same = {}, {}, {}
    for cur in groups:
        defaults[cur] = bp.rate_to_home(cur, home)
        # галочка «однаковий курс з DigiKey» (за замовчуванням): курс — лише той, що для DigiKey
        if request.method == "POST":
            same[cur] = bool(defaults[cur]) and bool(request.POST.get(f"same_{cur}"))
            posted = None if same[cur] else _rate(request.POST.get(f"rate_{cur}"))
        else:
            posted = _rate(request.GET.get(f"rate_{cur}"))
            same[cur] = bool(defaults[cur]) and not posted
        rates[cur] = posted or defaults[cur]
    if request.method == "POST" and request.POST.get("action") != "preview":
        if not request.POST.get("confirm"):
            messages.warning(request, "Поставте галочку «Так, перевести» — нічого не змінено.")
        else:
            plan, skipped = [], []
            note = (request.POST.get("note") or "").strip()[:120]
            for cur, items in groups.items():
                if not rates.get(cur):
                    skipped += [p.sku for p in items]
                    continue
                origin = (f"як для DigiKey: {_dk_rate_source()}"[:60] if same[cur]
                          else _origin(request.POST.get(f"origin_{cur}"), rates[cur], defaults[cur], home))
                plan += [(p.pk, rates[cur], origin) for p in items]
            if skipped:
                messages.warning(request, f"Без курсу — не переведено ({len(skipped)}): " + ", ".join(skipped[:20]))
            if not plan:
                messages.warning(request, "Нічого не переведено.")
            elif bp.start_convert(plan, home, user=request.user, note=note, cents=cents):
                messages.info(request, f"Переводжу базові ціни {len(plan)} товарів у {home} — хід видно вгорі. "
                                       "Сторінку можна закрити: переведення не зупиниться.")
            else:
                messages.warning(request, "Уже йде інше переведення — дочекайтеся, поки воно закінчиться.")
            return redirect(request.get_full_path())
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
    job = bp.convert_state()
    if job:
        job["pct"] = int(100 * job.get("done", 0) / job["total"]) if job.get("total") else 100
        try:
            from datetime import datetime

            from django.utils import timezone
            job["when"] = timezone.localtime(datetime.fromisoformat(job["started"])).strftime("%d.%m.%Y %H:%M")
        except (KeyError, TypeError, ValueError):
            job["when"] = ""
    return render(request, "admin/inventory/product/base_currency.html", {
        **model_admin.admin_site.each_context(request),
        "title": f"💱 Базові ціни → {home}", "opts": Product._meta, "home": home,
        "groups": [{"cur": cur, "count": len(items), "rate": bp.rate_text(rates[cur]) if rates.get(cur) else "",
                    "inverse": bp.rate_text(1 / rates[cur]) if rates.get(cur) else "",
                    "origin": "" if same[cur] else request.POST.get(f"origin_{cur}", "")[:60],
                    "same": same[cur], "default": bp.rate_text(defaults[cur]) if defaults[cur] else "",
                    "shift": "" if same[cur] else _shift(rates[cur], defaults[cur]),
                    "hints": _hints(cur, home)} for cur, items in sorted(groups.items())],
        "rows": rows, "total": len(products), "more": max(0, len(products) - PREVIEW_ROWS),
        "ids": ids, "dk_rate_source": _dk_rate_source(), "cents": cents,
        "back_url": reverse("admin:inventory_product_changelist"),
        "job": job, "job_status_url": reverse("admin:inventory_product_base_currency_status"),
        "home_list_url": reverse("admin:inventory_product_changelist") + f"?base_cur={home}",
    })


def currency_status_view(model_admin, request):
    """Хід переведення для смуги на сторінці (JSON)."""
    if not model_admin.has_change_permission(request):
        raise PermissionDenied
    from django.http import JsonResponse
    st = bp.convert_state()
    return JsonResponse({k: st.get(k) for k in ("running", "total", "done", "converted", "stalled", "finished",
                                                "error")} | {"skipped": len(st.get("skipped") or []),
                                                             "errors": len(st.get("errors") or [])})


def card_config(product_id=None) -> dict:
    """Для редактора базових цін у картці товару: валюта Minerva, курс для DigiKey (EUR → USD), курси «до валюти
    Minerva» тим самим правилом, що для DigiKey (кнопка «Конвертувати» й «≈»), адреса переведення товару,
    останнє переведення (`last_fx`: картка показує, якщо переведено не за курсом DigiKey)."""
    home = bp.home_currency()
    out = {"minerva": home, "to_minerva": {}, "dk": None, "convert_url": page_url([product_id]) if product_id else "",
           "fx_online_url": reverse("core_fx_online")}
    product = Product.objects.filter(pk=product_id).first() if product_id else None
    current = product.base_price_currency if product else None
    last = bp.last_conversion(product) if product else None
    if last:
        out["last_fx"] = {"pk": last["pk"], "from": last["from"], "rate": float(last["rate"]), "origin": last["origin"],
                          "prices": [[r["min_qty"], r["unit_price"]] for r in last["rows"]]}
    try:
        from bots.services import dk_prices
        s = dk_prices.settings()
        dk = dk_prices.currency(s)
        if dk != home:
            r = dk_prices.fx(home, dk, s)
            out["dk"] = {"currency": dk, "rate": float(r) if r else None, "source": dk_prices.rate_source_text(s),
                         "online": s.fx_source == s.FX_ONLINE,
                         "settings_url": reverse("admin:bots_digikeypricesettings_change", args=[s.pk])
                         + "#fieldset-0-2"}
        for cur in {dk, (current or home).upper(), last["from"] if last else home} - {home}:
            r = bp.rate_to_home(cur, home)
            if r:
                out["to_minerva"][cur] = float(r)
    except Exception:  # noqa: BLE001 — без DigiKey картка працює з «Курсами валют»
        pass
    return out


def _dk_rate_source() -> str:
    try:
        from bots.services.dk_prices import rate_source_text
        return rate_source_text()
    except Exception:
        return "«Курси валют» Minerva"
