"""Маршрути бази знань для RAG: /api/v1/knowledge/… (схеми: api/schemas/knowledge, README там же).

Лише APIKey з правами knowledge:read / knowledge:write; ключ магазину → 403 shop_key_forbidden.
Будь-яка помилка — JSON {code, message, retryable, request_id[, details]}.
"""
import json
import logging
import uuid

from django.http import Http404, HttpResponse
from rest_framework import exceptions as drf_exc
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from rag_assistant import knowledge
from rag_assistant.knowledge import KnowledgeError

from .authentication import APIKeyAuthentication
from .models import APIKey
from .throttling import APIKeyRateThrottle

logger = logging.getLogger(__name__)


class KnowledgeRateThrottle(APIKeyRateThrottle):
    scope = "knowledge"


def _request_id(request) -> str:
    rid = getattr(request, "_knowledge_request_id", None)
    if rid is None:
        rid = request._knowledge_request_id = "req-" + uuid.uuid4().hex[:16]
    return rid


def error_response(request, code, status, message, retryable=False, details=None, headers=None):
    body = {"code": code, "message": message, "retryable": retryable, "request_id": _request_id(request)}
    if details:
        body["details"] = details
    response = Response(body, status=status)
    for k, v in (headers or {}).items():
        response[k] = v
    return response


def _int_param(request, name, default, lo, hi):
    raw = request.query_params.get(name)
    if raw in (None, ""):
        return default
    if not str(raw).isdigit() or not lo <= int(raw) <= hi:
        raise KnowledgeError("invalid_request", 400, f"Параметр {name}: ціле від {lo} до {hi}.")
    return int(raw)


class KnowledgeView(APIView):
    authentication_classes = [APIKeyAuthentication]
    permission_classes = []
    throttle_classes = [KnowledgeRateThrottle]
    renderer_classes = [JSONRenderer]
    scope_mode = "read"

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        auth = request.auth
        if not isinstance(auth, APIKey):
            raise KnowledgeError("unauthorized", 401, "Потрібен заголовок Authorization: Token <ключ>.")
        if auth.shop_id:
            raise KnowledgeError("shop_key_forbidden", 403, "Ключ магазину не має доступу до бази знань.")
        if not auth.has_scope(f"knowledge:{self.scope_mode}"):
            raise KnowledgeError("forbidden_scope", 403, f"Ключ не має права knowledge:{self.scope_mode}.")
        if not knowledge.instance_id():
            raise KnowledgeError("instance_not_configured", 503,
                                 "MINERVA_INSTANCE_ID не задано — база знань вимкнена на цьому сервері.", retryable=True)

    def handle_exception(self, exc):
        request = self.request
        if isinstance(exc, KnowledgeError):
            return error_response(request, exc.code, exc.status, exc.message, exc.retryable, exc.details)
        if isinstance(exc, (drf_exc.NotAuthenticated, drf_exc.AuthenticationFailed)):
            return error_response(request, "unauthorized", 401, str(getattr(exc, "detail", "")) or "Невірний ключ.",
                                  headers={"WWW-Authenticate": "Token"})
        if isinstance(exc, drf_exc.Throttled):
            wait = max(1, int(exc.wait or 1))
            return error_response(request, "rate_limited", 429, "Забагато запитів.", True,
                                  {"retry_after_seconds": wait}, headers={"Retry-After": str(wait)})
        if isinstance(exc, (drf_exc.ParseError, drf_exc.ValidationError, drf_exc.UnsupportedMediaType)):
            return error_response(request, "invalid_request", 400, "Некоректний запит.")
        if isinstance(exc, (drf_exc.PermissionDenied,)):
            return error_response(request, "forbidden_scope", 403, "Недостатньо прав.")
        if isinstance(exc, drf_exc.MethodNotAllowed):
            return error_response(request, "invalid_request", 405, "Метод не підтримується.")
        if isinstance(exc, (Http404, drf_exc.NotFound)):
            return error_response(request, "document_not_found", 404, "Не знайдено.")
        logger.exception("knowledge API error (%s)", _request_id(request))
        return error_response(request, "internal_error", 500, "Внутрішня помилка Minerva.", True)


class ChangesView(KnowledgeView):
    def get(self, request):
        return Response(knowledge.read_changes(
            after=_int_param(request, "after", 0, 0, 2 ** 62),
            limit=_int_param(request, "limit", 50, 1, 200),
            generation=request.query_params.get("generation") or None))


class DocumentView(KnowledgeView):
    def get(self, request, document_id):
        return Response(knowledge.get_document(document_id)[2])


class AttachmentView(KnowledgeView):
    def get(self, request, document_id, index):
        revision = _int_param(request, "revision", None, 1, 2 ** 31)
        if revision is None:
            raise KnowledgeError("invalid_request", 400, "Параметр revision обов'язковий.")
        data, att = knowledge.attachment_bytes(document_id, index, revision)
        response = HttpResponse(data, content_type=att["content_type"])
        digest = att["content_hash"].split(":", 1)[1]
        response["X-Content-SHA256"] = digest
        response["ETag"] = f'"{att["content_hash"]}"'
        response["Content-Disposition"] = "attachment"
        return response


class SnapshotCreateView(KnowledgeView):
    def post(self, request):
        return Response(knowledge.create_snapshot(), status=201)


class SnapshotPageView(KnowledgeView):
    def get(self, request, snapshot_id):
        return Response(knowledge.snapshot_page(snapshot_id, request.query_params.get("cursor"),
                                                _int_param(request, "limit", 500, 1, 5000)))


class StatusView(KnowledgeView):
    scope_mode = "write"

    def post(self, request):
        try:
            payload = json.loads(request.body or b"null")
        except ValueError:
            raise KnowledgeError("invalid_request", 400, "Тіло — не JSON.")
        return Response(knowledge.apply_status(payload, _request_id(request)))


class SummaryView(KnowledgeView):
    def get(self, request):
        return Response(knowledge.summary())
