"""JSON-ендпоінти віджета «Помічник по продукції» (/rag/api/…).

Браузер працює лише з Minerva; Minerva викликає RAG API з ключем на сервері.
Розмови належать користувачу: чужі розмови й повідомлення недоступні (404).
"""
from __future__ import annotations

import json
import os
from datetime import timedelta

from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .client import RagClient, RagError
from .models import RagConversation, RagMessage, RagSettings

APP_LABEL = "rag_assistant"
PENDING_LIMIT = timedelta(minutes=20)


# ── Доступ ───────────────────────────────────────────────────────────────────

def has_access(user) -> bool:
    """Активний співробітник + модуль увімкнений + роль має доступ (як ModuleAccessMiddleware)."""
    if not (user and user.is_authenticated and user.is_active and user.is_staff):
        return False
    try:
        from core.models import ModuleRegistry
        if not ModuleRegistry.check_active(APP_LABEL):
            return False
    except Exception:
        pass
    if user.is_superuser:
        return True
    try:
        profile = user.profile
        if profile.role in ("superadmin", "admin"):
            return True
        allowed = profile.get_allowed_modules()
        return allowed == "__all__" or APP_LABEL in (allowed or [])
    except Exception:
        return True


def _guard(view):
    def wrapped(request, *args, **kwargs):
        if not has_access(request.user):
            return JsonResponse({"error": "Немає доступу до помічника."}, status=403)
        return view(request, *args, **kwargs)
    wrapped.__name__ = view.__name__
    return wrapped


def _err(e: RagError, status: int = 503):
    return JsonResponse({"error": e.message, "code": e.code}, status=429 if e.code == "busy" else status)


def _client(request) -> RagClient:
    return RagClient(RagSettings.get(), client_id=f"minerva:u{request.user.pk}")


# ── Серіалізація ─────────────────────────────────────────────────────────────

def _msg(m: RagMessage) -> dict:
    return {"id": m.id, "role": m.role, "content": m.content, "status": m.status, "stage": m.stage,
            "sources": m.sources, "products": m.products, "trace": m.trace, "error": m.error,
            "created_at": m.created_at.isoformat()}


def _conv(c: RagConversation) -> dict:
    return {"id": c.id, "title": c.title or "Нова розмова", "updated_at": c.updated_at.isoformat()}


def _short_trace(trace) -> list:
    out = []
    for s in trace or []:
        if not isinstance(s, dict):
            continue
        out.append({"title": str(s.get("title", ""))[:120], "status": s.get("status", ""),
                    "elapsed": s.get("elapsed_seconds"),
                    "details": [str(d)[:240] for d in (s.get("details") or [])[:6]]})
    return out[:20]


def _sources(raw) -> list:
    return [{"id": s.get("id"), "source": str(s.get("source", ""))[:200], "page": s.get("page"),
             "text": str(s.get("text", ""))[:600], "document_id": s.get("document_id")}
            for s in (raw or []) if isinstance(s, dict)][:12]


def _products(raw) -> list:
    """Продукти з RAG + актуальні дані Minerva (посилання на товар, залишок, ціна продажу, статус)."""
    from inventory.models import Product
    from inventory.services.stock import annotate_stock, resolve_sku
    rows = []
    for p in (raw or [])[:10]:
        if not isinstance(p, dict):
            continue
        sku = str(p.get("product") or p.get("sku") or "").strip()
        if not sku:
            continue
        row = {"sku": sku, "source": str(p.get("source", ""))[:200], "page": p.get("page")}
        try:  # гібридний режим RAG: текст продукту — JSON запису каталогу
            data = json.loads(p.get("text") or "")
            if isinstance(data, dict):
                for k in ("name", "price", "currency", "available", "in_stock", "lifecycle_status"):
                    if data.get(k) is not None:
                        row[k] = data[k]
        except (TypeError, ValueError):
            pass
        product = resolve_sku(sku)
        if product:
            q = annotate_stock(Product.objects.filter(pk=product.pk)).values("_available").first() or {}
            row["minerva"] = {
                "id": product.pk, "sku": product.sku, "name": product.name or "",
                "url": reverse("admin:inventory_product_change", args=[product.pk]),
                "available": float(q.get("_available") or 0),
                "sale_price": float(product.sale_price) if product.sale_price is not None else None,
                "lifecycle_status": product.lifecycle_status,
            }
        rows.append(row)
    return rows


