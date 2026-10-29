"""JSON для пошти: «📝 Angebot з листа», «✕ Не запит»."""
from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import detection, services
from .models import QuoteRequest


def _user_threads(request):
    from email_assistant.models import EmailThread
    return EmailThread.objects.all() if request.user.is_superuser else EmailThread.objects.filter(
        account__user=request.user)


@staff_member_required
@require_POST
def from_thread_view(request, thread_pk):
    """Чернетка Angebot з гілки: клієнт і проєкт гілки, вихідний лист, позиції з артикулів у тексті."""
    thread = get_object_or_404(_user_threads(request), pk=thread_pk)
    offer = services.create_from_thread(thread, request.user)
    detection.mark_done(thread, offer, request.user)
    return JsonResponse({
        "ok": True, "id": offer.pk, "number": offer.number, "lines": offer.lines.count(),
        "url": reverse("admin:offers_offer_change", args=[offer.pk]),
        "warnings": services.warnings(offer),
    })


@staff_member_required
@require_POST
def rescan_message_view(request, message_pk):
    """«↻ Перевірити ще раз» з «🔍 Чому лист тут»: лист знову перевіряється правилами (рішення людини лишається)."""
    from email_assistant.models import EmailMessage
    msg = get_object_or_404(EmailMessage, pk=message_pk, thread__in=_user_threads(request))
    detection.scan(msg)
    return JsonResponse({"ok": True, **detection.explain(msg)})


@staff_member_required
@require_POST
def dismiss_quote_request_view(request, pk):
    """«✕ Не запит»: підказку прибрано, автоматика цей лист більше не позначить."""
    qr = get_object_or_404(QuoteRequest, pk=pk, thread__in=_user_threads(request))
    detection.dismiss(qr, request.user)
    return JsonResponse({"ok": True})


# ── ⚡ Легка форма Angebot — як Commercial Invoice: одна сторінка → документ на бланку ─────────
def _quick_fields(request) -> dict:
    import datetime
    from decimal import Decimal, InvalidOperation

    from django.utils import timezone

    from crm.models import Customer

    from .models import LANGUAGES, LEAD_MODES, PRICE_MODES
    P = request.POST
    customer = Customer.objects.filter(pk=P.get("customer")).first() if str(P.get("customer") or "").isdigit() else None
    project = None
    if str(P.get("project") or "").isdigit():
        from projects.models import Project
        project = Project.objects.filter(pk=int(P["project"])).first()
    try:
        days = max(0, min(365, int(P.get("valid_days") or 30)))
    except ValueError:
        days = 30
    try:
        shipping = max(Decimal(str(P.get("shipping_cost") or 0).replace(",", ".")), Decimal(0))
    except InvalidOperation:
        shipping = Decimal(0)
    language = P.get("language") if P.get("language") in dict(LANGUAGES) else "de"
    return {"customer": customer, "project": project, "language": language,
            "price_mode": P.get("price_mode") if P.get("price_mode") in dict(PRICE_MODES) else "auto",
            "lead_mode": P.get("lead_mode") if P.get("lead_mode") in dict(LEAD_MODES) else "auto",
            "contact_name": P.get("contact_name", "").strip()[:200],
            "contact_email": P.get("contact_email", "").strip()[:254],
            "contact_phone": P.get("contact_phone", "").strip()[:50],
            "signer_name": P.get("signer_name", "").strip()[:150],
            "signer_position": P.get("signer_position", "").strip()[:100],
            "billing_address": P.get("billing_address", "").strip(),
            "currency": (P.get("currency", "").strip().upper() or "EUR")[:3],
            "valid_until": timezone.localdate() + datetime.timedelta(days=days), "shipping_cost": shipping,
            "intro_text": P.get("intro_text", ""), "payment_terms": P.get("payment_terms", ""),
            "delivery_terms": P.get("delivery_terms", ""), "outro_text": P.get("outro_text", "")}


def _quick_rows(request) -> list:
    import json
    try:
        rows = json.loads(request.POST.get("lines_json") or "[]")
    except ValueError:
        return []
    return [r for r in rows if isinstance(r, dict)][:100]


