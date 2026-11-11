"""🎙 Наради — адреси для вікна запису на RAG-ПК: /api/v1/meetings/… (уся логіка — meetings/services.py).

Лише службовий ключ RAG-ПК з правом meetings:write («Authorization: Token <ключ>»; кнопка «🔑 Створити ключ для
RAG-ПК (наради)» у «⚙️ Налаштування нарад»). Сесія адмінки не діє (лише APIKeyAuthentication), ключ магазину →
403 shop_key_forbidden. Квиток вікна запису — лише в тілі запиту, ключ надсилання (push_token) — лише в заголовку
X-Meeting-Token: адреса й параметри йдуть у журнал API, тіло й заголовки — ні (параметри адреси тут не потрібні й у
журнал потрапляють лише як «***»). Відповіді — без змісту наради: ні розшифровки, ні цитат (назва — лише в redeem,
для банера вікна запису). Тіло читається тут же (request.body), тож діє межа розміру: більше — 413 payload_too_large.
Будь-яка помилка — JSON {code, message, retryable, request_id[, details]}, як у бази знань (api/knowledge_views.py).
Протокол і поля — api/README.md, розділ 13.
"""
import json
import logging
import uuid

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.http import Http404, QueryDict
from rest_framework import exceptions as drf_exc
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from meetings import services
from meetings.services import MeetingError, PushError, TicketError

from .authentication import APIKeyAuthentication
from .models import APIKey
from .permissions import HasAPIKeyScope, required_scope
from .throttling import APIKeyQuotaThrottle, APIKeyRateThrottle

logger = logging.getLogger(__name__)

BODY_MAX = 2 * 1024 * 1024          # байтів тіла: RAG-ПК ділить розшифровку на частини ≤ 512 КБ
TOKEN_HEADER = "HTTP_X_MEETING_TOKEN"
TOKEN_MAX = 300


class MeetingsRateThrottle(APIKeyRateThrottle):
    """Окремий хвилинний ліміт для RAG-ПК (MEETINGS_RATE_LIMIT, за замовчуванням 300/min)."""
    scope = "meetings"


class Refusal(Exception):
    """Відмова самого API (не логіки нарад): ключ магазину, тіло завелике чи не JSON."""

    def __init__(self, code, status, message, retryable=False, details=None):
        super().__init__(message)
        self.code, self.status, self.message, self.retryable, self.details = code, status, message, retryable, details


def _request_id(request) -> str:
    rid = getattr(request, "_meetings_request_id", None)
    if rid is None:
        rid = request._meetings_request_id = "req-" + uuid.uuid4().hex[:16]
    return rid


def error_response(request, code, status, message, retryable=False, details=None, headers=None):
    body = {"code": code, "message": message, "retryable": retryable, "request_id": _request_id(request)}
    if details:
        body["details"] = details
    response = Response(body, status=status)
    for k, v in (headers or {}).items():
        response[k] = v
    return response


def body_limit() -> int:
    """Межа тіла: BODY_MAX, але не більше за DATA_UPLOAD_MAX_MEMORY_SIZE Django (якщо задано)."""
    django_max = getattr(settings, "DATA_UPLOAD_MAX_MEMORY_SIZE", None)
    return min(BODY_MAX, django_max) if django_max else BODY_MAX


def _too_big(limit):
    return Refusal("payload_too_large", 413, "Запит завеликий — надсилайте розшифровку меншими частинами.",
                   details={"max_bytes": limit})


