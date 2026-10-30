"""
bots/admin_dk_tools.py — сторінки DigiKey для лістингів:

«🔍 Що бачить DigiKey» (`…/digikeylisting/dk-diff/`): `?pk=` — один лістинг наживо (офер + товар, кожне поле
«Minerva → DigiKey»), `?ids=` / `?all=1` — офери багатьох; «📤 Надіслати офер з Minerva» (POST `dk-diff/push/<pk>/`
→ `dk_marketplace.update_offer`). Логіка порівняння — bots/services/dk_view.py.

«⏱ Строки відвантаження» (`…/digikeylisting/lead-times/`): список лістингів зі строком Minerva і (за кнопкою)
DigiKey, вибрати → новий строк, «одразу на DigiKey» — лише строк (bots/services/dk_lead.py), хід — `status/`.
"""
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse

from bots.models import DigiKeyListing
from bots.services import dk_lead, dk_view

LIST_LIMIT = 300


def _ids(request) -> list:
    raw = ",".join(request.POST.getlist("ids") or request.GET.getlist("ids"))
    return [int(x) for x in raw.replace(" ", "").split(",") if x.isdigit()]


def _ctx(model_admin, request, **kw):
    return {**model_admin.admin_site.each_context(request), "opts": DigiKeyListing._meta, **kw}


def diff_view(request, model_admin):
    if not model_admin.has_view_permission(request):
        raise PermissionDenied
    can_change = model_admin.has_change_permission(request)
    pk = request.GET.get("pk")
    if pk:
        listing = get_object_or_404(DigiKeyListing.objects.select_related("product"), pk=pk)
        return TemplateResponse(request, "admin/bots/digikeylisting/dk_view.html", _ctx(
            model_admin, request, title=f"Що бачить DigiKey: {listing.get_supplier_sku()}", v=dk_view.live(listing),
            can_change=can_change, back=reverse("admin:bots_digikeylisting_change", args=[listing.pk])))
    ids, everything = _ids(request), request.GET.get("all") == "1"
    rows, error = [], ""
    if ids or everything:
        qs = DigiKeyListing.objects.select_related("product").order_by("product__sku")
        qs = qs.filter(pk__in=ids) if ids else qs.exclude(dk_offer_id="")
        try:
            rows = dk_view.bulk(qs[:LIST_LIMIT])
        except Exception as exc:  # noqa: BLE001 — мережа / токен
            error = f"Не вдалося прочитати офери з DigiKey: {exc}"
    if request.GET.get("only") == "diff":
        rows = [r for r in rows if r["sum"]["diff"] or r["sum"]["missing"]]
    return TemplateResponse(request, "admin/bots/digikeylisting/dk_view.html", _ctx(
        model_admin, request, title="Що бачить DigiKey — звірка оферів", rows=rows, error=error, bulk=True,
        asked=bool(ids or everything), ids=",".join(map(str, ids)), can_change=can_change,
        differ=sum(1 for r in rows if r["sum"]["diff"] or r["sum"]["missing"]), limit=LIST_LIMIT))