@staff_member_required
def quick_view(request, pk=None):
    """«⚡ Angebot» (нове з листа ?thread= / порожнє, або чернетка): поля → «📄 Згенерувати» → DOCX / PDF."""
    import logging

    from django.contrib import admin, messages
    from django.core.exceptions import PermissionDenied
    from django.shortcuts import redirect, render

    from .models import LANGUAGES, LEAD_MODES, PRICE_MODES, Offer
    offer = get_object_or_404(Offer, pk=pk) if pk else None
    thread = None
    tpk = request.POST.get("thread") or request.GET.get("thread")
    if offer is None and str(tpk or "").isdigit():
        thread = get_object_or_404(_user_threads(request), pk=int(tpk))
    if request.method == "POST":
        if not request.user.has_perm("offers.add_offer" if offer is None else "offers.change_offer"):
            raise PermissionDenied
        source = services.thread_source(thread)[0] if thread is not None else None
        try:
            offer = services.save_quick(_quick_fields(request), _quick_rows(request), request.user, offer=offer,
                                        source=source, thread=thread)
        except services.OfferError as e:
            messages.error(request, f"❌ {e}")
            return redirect(request.get_full_path())
        try:
            rev = services.generate_document(offer, request.user)
            messages.success(request, f"✅ Angebot {offer.display_number} сформовано" +
                             (" (DOCX + PDF)." if rev.pdf_file else " (DOCX; PDF — на сервері немає LibreOffice)."))
        except Exception as e:  # noqa: BLE001 — Angebot збережено, документ можна сформувати ще раз
            logging.getLogger(__name__).exception("quick offer document")
            messages.warning(request, f"Angebot {offer.display_number} збережено, але документ не сформовано: {e}")
        return redirect(reverse("offers:quick_edit", args=[offer.pk]))
    data = services.quick_prefill(thread) if offer is None else services.quick_from_offer(offer)
    try:
        from shop.models import ShopSettings
        breaks = ShopSettings.get().price_breaks or []
    except ImportError:
        breaks = []
    from . import document
    from .models import OfferSettings
    return render(request, "offers/quick.html", {
        "price_breaks": breaks, "template_vars": document.TEMPLATE_VARS,
        "has_custom_template": bool(OfferSettings.get().template),
        "can_template": request.user.has_perm("offers.change_offersettings"),
        **admin.site.each_context(request),
        "title": f"Angebot {offer.display_number}" if offer else "Новий Angebot",
        "data": data, "offer": offer, "thread": thread, "languages": LANGUAGES,
        "price_modes": PRICE_MODES, "lead_modes": LEAD_MODES,
        "texts_by_lang": {key: services.texts_for(key) for key, _label in LANGUAGES},
        "rev": services.current_revision(offer) if offer else None,
        "locked": bool(offer and offer.is_locked), "warnings": services.warnings(offer) if offer else [],
        "targets": services.send_targets(offer, request.user) if offer else [],
        "lines": data["lines"],
    })


def _offer_settings_perm(request):
    from django.core.exceptions import PermissionDenied
    if not request.user.has_perm("offers.change_offersettings"):
        raise PermissionDenied


@staff_member_required
def list_view(request):
    """«📝 Angebote» — як Commercial Invoices: новий (легка форма / з пошти), згенеровані з файлами й «Надіслати»,
    Word шаблон."""
    from django.core.exceptions import PermissionDenied
    from django.shortcuts import render

    from django.contrib import admin

    from . import document
    from .models import Offer, OfferSettings
    if not request.user.has_perm("offers.view_offer"):
        raise PermissionDenied
    q, status = request.GET.get("q", "").strip(), request.GET.get("status", "")
    status = status if status in dict(Offer.STATUS_CHOICES) else ""
    data = services.overview(q, status, request.user)
    statuses = [(key, label, data["counts"].get(key, 0)) for key, label in Offer.STATUS_CHOICES
                if data["counts"].get(key)]
    return render(request, "offers/list.html", {
        **admin.site.each_context(request), "title": "Angebote", **data, "q": q, "status": status,
        "statuses": statuses, "open_requests": QuoteRequest.objects.filter(status=QuoteRequest.NEW).count(),
        "template_vars": document.TEMPLATE_VARS, "has_custom_template": bool(OfferSettings.get().template),
        "can_template": request.user.has_perm("offers.change_offersettings"),
        "can_add": request.user.has_perm("offers.add_offer"),
        "can_delete": request.user.has_perm("offers.delete_offer"),
    })