def read_body(request) -> dict:
    """Тіло запиту — JSON-об'єкт. Завелике — 413 payload_too_large (не повторювати те саме), не JSON — 400."""
    limit = body_limit()
    try:
        length = int(request.META.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        length = 0
    if length > limit:
        raise _too_big(limit)
    try:
        raw = request.body
    except RequestDataTooBig:
        raise _too_big(limit) from None
    if len(raw) > limit:
        raise _too_big(limit)
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise Refusal("invalid_request", 400, "Тіло запиту — не JSON.", details={"field": "body"}) from None
    if not isinstance(data, dict):
        raise Refusal("invalid_request", 400, "Тіло запиту має бути JSON-об'єктом.", details={"field": "body"})
    return data


def push_token(request) -> str:
    """Ключ надсилання з заголовка X-Meeting-Token (не з тіла й не з адреси)."""
    return str(request.META.get(TOKEN_HEADER, "") or "").strip()[:TOKEN_MAX]


class MeetingsBenchView(APIView):
    """Основа адрес нарад для RAG-ПК (див. опис модуля)."""
    authentication_classes = [APIKeyAuthentication]
    permission_classes = [HasAPIKeyScope]
    throttle_classes = [MeetingsRateThrottle, APIKeyQuotaThrottle]
    renderer_classes = [JSONRenderer]
    resource_scope = "meetings"          # POST — meetings:write (ping — теж write, див. MeetingPingView)
    action_scopes = {}
    shop_key_forbidden = True            # api/key_insight._access: ключ магазину — «заборонено»
    shop_denied_text = "ключ магазину не має доступу до нарад"

    def dispatch(self, request, *args, **kwargs):
        # Параметри адреси тут нічого не значать; щоб випадковий ?ticket=… не потрапив у журнал API — лише «***».
        if request.GET:
            masked = QueryDict(mutable=True)
            for name in request.GET:
                masked.setlist(name, ["***"] * len(request.GET.getlist(name)))
            request.GET = masked
            request.META["QUERY_STRING"] = masked.urlencode(safe="*")
        return super().dispatch(request, *args, **kwargs)

    def check_permissions(self, request):
        auth = request.auth
        if not isinstance(auth, APIKey):
            raise drf_exc.NotAuthenticated("Потрібен заголовок Authorization: Token <ключ RAG-ПК>.")
        if auth.shop_id:
            raise Refusal("shop_key_forbidden", 403, "Ключ магазину не має доступу до нарад — потрібен ключ RAG-ПК.")
        method = request.method.lower()
        if method not in self.http_method_names or method == "options" or not hasattr(self, method):
            raise drf_exc.MethodNotAllowed(request.method)     # 405 раніше за право (meetings:read не існує)
        super().check_permissions(request)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response

    def handle_exception(self, exc):
        request = self.request
        if isinstance(exc, PushError):
            body = exc.as_dict()
            return error_response(request, body["code"], exc.status, body["message"], body["retryable"],
                                  body.get("details"))
        if isinstance(exc, TicketError):
            return error_response(request, exc.code, exc.status, exc.message)
        if isinstance(exc, Refusal):
            return error_response(request, exc.code, exc.status, exc.message, exc.retryable, exc.details)
        if isinstance(exc, RequestDataTooBig):
            return self.handle_exception(_too_big(body_limit()))
        if isinstance(exc, (drf_exc.NotAuthenticated, drf_exc.AuthenticationFailed)):
            return error_response(request, "unauthorized", 401, str(getattr(exc, "detail", "")) or "Невірний ключ.",
                                  headers={"WWW-Authenticate": "Token"})
        if isinstance(exc, drf_exc.Throttled):
            wait = max(1, int(exc.wait or 1))
            return error_response(request, "rate_limited", 429, "Забагато запитів — повторіть пізніше.", True,
                                  {"retry_after_seconds": wait}, headers={"Retry-After": str(wait)})
        if isinstance(exc, drf_exc.PermissionDenied):
            scope = required_scope(request, self) or "meetings:write"
            return error_response(request, "forbidden_scope", 403, f"Ключ не має права {scope}.")
        if isinstance(exc, drf_exc.MethodNotAllowed):
            return error_response(request, "invalid_request", 405, "Метод не підтримується.")
        if isinstance(exc, (drf_exc.ParseError, drf_exc.ValidationError, drf_exc.UnsupportedMediaType,
                            drf_exc.NotAcceptable)):
            return error_response(request, "invalid_request", getattr(exc, "status_code", 400),
                                  "Некоректний запит.")
        if isinstance(exc, (Http404, drf_exc.NotFound)):
            return error_response(request, "not_found", 404, "Не знайдено.")
        if isinstance(exc, MeetingError):
            return error_response(request, "invalid_request", 400, str(exc)[:300])
        # Текст винятку не віддається (у ньому можуть бути дані наради) — лише в журнал сервера
        logger.exception("meetings API error (%s)", _request_id(request))
        return error_response(request, "internal_error", 500, "Внутрішня помилка Minerva — повторіть пізніше.", True)


class MeetingRedeemView(MeetingsBenchView):
    def post(self, request):
        """Обміняти одноразовий квиток вікна запису на нараду (назва, мови, тексти згоди) і ключ надсилання."""
        data = read_body(request)
        result = services.redeem(data.get("ticket"))
        return Response({"meeting": result["meeting"], "push_token": result["push_token"]})


class MeetingStatusView(MeetingsBenchView):
    def post(self, request, meeting_id):
        """Стан запису наради від RAG-ПК: запис зі згодою, записано, розшифровка, готово, помилка."""
        result = services.push_status(meeting_id, push_token(request), read_body(request))
        return Response({"ok": True, "applied": bool(result.get("applied")), "status": result.get("status", "")})


class MeetingTranscriptView(MeetingsBenchView):
    def post(self, request, meeting_id):
        """Частина розшифровки наради (upload_id, part з parts, segments)."""
        result = services.push_transcript(meeting_id, push_token(request), read_body(request))
        out = {"ok": True, "complete": bool(result.get("complete"))}
        for name in ("missing", "segments"):
            if isinstance(result.get(name), int):
                out[name] = result[name]
        return Response(out)


class MeetingMomentsView(MeetingsBenchView):
    def post(self, request, meeting_id):
        """Ключові моменти наради, знайдені локальною моделлю на RAG-ПК."""
        result = services.push_moments(meeting_id, push_token(request), read_body(request))
        return Response({"ok": True, "added": int(result.get("added") or 0), "skipped": int(result.get("skipped") or 0),
                         "known": int(result.get("known") or 0)})


class MeetingSyncView(MeetingsBenchView):
    def post(self, request):
        """Звірка записів нарад на RAG-ПК: що лишити, видалити, повторити, де прибрати аудіо."""
        data = read_body(request)
        sessions = data.get("sessions", data.get("entries", []))
        if sessions is None:
            sessions = []
        if not isinstance(sessions, list):
            raise Refusal("invalid_request", 400, "sessions — має бути список.", details={"field": "sessions"})
        bench = data.get("bench")
        result = services.sync(sessions, bench if isinstance(bench, dict) else None)
        return Response({"sessions": [{k: e.get(k) for k in ("meeting", "uid", "session", "action")}
                                      for e in result]})


class MeetingPingView(MeetingsBenchView):
    action = "get"                       # для прав, журналу й «🔍 Що бачить ключ»: GET, але право meetings:write
    action_scopes = {"get": "write"}

    def get(self, request):
        """Перевірка ключа RAG-ПК для нарад (потрібне право meetings:write): ok і права ключа."""
        return Response({"ok": True, "scopes": [str(s) for s in (request.auth.scopes or [])]})
