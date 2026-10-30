"""
tabele/media.py — роздача /media/ (nginx у стеку немає) з перевіркою доступу.

Публічні (їх відкривають листи-сповіщення, сайт магазину, RAG без сесії Minerva): фото й даташити товарів,
мініатюри, файли для сайту, логотип фірми. Решта — лише активному співробітнику з доступом до модуля, якому
належить тека (`user.has_module_perms` = ролі Minerva, core/permissions.py); вкладення листів — лише власнику
скриньки; файли бази знань — лише адміністратору (іншим — сторінка документа /rag/knowledge/<pk>/ з перевіркою
видимості). HTML / SVG / XML / JS ніколи не відкриваються сторінкою Minerva — лише завантаженням, з `nosniff` і
`Content-Security-Policy: sandbox` (інакше вкладення з чужого листа виконувало б скрипт від імені людини).
"""
import posixpath

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.http import Http404
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.static import serve

PUBLIC = ("products/", "thumbs/", "shop_media/", "config/", "accounting/logos/")
MODULES = (                          # тека → розділ Minerva (перший збіг)
    ("orders/", "sales"), ("offers/", "offers"), ("accounting/", "accounting"),
    ("invoices/", "shipping"), ("packing_lists/", "shipping"), ("commercial_invoices/", "shipping"),
    ("shipments/", "shipping"), ("shipping/", "shipping"), ("generated_documents/", "documents"), ("document_templates/", "documents"),
    ("scraper/", "scraper_hub"), ("rfq_diagrams/", "inventory"),
)
ADMIN_ONLY = ("knowledge/", "knowledge_sources/")
UNSAFE_TYPES = ("text/html", "image/svg+xml", "application/xhtml+xml", "text/xml", "application/xml",
                "text/javascript", "application/javascript", "application/x-javascript")


def _allowed(user, path: str) -> bool:
    from core.utils import is_minerva_admin
    if is_minerva_admin(user) and not path.startswith("email_attachments/"):
        return True
    if path.startswith("email_attachments/"):     # email_attachments/<скринька>/… — лише власник скриньки
        part = path.split("/")[1] if path.count("/") >= 2 else ""
        if not part.isdigit():
            return False
        from email_assistant.models import EmailAccount
        return EmailAccount.objects.filter(pk=int(part), user=user).exists()
    if path.startswith(ADMIN_ONLY):
        return False
    for prefix, app_label in MODULES:
        if path.startswith(prefix):
            return user.has_module_perms(app_label)
    return True                                     # інші теки (етикетки DYMO, чернетки листів…) — співробітник


@xframe_options_exempt                              # PDF у iframe попереднього перегляду (та сама адреса)
def media(request, path):
    path = posixpath.normpath(path).lstrip("/")
    if path.startswith("..") or path == ".":
        raise Http404
    if not path.startswith(PUBLIC):
        user = request.user
        if not (user.is_authenticated and user.is_active and user.is_staff):
            return redirect_to_login(request.get_full_path())
        if not _allowed(user, path):
            raise Http404
    response = serve(request, path, document_root=settings.MEDIA_ROOT)
    response["X-Content-Type-Options"] = "nosniff"
    ctype = (response.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype in UNSAFE_TYPES:
        response["Content-Disposition"] = "attachment"
        response["Content-Security-Policy"] = "sandbox"
    return response
