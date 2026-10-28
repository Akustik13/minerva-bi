"""«🧭 Огляд» і «❓ Запитати» в картках (rag_assistant/entity_help.py): запуск, опитування, останній результат.

Доступ — співробітник із правом перегляду картки (view_<model>); запис EntityHelp бачить лише його власник.
"""
from __future__ import annotations

import json

from django.http import Http404, JsonResponse
from django.views.decorators.http import require_GET, require_POST

from . import entity_help
from .models import EntityHelp


def _entity(request, kind, pk):
    if not entity_help.can_view(request.user, kind):
        raise Http404
    obj = entity_help.get_object(kind, pk)
    if obj is None:
        raise Http404
    return obj


@require_POST
def entity_start(request, kind, pk, mode):
    """POST {question} → новий огляд (review) або відповідь (answer) у фоні; браузер опитує help/<id>/."""
    obj = _entity(request, kind, pk)
    if mode not in dict(EntityHelp.MODES):
        raise Http404
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        body = {}
    question = " ".join(str(body.get("question") or "").split())[:2000]
    if mode == EntityHelp.MODE_ANSWER and not question:
        return JsonResponse({"ok": False, "error": "Напишіть запитання"}, status=400)
    if mode == EntityHelp.MODE_REVIEW and kind == "project":
        return JsonResponse({"ok": False, "error": "Огляд проєкту — кнопкою «🧭 Скласти огляд» вище"}, status=400)
    help_ = entity_help.start(request.user, kind, obj, mode=mode, question=question)
    return JsonResponse(entity_help.as_json(help_))


@require_GET
def entity_help_state(request, pk):
    help_ = EntityHelp.objects.filter(pk=pk, user=request.user).first()
    if help_ is None or not entity_help.can_view(request.user, help_.kind):
        raise Http404
    return JsonResponse(entity_help.as_json(help_))


@require_GET
def entity_latest(request, kind, pk):
    """Останній огляд / відповідь користувача для картки (?mode=review|answer) — показати при відкритті сторінки."""
    _entity(request, kind, pk)
    mode = request.GET.get("mode") or EntityHelp.MODE_REVIEW
    help_ = entity_help.latest(request.user, kind, pk, mode)
    if help_ is None:
        return JsonResponse({"ok": True, "none": True})
    return JsonResponse(entity_help.as_json(help_, check_stale=mode == EntityHelp.MODE_REVIEW))
