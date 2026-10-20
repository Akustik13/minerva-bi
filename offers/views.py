"""JSON для пошти: «📝 Angebot з листа»."""
from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import services


@staff_member_required
@require_POST
def from_thread_view(request, thread_pk):
    """Чернетка Angebot з гілки: клієнт і проєкт гілки, вихідний лист, позиції з артикулів у тексті."""
    from email_assistant.models import EmailThread
    qs = EmailThread.objects.all() if request.user.is_superuser else EmailThread.objects.filter(
        account__user=request.user)
    thread = get_object_or_404(qs, pk=thread_pk)
    offer = services.create_from_thread(thread, request.user)
    return JsonResponse({
        "ok": True, "id": offer.pk, "number": offer.number, "lines": offer.lines.count(),
        "url": reverse("admin:offers_offer_change", args=[offer.pk]),
        "warnings": services.warnings(offer),
    })
