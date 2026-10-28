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

    from .models import LANGUAGES
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
            "contact_name": P.get("contact_name", "").strip()[:200],
            "contact_email": P.get("contact_email", "").strip()[:254],
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

    from .models import LANGUAGES, Offer
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
    return render(request, "offers/quick.html", {
        "price_breaks": breaks,
        **admin.site.each_context(request),
        "title": f"Angebot {offer.display_number}" if offer else "Новий Angebot",
        "data": data, "offer": offer, "thread": thread, "languages": LANGUAGES,
        "rev": services.current_revision(offer) if offer else None,
        "locked": bool(offer and offer.is_locked), "warnings": services.warnings(offer) if offer else [],
        "lines": data["lines"],
    })


@staff_member_required
def price_view(request):
    """Ціни Minerva для артикула й кількостей (підказка в легкій формі)."""
    g = request.GET
    return JsonResponse(services.price_hints(g.get("sku", ""), g.get("qtys", ""), g.get("currency", "EUR")))


@staff_member_required
def customer_view(request, pk):
    """Клієнт CRM для легкої форми: контакт і адреса."""
    from crm.models import Customer
    c = get_object_or_404(Customer, pk=pk)
    return JsonResponse({"id": c.pk, "label": c.company or c.name, "name": c.name or "", "email": c.email or "",
                         "address": services.customer_address(c)})
