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
def dismiss_quote_request_view(request, pk):
    """«✕ Не запит»: підказку прибрано, автоматика цей лист більше не позначить."""
    qr = get_object_or_404(QuoteRequest, pk=pk, thread__in=_user_threads(request))
    detection.dismiss(qr, request.user)
    return JsonResponse({"ok": True})