def push_view(request, model_admin, pk):
    """Оновити на DigiKey: what=fields — лише вибрані поля офера (fields), what=product — товар (на перевірку
    DigiKey), інакше — увесь офер з даними лістингу (ціни, залишок, MOQ, строк, назва)."""
    if request.method != "POST" or not model_admin.has_change_permission(request):
        raise PermissionDenied
    from bots.models import DigiKeyConfig
    from bots.services.dk_marketplace import update_offer
    listing = get_object_or_404(DigiKeyListing.objects.select_related("product"), pk=pk)
    back = redirect(reverse("admin:bots_digikeylisting_dk_diff") + f"?pk={listing.pk}")
    what = request.POST.get("what", "")
    if what == "fields":
        try:
            done = dk_view.push_offer_fields(listing, request.POST.getlist("fields"))
            messages.success(request, f"{listing.get_supplier_sku()}: на DigiKey оновлено — "
                                      f"{', '.join(dk_view.PUSHABLE[f] for f in done)}. Нижче те, що DigiKey повертає тепер.")
        except Exception as exc:  # noqa: BLE001
            messages.error(request, f"{listing.get_supplier_sku()}: не оновлено — {exc}")
        return back
    if what == "product":
        try:
            dk_view.push_product(listing)
            messages.success(request, f"{listing.get_supplier_sku()}: товар надіслано — DigiKey перевірить зміни "
                                      "(на supplier.digikey.com: Products → Pending); до того діє затверджена версія.")
        except Exception as exc:  # noqa: BLE001
            messages.error(request, f"{listing.get_supplier_sku()}: DigiKey не прийняв товар — {exc}")
        return back
    if not listing.dk_offer_id:
        messages.error(request, "У лістингу немає ID офера — спершу «📦 Створити Offer» або «🔗 Звірити офери».")
    else:
        try:
            update_offer(DigiKeyConfig.get(), listing)
            messages.success(request, f"{listing.get_supplier_sku()}: офер надіслано на DigiKey — нижче те, що "
                                      "DigiKey повертає тепер.")
        except Exception as exc:  # noqa: BLE001
            messages.error(request, f"{listing.get_supplier_sku()}: DigiKey не прийняв — {exc}")
    return redirect(reverse("admin:bots_digikeylisting_dk_diff") + f"?pk={listing.pk}")


def lead_view(request, model_admin):
    if not model_admin.has_view_permission(request):
        raise PermissionDenied
    can_change = model_admin.has_change_permission(request)
    if request.method == "POST":
        if not can_change:
            raise PermissionDenied
        chosen = [int(x) for x in request.POST.getlist("pick") if x.isdigit()]
        try:
            days = dk_lead.parse_days(request.POST.get("days"))
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            if not chosen:
                messages.warning(request, "Позначте лістинги галочкою.")
            else:
                n = dk_lead.set_days(chosen, days)
                messages.success(request, f"Строк {days} дн — у Minerva для {len(chosen)} лістингів (змінено {n}).")
                if request.POST.get("push") == "1":
                    who = request.user.get_username()
                    if dk_lead.start_push(chosen, who):
                        messages.info(request, "Надсилаю строки на DigiKey — хід нижче (ціни й залишок не змінюються).")
                    else:
                        messages.warning(request, "На DigiKey уже надсилаються строки — дочекайтеся кінця й повторіть.")
        return redirect(request.get_full_path())
    qs = DigiKeyListing.objects.select_related("product").order_by("product__sku")
    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(product__sku__icontains=q) | Q(dk_title__icontains=q) | Q(dk_supplier_sku__icontains=q))
    cat = request.GET.get("cat") or ""
    if cat:
        qs = qs.filter(category_type=cat)
    if request.GET.get("offer") == "1":
        qs = qs.exclude(dk_offer_id="")
    ids = _ids(request)
    if ids:
        qs = qs.filter(pk__in=ids)
    listings = list(qs[:LIST_LIMIT])
    offers, error = None, ""
    if request.GET.get("live") == "1":
        try:
            offers = dk_lead.live_map(listings)
        except Exception as exc:  # noqa: BLE001
            error = f"Не вдалося прочитати офери з DigiKey: {exc}"
    rows = dk_lead.rows(listings, offers)
    return TemplateResponse(request, "admin/bots/digikeylisting/lead_times.html", _ctx(
        model_admin, request, title="Строки відвантаження DigiKey", rows=rows, q=q, cat=cat, error=error,
        offer_only=request.GET.get("offer") == "1", live=offers is not None, ids=",".join(map(str, ids)),
        cats=DigiKeyListing.CAT_CHOICES, can_change=can_change, state=dk_lead.state(), limit=LIST_LIMIT,
        total=qs.count(), differ=sum(1 for r in rows if r["differs"])))


def lead_status_view(request, model_admin):
    if not model_admin.has_view_permission(request):
        raise PermissionDenied
    return JsonResponse(dk_lead.state())
