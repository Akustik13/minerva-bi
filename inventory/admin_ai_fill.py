"""HTTP для панелі «🤖 AI: заповнити з даташита, посилань і тексту» (логіка — inventory/services/ai_fill.py).

  POST …/ai-fill/            (multipart: files, links, text, pn, web, model, datasheet, product_id, listing_id) → job
  GET  …/ai-fill/<job>/      → хід, результат (лише автору)
  POST …/ai-fill/<job>/      {action: cancel} → зупинити

Підключається до адмінки товарів (ціль product) і лістингів DigiKey (ціль digikey) — `urls(...)`; хто може
запускати, вирішує сторінка (`can(request)`: додавати або змінювати товари / лістинги).
"""
from django.http import JsonResponse
from django.urls import path

from inventory.services import ai_fill


def _refuse(status, text):
    resp = JsonResponse({"ok": False, "error": text}, status=status)
    if status == 405:
        resp["Allow"] = "POST"
    return resp


def _int(value):
    value = str(value or "").strip()
    return int(value) if value.isdigit() else None


def start_view(request, target, can):
    if request.method != "POST":
        return _refuse(405, "Лише POST")
    if not can(request):
        return _refuse(403, "Немає права заповнювати цю картку")
    from inventory.models import Product
    product = listing = None
    pid = _int(request.POST.get("product_id"))
    if pid is not None:
        if not request.user.has_perm("inventory.view_product"):
            return _refuse(403, "Немає права бачити товари")
        product = Product.objects.filter(pk=pid).first()
    lid = _int(request.POST.get("listing_id"))
    if lid is not None and target == "digikey":
        from bots.models import DigiKeyListing
        listing = DigiKeyListing.objects.filter(pk=lid).first()
        if listing is not None and product is None and listing.product_id:
            product = listing.product
    files = []
    for f in request.FILES.getlist("files")[:ai_fill.FILES_MAX + 1]:
        if f.size > ai_fill.FILE_MB * 1024 * 1024:
            return _refuse(400, f"«{f.name}» більший за {ai_fill.FILE_MB} МБ")
        files.append((f.name, f.read()))
    try:
        job = ai_fill.start(request.user, target, files=files, links=request.POST.get("links", ""),
                            text=request.POST.get("text", ""), pn=request.POST.get("pn", ""), product=product,
                            listing=listing, datasheet=request.POST.get("datasheet") == "1",
                            web=request.POST.get("web", "1") == "1", model=request.POST.get("model", ""))
    except ai_fill.FillError as e:
        return _refuse(400, str(e))
    return JsonResponse(ai_fill.public(job))


def status_view(request, job):
    found = ai_fill.state(job, request.user)
    if found is None:
        return _refuse(404, "Немає такого заповнення (або воно чуже)")
    if request.method == "POST" and request.POST.get("action") == "cancel":
        ai_fill.cancel(job, request.user)
        found = ai_fill.state(job, request.user)
    return JsonResponse(ai_fill.public(found))


def urls(admin_site, name, target, can):
    """name — префікс назв маршрутів («inventory_product» → inventory_product_ai_fill / …_ai_fill_status)."""
    return [
        path("ai-fill/", admin_site.admin_view(lambda request: start_view(request, target, can)),
             name=f"{name}_ai_fill"),
        path("ai-fill/<str:job>/", admin_site.admin_view(lambda request, job: status_view(request, job)),
             name=f"{name}_ai_fill_status"),
    ]
