"""
bots/admin_from_stock.py — «🔷 На DigiKey зі складу» (`/admin/bots/digikeylisting/from-stock/`).

Без ?ids — вибір товарів складу без лістингу (пошук). З ?ids — для кожного товару чернетка картки
(`dk_from_stock.build_draft`): що Minerva заповнила сама й звідки, чого бракує — поля для вводу (обов'язкові DigiKey
першими, далі параметри як у наших схожих лістингів). POST: `refresh_fields` — запитати поля в DigiKey,
`preview` — перерахувати з введеним (інша категорія → інший набір полів), `create` — створити лістинги
(і за галочкою одразу надіслати на DigiKey — `dk_marketplace.publish_listing`).
Логіка — лише bots/services/dk_from_stock.py.
"""
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import reverse

from bots.models import DigiKeyListing
from bots.services import dk_from_stock as svc
from bots.services import dk_publish

PICK_LIMIT = 60
MAX_PRODUCTS = 30
ID_FIELDS = (("dk_title", "title"), ("dk_description", "desc"), ("dk_manufacturer", "mfr"),
             ("dk_category_id", "cat"), ("dk_prices", "prices"))


def _ids(request) -> list:
    raw = ",".join(request.POST.getlist("ids") or request.GET.getlist("ids"))
    return list(dict.fromkeys(int(x) for x in raw.replace(" ", "").split(",") if x.isdigit()))[:MAX_PRODUCTS]


def _inputs(request, pk) -> tuple:
    """(given {code: value}, overrides) з форми сторінки для товару pk."""
    prefix = f"p{pk}-"
    given = {k[len(prefix) + 2:]: v.strip() for k, v in request.POST.items()
             if k.startswith(prefix + "f-") and v.strip()}
    overrides = {field: request.POST.get(prefix + short, "").strip() for field, short in ID_FIELDS}
    return given, overrides


def _picker(request, model_admin):
    from inventory.models import Product
    q = (request.GET.get("q") or "").strip()
    qs = Product.objects.filter(dk_listing__isnull=True, is_active=True)
    if q:
        qs = qs.filter(Q(sku__icontains=q) | Q(name__icontains=q) | Q(name_export__icontains=q))
    total = qs.count()
    return TemplateResponse(request, "admin/bots/digikeylisting/from_stock.html", {
        **model_admin.admin_site.each_context(request), "opts": DigiKeyListing._meta,
        "title": "🔷 На DigiKey зі складу", "picker": True, "q": q, "total": total,
        "products": list(qs.order_by("sku")[:PICK_LIMIT]), "limit": PICK_LIMIT,
    })


def _card(product, given, overrides):
    draft = svc.build_draft(product, given, overrides)
    required = [m for m in draft["missing"] if m["required"]]
    optional = [m for m in draft["missing"] if not m["required"]]
    common = {"packaging": draft["fields"]["dk_packaging"],
              "productLifecycleStatus": draft["fields"]["dk_lifecycle_status"]}
    filled = [{**r, "value": draft["attrs"].get(r["code"]) or common.get(r["code"], ""),
               "source": draft["sources"][r["code"]], "given": r["code"] in given}
              for r in draft["rows"] if r["code"] in draft["sources"]]
    f = draft["fields"]
    todo = ([f"{len(required)} пол." if len(required) > 1 else required[0]["name"]] if required else []) + \
        (["категорія"] if not f["dk_category_id"] else []) + (["ціни"] if not f["dk_prices"] else [])
    listed_ids = {o["id"] for o in svc.category_choices()["ours"]}
    return {"product": product, "draft": draft, "fields": f, "required": required, "optional": optional,
            "filled": filled, "given": given, "ready": not todo, "todo": todo,
            "prices_text": overrides.get("dk_prices", ""), "cat_listed": f["dk_category_id"] in listed_ids}


