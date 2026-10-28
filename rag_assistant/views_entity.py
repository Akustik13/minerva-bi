"""«🧭 Огляд» і «❓ Запитати» в картках (rag_assistant/entity_help.py): запуск, опитування, останній результат.

Доступ — співробітник із правом перегляду картки (view_<model>); запис EntityHelp бачить лише його власник.
"""
from __future__ import annotations

import json

from django.http import Http404, JsonResponse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

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


# ── 📎 Документи й посилання картки (knowledge_items.py) ─────────────────────

def item_json(item, user, supported=None) -> dict:
    """Документ для блоку «📎 Документи й посилання»: що це, хто бачить, стан у RAG."""
    from django.utils import timezone

    from . import knowledge_items, retrieval
    from .models import KnowledgeDoc
    doc = KnowledgeDoc.objects.filter(doc_type=knowledge_items.DOC_TYPE, obj_id=item.pk).first()
    supported = retrieval.documents_supported() if supported is None else supported
    if item.url and not item.fetched_at and not item.fetch_error and not item.file:
        rag = "⏳ Minerva завантажує сторінку…"
    elif doc is None or not doc.fed:
        rag = "не в базі знань (індексацію вимкнено або немає тексту)"
    elif not supported:
        rag = "чекає: RAG ще не приймає документи (потрібне оновлення RAG-ПК)"
    else:
        rag = {"indexed": "✓ у RAG", "failed": f"✗ RAG: {doc.error or 'помилка'}", "skipped": "RAG пропустив (немає тексту)",
               "pending": "⏳ чекає RAG", "deleted": "прибрано"}.get(doc.current_state, doc.current_state)
    return {
        "id": item.pk, "kind": item.kind, "kind_label": item.get_kind_display(), "title": item.title, "note": item.note,
        "url": item.url, "open": f"/rag/knowledge/{item.pk}/", "file_name": item.file_name,
        "size_kb": round(item.file_size / 1024) if item.file_size else 0, "private": item.visibility == item.PRIVATE,
        "owner": (item.owner.get_full_name() or item.owner.username) if item.owner_id else "",
        "created": timezone.localtime(item.created_at).strftime("%d.%m.%Y"), "fetch_error": item.fetch_error,
        "auto": item.auto, "can_change": knowledge_items.can_change(user, item), "rag": rag,
    }


@require_http_methods(["GET", "POST"])
def entity_documents(request, kind, pk):
    """GET — документи картки, які бачить користувач; POST (multipart: file або url, title, note, private) — додати."""
    from . import knowledge_items, retrieval
    obj = _entity(request, kind, pk)
    if request.method == "POST":
        links = {kind: obj}
        if kind == "order" and obj is not None:              # документ замовлення — і в картці клієнта
            customer = entity_help._order_customer(obj)
            if customer is not None:
                links["customer"] = customer
        if kind == "project" and obj.customer_id:
            links["customer"] = obj.customer
        fields = dict(title=request.POST.get("title", ""), note=request.POST.get("note", ""),
                      private=request.POST.get("private"), links=links)
        try:
            if request.FILES.get("file"):
                knowledge_items.add_file(request.user, request.FILES["file"], **fields)
            else:
                knowledge_items.add_link(request.user, request.POST.get("url", ""), **fields)
        except knowledge_items.ItemError as e:
            return JsonResponse({"ok": False, "error": str(e)}, status=400)
    supported = retrieval.documents_supported()
    items = knowledge_items.items_for(request.user, kind, obj)
    return JsonResponse({"ok": True, "items": [item_json(i, request.user, supported) for i in items],
                         "supported": supported})


@require_POST
def knowledge_item_action(request, pk, action):
    """«🔄 Оновити» сторінку посилання, «🔒 / 👥» видимість, «✕ Видалити» — лише власник або адміністратор."""
    from . import knowledge_items
    from .models import KnowledgeItem
    item = KnowledgeItem.objects.filter(pk=pk).first()
    if item is None or not knowledge_items.can_view(request.user, item):
        raise Http404
    if not knowledge_items.can_change(request.user, item):
        return JsonResponse({"ok": False, "error": "Змінити може той, хто додав документ, або адміністратор"}, status=403)
    if action == "delete":
        knowledge_items.delete(item)
        return JsonResponse({"ok": True, "deleted": True})
    if action == "refresh":
        if not item.url:
            return JsonResponse({"ok": False, "error": "Це не посилання"}, status=400)
        KnowledgeItem.objects.filter(pk=item.pk).update(fetched_at=None, fetch_error="")
        knowledge_items.start_fetch(item.pk)
    elif action == "visibility":
        knowledge_items.update(item, private=item.visibility != item.PRIVATE)
    else:
        raise Http404
    item.refresh_from_db()
    return JsonResponse({"ok": True, "item": item_json(item, request.user)})


@require_GET
def knowledge_item_page(request, pk):
    """Документ бази знань для людини (посилання з джерел RAG): файл — у браузері, посилання — на сайт."""
    from django.http import FileResponse, HttpResponseRedirect

    from . import knowledge_items
    from .models import KnowledgeItem
    item = KnowledgeItem.objects.filter(pk=pk).select_related("customer", "project", "order", "product").first()
    if item is None or not knowledge_items.can_view(request.user, item):
        raise Http404
    path = knowledge_items.file_path(item)
    if path:
        return FileResponse(open(path, "rb"), as_attachment=False, filename=item.file_name or None)
    if item.url.startswith(("http://", "https://")):
        return HttpResponseRedirect(item.url)
    raise Http404