@staff_member_required
@require_POST
def delete_view(request, pk):
    """🗑 зі сторінки «📝 Angebote» — лише ненадіслана (надіслана версія незмінна, як в адмінці)."""
    from django.contrib import messages
    from django.core.exceptions import PermissionDenied
    from django.shortcuts import redirect

    from .models import Offer
    offer = get_object_or_404(Offer, pk=pk)
    if not request.user.has_perm("offers.delete_offer"):
        raise PermissionDenied
    if offer.is_locked:
        messages.error(request, f"❌ {offer.display_number} уже надіслано — не видаляється (можна «🚫 Скасовано» в картці).")
    else:
        number = offer.display_number
        offer.delete()
        messages.success(request, f"🗑 Angebot {number} видалено.")
    return redirect(reverse("offers:list"))


@staff_member_required
def template_download_view(request):
    """⬇ Поточний шаблон Angebot (власний або зібраний з бланка Commercial Invoice) — редагувати у Word."""
    from django.http import FileResponse

    from . import document
    from .models import OfferSettings
    custom = bool(OfferSettings.get().template)
    return FileResponse(open(document.template_path(), "rb"), as_attachment=True,
                        filename="angebot_template_custom.docx" if custom else "angebot_template.docx")


@staff_member_required
@require_POST
def template_upload_view(request):
    """⬆ Власний шаблон Angebot: перевіряється на тестових даних, потім — для всіх нових документів."""
    from django.contrib import messages
    from django.core.files.base import ContentFile
    from django.shortcuts import redirect

    from . import document
    from .models import OfferSettings
    _offer_settings_perm(request)
    back = request.POST.get("next") or reverse("offers:quick")
    f = request.FILES.get("template")
    if not f or not f.name.lower().endswith(".docx"):
        messages.error(request, "❌ Потрібен файл .docx")
        return redirect(back)
    data = f.read()
    try:
        document.check_template(data)
    except Exception as e:  # noqa: BLE001 — помилку шаблону показати людині
        messages.error(request, f"❌ Шаблон не прийнято — помилка в змінних: {e}")
        return redirect(back)
    s = OfferSettings.get()
    if s.template:
        s.template.delete(save=False)
    s.template.save("angebot_template_custom.docx", ContentFile(data), save=True)
    messages.success(request, "✅ Власний шаблон Angebot завантажено й перевірено — нові документи робляться з нього.")
    return redirect(back)


@staff_member_required
@require_POST
def template_reset_view(request):
    """↺ Знову шаблон з бланка Commercial Invoice (власний шаблон Angebot прибирається)."""
    from django.contrib import messages
    from django.shortcuts import redirect

    from .models import OfferSettings
    _offer_settings_perm(request)
    s = OfferSettings.get()
    if s.template:
        s.template.delete(save=False)
        s.template = None
        s.save(update_fields=["template"])
    messages.success(request, "↩️ Знову шаблон на бланку Commercial Invoice.")
    return redirect(request.POST.get("next") or reverse("offers:quick"))


@staff_member_required
def template_sample_view(request):
    """👁 Приклад документа на тестових даних (DOCX) — як виглядатиме Angebot з поточним шаблоном."""
    from django.http import HttpResponse

    from . import document
    response = HttpResponse(document.render_docx(document.sample_snapshot()),
                            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    response["Content-Disposition"] = 'attachment; filename="angebot_beispiel.docx"'
    return response


@staff_member_required
def price_view(request):
    """Ціни Minerva для артикула й кількостей (підказка в легкій формі)."""
    g = request.GET
    return JsonResponse(services.price_hints(g.get("sku", ""), g.get("qtys", ""), g.get("currency", "EUR"),
                                             g.get("price_mode", "auto"), g.get("lead_mode", "auto"),
                                             g.get("language", "de")))


@staff_member_required
def products_view(request):
    """Пошук артикула зі складу для легкої форми: ?q=FT → товари (і офери DigiKey), що підходять."""
    return JsonResponse({"results": services.search_products(request.GET.get("q", ""))})


@staff_member_required
def currency_view(request):
    """Валюта за країною клієнта (клієнт CRM, адреса, email) — для легкої форми."""
    from crm.models import Customer

    from . import pricing
    g = request.GET
    customer = Customer.objects.filter(pk=g["customer"]).first() if str(g.get("customer") or "").isdigit() else None
    return JsonResponse(pricing.currency_for(customer, address=g.get("address", ""), email=g.get("email", "")))


@staff_member_required
def customer_view(request, pk):
    """Клієнт CRM для легкої форми: контакт і адреса."""
    from crm.models import Customer
    c = get_object_or_404(Customer, pk=pk)
    return JsonResponse({"id": c.pk, "label": c.company or c.name, "name": c.name or "", "email": c.email or "",
                         "phone": c.phone or "",
                         "address": services.customer_address(c)})