def _refresh(m: RagMessage, client: RagClient | None) -> None:
    """Опитати RAG для повідомлення, що виконується, і зберегти результат."""
    if m.status != RagMessage.PENDING or not m.rag_job_id:
        return
    if timezone.now() - m.created_at > PENDING_LIMIT:
        m.status, m.error, m.finished_at = RagMessage.FAILED, "Час очікування відповіді минув.", timezone.now()
        m.save(update_fields=["status", "error", "finished_at"])
        return
    if client is None:
        return
    try:
        job = client.job(m.rag_job_id)
    except RagError as e:
        if e.code in ("not_found", "auth"):
            m.status, m.error, m.finished_at = RagMessage.FAILED, e.message, timezone.now()
            m.save(update_fields=["status", "error", "finished_at"])
        else:
            m.stage = e.message  # тимчасова проблема зв'язку — спробуємо при наступному опитуванні
            m.save(update_fields=["stage"])
        return
    fields = ["stage", "trace"]
    m.stage = str(job.get("stage") or "")[:200]
    m.trace = _short_trace(job.get("trace"))
    status = job.get("status")
    if status == "completed":
        res = job.get("result") or {}
        answer = str(res.get("answer") or "")
        warnings = [str(w) for w in (res.get("warnings") or []) if w]
        if warnings:
            answer += "\n\n" + "\n".join("⚠ " + w for w in warnings[:5])
        m.content = answer or "Знайдено джерела, але відповідь не сформовано."
        m.sources, m.products = _sources(res.get("sources")), _products(res.get("products"))
        m.trace = _short_trace(res.get("trace") or job.get("trace"))
        m.usage = res.get("usage") or {}
        m.status, m.finished_at = RagMessage.DONE, timezone.now()
        fields += ["content", "sources", "products", "usage", "status", "finished_at"]
    elif status == "failed":
        m.status, m.finished_at = RagMessage.FAILED, timezone.now()
        m.error = str(job.get("error") or "Обробка не вдалася.")[:255]
        fields += ["status", "error", "finished_at"]
    m.save(update_fields=fields)


# ── Ендпоінти ────────────────────────────────────────────────────────────────

@require_GET
def avatar(request):
    path = os.path.join(os.path.dirname(__file__), "assets", "avatar.png")
    if not os.path.exists(path):
        raise Http404
    resp = FileResponse(open(path, "rb"), content_type="image/png")
    resp["Cache-Control"] = "public, max-age=86400"
    return resp


@require_GET
@_guard
def state(request):
    st = RagSettings.get()
    convs = RagConversation.objects.filter(user=request.user)[:30]
    return JsonResponse({"enabled": st.enabled, "configured": st.configured, "name": st.name,
                         "conversations": [_conv(c) for c in convs]})


@require_POST
@_guard
def conversation_new(request):
    c = RagConversation.objects.create(user=request.user)
    return JsonResponse({"conversation": _conv(c)})


@require_GET
@_guard
def conversation_detail(request, pk):
    c = get_object_or_404(RagConversation, pk=pk, user=request.user)
    return JsonResponse({"conversation": _conv(c), "messages": [_msg(m) for m in c.messages.all()]})


@require_POST
@_guard
def conversation_delete(request, pk):
    get_object_or_404(RagConversation, pk=pk, user=request.user).delete()
    return JsonResponse({"ok": True})


@require_POST
@_guard
def ask(request, pk):
    c = get_object_or_404(RagConversation, pk=pk, user=request.user)
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"error": "Некоректний запит."}, status=400)
    question = str(body.get("question") or "").strip()
    if not 1 <= len(question) <= 3000:
        return JsonResponse({"error": "Запитання — від 1 до 3000 символів."}, status=400)
    st = RagSettings.get()
    if not st.enabled:
        return JsonResponse({"error": "Помічник вимкнений у налаштуваннях."}, status=503)
    if c.messages.filter(status=RagMessage.PENDING).exists():
        return JsonResponse({"error": "Дочекайтеся відповіді на попереднє запитання."}, status=409)
    history = [{"role": m.role, "content": m.content[:3000]}
               for m in c.messages.filter(status=RagMessage.DONE).exclude(content="").order_by("-created_at", "-pk")[:8]]
    history.reverse()
    try:
        job_id = _client(request).ask(question, history, language=st.language, latest=st.latest_only)
    except RagError as e:
        return _err(e)  # 429 busy — запит не прийнято, віджет повторить
    user_msg = RagMessage.objects.create(conversation=c, role=RagMessage.ROLE_USER, content=question)
    bot_msg = RagMessage.objects.create(conversation=c, role=RagMessage.ROLE_ASSISTANT, status=RagMessage.PENDING,
                                        rag_job_id=job_id, stage="Запит прийнято")
    if not c.title:
        c.title = question[:120]
    c.save(update_fields=["title", "updated_at"])
    return JsonResponse({"user": _msg(user_msg), "assistant": _msg(bot_msg), "conversation": _conv(c)}, status=202)


@require_GET
@_guard
def message(request, pk):
    m = get_object_or_404(RagMessage, pk=pk, conversation__user=request.user)
    if m.status == RagMessage.PENDING:
        try:
            client = _client(request)
        except RagError:
            client = None
        _refresh(m, client)
    return JsonResponse({"message": _msg(m)})
