"""📎 Файли й 🔎 RAG-ПК для зовнішнього агента через REST API (`/api/v1/files/…`, `/api/v1/rag/…`).

Ті самі файли, права й посилання, що інструменти MCP list_files / get_file / upload_file / upload_link
(agent/files.py, agent/tools_files.py) і пошук RAG search_datasheets / search_history (agent/rag.py). Ключ — як для
MCP: «Authorization: Token <ключ>» або «Bearer <ключ>» (і токен 🔌 OAuth), права `agent:read` (список, завантаження,
пошук) і `agent:write` (передати файл), «🤖 Агент (MCP) — від імені кого» — людина, від чийого імені все робиться
(її права Minerva); вкладення пошти й пошук в історії пошти — лише 🔒 особистий ключ (агент — сам власник).

Посилання без ключа (`/files/link/<token>/`, `/files/upload/<token>/`) дає агент (get_file, upload_link) — підписані,
діють годину / 30 хв, ключ і людина перевіряються ще раз.
"""
import logging

from django.http import HttpResponse
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from .mcp import MCPKeyAuthentication
from .models import APIKey

logger = logging.getLogger(__name__)
LIST_PARAMS = ('order', 'product', 'project', 'request', 'mail', 'customer', 'offer')


def _person(request):
    """(ключ, людина, personal) — як MCP (api/mcp.MCPView._key_user)."""
    key = request.auth
    if not isinstance(key, APIKey):
        raise PermissionDenied('Потрібен ключ API (Authorization: Token <ключ>).')
    if key.shop_id:
        raise PermissionDenied('Ключ сайту магазину тут не підходить.')
    user = key.agent_user
    if user is None or not user.is_active:
        raise PermissionDenied('У ключа не вказано «🤖 Агент (MCP) — від імені кого».')
    if not user.has_perm('agent.add_agenttask'):
        raise PermissionDenied('Людина ключа не має права на завдання агенту.')
    from agent.mcp import personal_ok
    return key, user, personal_ok(key)


def _error(e, code=status.HTTP_400_BAD_REQUEST):
    return Response({'error': 'file_error', 'message': str(e)}, status=code)


def file_response(name, data, ctype):
    """Файл завантаженням: nosniff, CSP sandbox — браузер нічого з нього не виконує."""
    from django.utils.http import content_disposition_header
    resp = HttpResponse(data, content_type='application/octet-stream')
    resp['Content-Disposition'] = content_disposition_header(True, name or 'file')
    resp['X-Minerva-Content-Type'] = ctype or 'application/octet-stream'
    resp['X-Content-Type-Options'] = 'nosniff'
    resp['Content-Security-Policy'] = "sandbox; default-src 'none'"
    resp['Referrer-Policy'] = 'no-referrer'
    resp['Cache-Control'] = 'private, no-store'
    return resp


def _saved(f):
    return {'ref': f.ref, 'name': f.name, 'size': f.size, 'content_type': f.content_type,
            'expires_at': f.expires_at.isoformat()}


def _save_uploads(request, user, key, source):
    """Файли multipart «file» (кілька) або JSON {name, content_base64 | text | url} → [{ref…}] / FileError."""
    import base64
    import binascii

    from agent import files
    uploads = request.FILES.getlist('file') if hasattr(request, 'FILES') else []
    out = []
    if uploads:
        if len(uploads) > 20:
            raise files.FileError('до 20 файлів за раз')
        for up in uploads:
            if up.size > files.MAX_BYTES:
                raise files.FileError(f'«{up.name}» більший за {files.MAX_BYTES // 1024 // 1024} МБ')
            out.append(files.save(user, up.name, up.read(), getattr(up, 'content_type', '') or '', key=key,
                                  source=source))
        return out
    data = request.data if isinstance(request.data, dict) else {}
    given = [k for k in ('content_base64', 'text', 'url') if str(data.get(k) or '').strip()]
    if len(given) != 1:
        raise files.FileError('потрібен файл (multipart «file») або одне з: content_base64, text, url')
    if given[0] == 'url':
        return [files.save_from_url(user, data['url'], str(data.get('name') or ''), key=key)]
    if given[0] == 'text':
        content = str(data['text']).encode('utf-8')
        if len(content) > files.TEXT_MAX:
            raise files.FileError(f'текст більший за {files.TEXT_MAX // 1024 // 1024} МБ')
    else:
        raw = ''.join(str(data['content_base64']).split())
        if raw.startswith('data:') and ',' in raw:
            raw = raw.split(',', 1)[1]
        try:
            content = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            raise files.FileError('content_base64 — не base64') from None
    return [files.save(user, data.get('name'), content, str(data.get('content_type') or ''), key=key,
                       source=source)]


class FilesView(APIView):
    """GET — файли об'єкта Minerva (одне з: order, product, project, request, mail, customer, offer; або uploads=1);
    POST — передати файл у Minerva (multipart «file», кілька; або JSON {name, content_base64 | text | url})
    → ref «upload:<номер>» для вкладення в лист (MCP propose_email attachments) чи propose_save_file."""
    authentication_classes = [MCPKeyAuthentication]
    resource_scope = 'agent'

    def get(self, request):
        from agent import files
        key, user, personal = _person(request)
        q = request.query_params
        try:
            what, rows = files.list_files(user, personal, uploads=q.get('uploads') in ('1', 'true', 'yes'),
                                          **{p: q.get(p, '') for p in LIST_PARAMS})
        except files.FileError as e:
            return _error(e)
        base = request.build_absolute_uri('/').rstrip('/')
        for r in rows:
            r['download'] = f'{base}/api/v1/files/download/?ref={_q(r["ref"])}'
        return Response({'what': what, 'files': rows})

    def post(self, request):
        from agent import files
        key, user, _personal = _person(request)
        try:
            saved = _save_uploads(request, user, key, 'api')
        except files.FileError as e:
            return _error(e)
        return Response({'files': [_saved(f) for f in saved]}, status=status.HTTP_201_CREATED)