def view(request, model_admin):
    if not model_admin.has_view_permission(request):
        raise PermissionDenied
    can_add = model_admin.has_add_permission(request)
    action = request.POST.get("action", "") if request.method == "POST" else ""
    if action and not can_add:
        raise PermissionDenied
    ids = _ids(request)
    if not ids:
        return _picker(request, model_admin)
    if action == "refresh_fields":
        try:
            defs = svc.field_defs(refresh=True)
            messages.success(request, f"DigiKey повернув {len(defs)} полів товару "
                                      f"({sum(1 for d in defs.values() if d['required'])} обов'язкових).")
        except Exception as exc:  # noqa: BLE001
            messages.error(request, f"Не вдалося отримати поля з DigiKey: {exc}")
        try:
            cats = svc.dk_categories(refresh=True)
            messages.success(request, f"Каталог DigiKey: категорій {len(cats)} "
                                      f"(для товару — {sum(1 for c in cats if c['leaf'])}).")
        except Exception as exc:  # noqa: BLE001
            messages.error(request, f"Не вдалося отримати категорії з каталогу DigiKey (ProductSearch): {exc}")
        return redirect(f"{request.path}?ids={','.join(map(str, ids))}")

    from inventory.models import Product
    products = {p.pk: p for p in Product.objects.filter(pk__in=ids)}
    if action == "create":
        chosen = [pk for pk in ids if request.POST.get(f"p{pk}-on") and pk in products]
        created, publish = [], request.POST.get("publish") == "1"
        for pk in chosen:
            given, overrides = _inputs(request, pk)
            try:
                listing, _draft = svc.create_listing(products[pk], given, overrides)
            except svc.AlreadyListed as exc:
                messages.warning(request, str(exc))
                continue
            created.append(listing)
        for listing in created if publish else []:
            _publish(request, listing)
        if not created:
            messages.warning(request, "Нічого не створено — позначте товари галочкою.")
        else:
            messages.success(request, f"Створено карток DigiKey: {len(created)} "
                                      f"({', '.join(l.product.sku for l in created)}).")
            if len(created) == 1:
                return redirect(reverse("admin:bots_digikeylisting_change", args=[created[0].pk]))
            return redirect(reverse("admin:bots_digikeylisting_changelist") + "?card_source=stock")
        return redirect(f"{request.path}?ids={','.join(map(str, ids))}")

    svc.dk_categories()                       # каталог DigiKey: з кешу, раз на тиждень — з DigiKey
    cards, listed = [], []
    for pk in ids:
        product = products.get(pk)
        if product is None:
            continue
        listing = DigiKeyListing.objects.filter(product=product).first()
        if listing:
            listed.append({"product": product, "listing": listing, "roadmap": dk_publish.roadmap(listing)})
            continue
        given, overrides = _inputs(request, pk) if action == "preview" else ({}, {})
        cards.append(_card(product, given, overrides))
    return TemplateResponse(request, "admin/bots/digikeylisting/from_stock.html", {
        **model_admin.admin_site.each_context(request), "opts": DigiKeyListing._meta,
        "title": "🔷 На DigiKey зі складу", "cards": cards, "listed": listed, "ids": ",".join(map(str, ids)),
        "fields_state": svc.fields_state(), "catalog": svc.catalog_state(), "choices": svc.category_choices(),
        "can_add": can_add,
        "title_max": svc.TITLE_MAX, "desc_max": svc.DESC_MAX, "mfr_max": svc.MANUFACTURER_MAX,
        "dk_currency": _dk_currency(),
    })


def _dk_currency():
    from bots.services.dk_prices import currency
    return currency()


def _publish(request, listing):
    from bots.services.dk_marketplace import DKMarketplaceError, publish_listing
    try:
        status = publish_listing(listing)
        messages.info(request, f"{listing.product.sku}: надіслано на DigiKey — "
                               f"{'опубліковано' if status == 'published' else 'чекає затвердження DigiKey'}.")
    except DKMarketplaceError as exc:
        messages.error(request, f"{listing.product.sku}: картку створено, але DigiKey не прийняв — {exc}")
    except Exception as exc:  # noqa: BLE001 — мережа / токен: картка лишається, надіслати можна з неї
        messages.error(request, f"{listing.product.sku}: картку створено, надіслати не вдалося — {exc}")


def product_link(product) -> str:
    """Посилання для картки товару без лістингу (блок «💲 Ціна на DigiKey»)."""
    return reverse("admin:bots_digikeylisting_from_stock") + f"?ids={product.pk}"