def _q(value):
    from urllib.parse import quote
    return quote(str(value), safe='')


class FileDownloadView(APIView):
    """GET ?ref=<ref> — сам файл (права людини ключа перевіряються заново)."""
    authentication_classes = [MCPKeyAuthentication]
    resource_scope = 'agent'

    def get(self, request):
        from agent import files
        key, user, personal = _person(request)
        try:
            name, data, ctype = files.open_file(user, request.query_params.get('ref', ''), personal)
        except files.FileError as e:
            return _error(e, status.HTTP_404_NOT_FOUND)
        return file_response(name, data, ctype)


class FileTextView(APIView):
    """GET ?ref=<ref>&first_page=&last_page= — текст файлу (PDF по сторінках, DOCX, XLSX, TXT, CSV, HTML)."""
    authentication_classes = [MCPKeyAuthentication]
    resource_scope = 'agent'

    def get(self, request):
        from agent import files
        key, user, personal = _person(request)
        q = request.query_params
        try:
            name, data, _ctype = files.open_file(user, q.get('ref', ''), personal)
            first = int(q['first_page']) if q.get('first_page') else None
            last = int(q['last_page']) if q.get('last_page') else None
        except files.FileError as e:
            return _error(e, status.HTTP_404_NOT_FOUND)
        except ValueError:
            return _error('first_page / last_page — числа')
        return Response({'ref': q.get('ref', ''), 'name': name, 'text': files.read_text(name, data, first, last)})


class LinkThrottle(SimpleRateThrottle):
    """Посилання без ключа — 120 на годину з однієї адреси."""
    scope = 'agent_file_link'
    rate = '120/hour'

    def get_cache_key(self, request, view):
        return self.cache_format % {'scope': self.scope, 'ident': self.get_ident(request)}


def _ip_check(request, key):
    """Посилання без ключа — з тих самих адрес, що й ключ («Дозволені IP-адреси», api/key_limits.py)."""
    from agent.files import FileError
    from .key_limits import client_ip, ip_allowed
    if key is not None and not ip_allowed(key, client_ip(request)):
        raise FileError('цей ключ не можна використовувати з вашої адреси')


class FileLinkView(APIView):
    """GET /files/link/<token>/ — файл за посиланням з get_file (без ключа; діє годину)."""
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [LinkThrottle]

    def get(self, request, token):
        from agent import files
        try:
            user, key, ref, personal = files.from_link(token)
            _ip_check(request, key)
            name, data, ctype = files.open_file(user, ref, personal)
        except files.FileError as e:
            return _error(e, status.HTTP_404_NOT_FOUND)
        return file_response(name, data, ctype)


class FileUploadLinkView(APIView):
    """POST /files/upload/<token>/ — передати файли за посиланням з upload_link (multipart «file»; без ключа)."""
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [LinkThrottle]

    def post(self, request, token):
        from agent import files
        try:
            user, key = files.from_upload(token)
            _ip_check(request, key)
            saved = _save_uploads(request, user, key, 'link')
        except files.FileError as e:
            return _error(e)
        return Response({'files': [_saved(f) for f in saved]}, status=status.HTTP_201_CREATED)


# ── 🔎 RAG-ПК ────────────────────────────────────────────────────────────────

class RagDatasheetsView(APIView):
    """GET ?q=&limit= — пошук по всіх даташитах і каталогу на RAG-ПК (помічник по продукції, без LLM): товари (і чи є
    вони в Minerva) та фрагменти — документ, сторінка, текст. Право людини «📦 Помічник по продукції»."""
    authentication_classes = [MCPKeyAuthentication]
    resource_scope = 'agent'

    def get(self, request):
        from agent import rag
        from agent.tools import ToolError
        key, user, _personal = _person(request)
        q = request.query_params
        try:
            query, result, limit = rag.run_datasheets(user, {'query': q.get('q') or q.get('query'),
                                                             'limit': q.get('limit')})
        except ToolError as e:
            return Response({'error': 'rag_error', 'message': str(e)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({'query': query, **rag.datasheets_data(result, limit)})


class RagHistoryView(APIView):
    """GET ?q=&customer=&project=&limit= — пошук в історії бази знань (листування ваших скриньок і документи бази
    знань, RAG `/v2`): лише перевірені Minerva фрагменти. Лише 🔒 особистий ключ і право «🔎 Пошук у базі знань»."""
    authentication_classes = [MCPKeyAuthentication]
    resource_scope = 'agent'

    def get(self, request):
        from agent import rag
        from agent.tools import ToolError
        key, user, personal = _person(request)
        if not personal:
            raise PermissionDenied('Історія пошти — лише через 🔒 особистий ключ (агент — сам власник скриньки).')
        q = request.query_params
        try:
            query, job, limit, scope = rag.run_history(user, {
                'query': q.get('q') or q.get('query'), 'customer': q.get('customer'), 'project': q.get('project'),
                'limit': q.get('limit')})
        except ToolError as e:
            return Response({'error': 'rag_error', 'message': str(e)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({'query': query, 'scope': scope, **rag.history_data(job, limit)})
