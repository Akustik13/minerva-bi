"""📎 Файли для зовнішнього агента (MCP — agent/tools_files.py, REST API — api/files.py): знайти, завантажити,
прочитати, передати в Minerva, вкласти в лист (propose_email `attachments`), покласти до замовлення / проєкту / клієнта.

Файл для агента — посилання (ref), як у вкладеннях пошти (email_assistant/attachments.py):
  order:<pk замовлення>:<назва>   документ теки замовлення media/orders/… (sales.view_salesorder)
  invoice:<pk>                     🧾 рахунок PDF (sales.view_salesinvoice)
  offer:<pk версії>                📝 документ версії Angebot (offers.view_offer); у листі клієнту — «надіслано»
  rfq:<pk> / rfqreply:<pk>         📨 файл запиту постачальникам / 📥 файл від постачальника
                                   (suppliers.view_supplierrequest)
  kb:<pk>                          📚 документ бази знань (knowledge_items.can_view)
  cust:<pk клієнта>:<назва>        📎 документ клієнта media/customers/<pk>/ (crm.view_customer)
  msg:<pk листа>:<номер>           вкладення листа СВОЇХ скриньок — лише з 🔒 особистим ключем (personal)
  datasheet:<артикул> / photo:<артикул>   даташит / фото товару (inventory.view_product)
  upload:<pk>                      файл, який агент передав у Minerva (AgentFile) — лише власник
Кожне відкриття перевіряє права людини ключа заново (`open_file`); чуже, неіснуюче, без права — FileError.

Посилання без ключа (агент з оболонкою — curl): `link_token` → GET /api/v1/files/link/<token>/ (завантажити,
`LINK_SECONDS`), `upload_token` → POST /api/v1/files/upload/<token>/ (передати файл, `UPLOAD_SECONDS`). Підписані
SECRET_KEY (django.core.signing), при використанні ключ і людина перевіряються ще раз.
"""
import datetime
import hashlib
import logging
import mimetypes
import os
import re
import tempfile

from django.conf import settings
from django.core import signing
from django.utils import timezone

logger = logging.getLogger(__name__)

MAX_BYTES = 25 * 1024 * 1024          # файл для завантаження / читання / вивантаження
B64_MAX_BYTES = 10 * 1024 * 1024      # upload_file з base64 через MCP (JSON-RPC)
TEXT_MAX = 2 * 1024 * 1024            # upload_file з текстом
MAIL_MAX_FILES = 10
MAIL_MAX_BYTES = 25 * 1024 * 1024     # усі вкладення листа разом
KEEP_DAYS = 30                        # скільки лежить файл від агента (пропозиція з ним подовжує)
LINK_SECONDS = 3600                   # посилання на завантаження
UPLOAD_SECONDS = 1800                 # посилання для вивантаження
LIST_MAX = 40
READ_CHARS = 40000
DL_SALT, UP_SALT = 'agent-file-dl', 'agent-file-up'
# Файли, які не вкладаємо й не приймаємо: програми й скрипти (лист із ними — підозрілий, поштовики відкидають)
BLOCKED_EXT = {'exe', 'bat', 'cmd', 'com', 'scr', 'pif', 'vbs', 'vbe', 'js', 'jse', 'wsf', 'wsh', 'ps1', 'msi',
               'msp', 'jar', 'dll', 'lnk', 'hta', 'cpl', 'reg', 'scf', 'sh'}
IMAGE_TYPES = ('image/png', 'image/jpeg', 'image/gif', 'image/webp')
# Посилання, які лишаються посиланнями й у листі (документ читається під час відправки, Angebot / рахунок —
# «надіслано» після відправки); решта копіюється у вкладення чернетки при ✓ (email_assistant.attachments.store_file).
KEEP_REF = ('order', 'invoice', 'offer', 'rfq', 'rfqreply', 'kb')
KINDS = ('order', 'invoice', 'offer', 'rfq', 'rfqreply', 'kb', 'cust', 'msg', 'datasheet', 'photo', 'upload')
REF_RE = re.compile(r'^([a-z]+):(.+)$')


class FileError(Exception):
    pass


def _perm(user, perm, what):
    if user is None or not getattr(user, 'is_active', False) or not user.has_perm(perm):
        raise FileError(f'немає права на {what} в Minerva')


def _ctype(name, ctype='') -> str:
    ctype = str(ctype or '').split(';')[0].strip().lower()
    if ctype and ctype not in ('application/octet-stream', 'binary/octet-stream'):
        return ctype[:100]
    return mimetypes.guess_type(name or '')[0] or 'application/octet-stream'


def ext_of(name) -> str:
    name = str(name or '').lower()
    return name.rsplit('.', 1)[-1] if '.' in name else ''


def human_size(n) -> str:
    n = int(n or 0)
    if n >= 1024 * 1024:
        return f'{n / 1024 / 1024:.1f} МБ'
    if n >= 1024:
        return f'{n / 1024:.0f} КБ'
    return f'{n} Б'


def _read_path(path) -> bytes:
    if os.path.getsize(path) > MAX_BYTES:
        raise FileError(f'файл більший за {MAX_BYTES // 1024 // 1024} МБ')
    with open(path, 'rb') as fh:
        return fh.read()


def _inside(folder, name):
    """Шлях файлу `name` (лише basename) у теці `folder`; інакше FileError."""
    name = os.path.basename(str(name or '').replace('\\', '/'))
    if not name or name.startswith('.'):
        raise FileError('такого файлу немає')
    root = os.path.realpath(folder)
    path = os.path.realpath(os.path.join(root, name))
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        raise FileError('такого файлу немає')
    return path, name


def customer_dir(pk) -> str:
    return os.path.join(str(settings.MEDIA_ROOT), 'customers', str(int(pk)))


def _product(sku):
    from inventory.models import Product
    from inventory.services.stock import resolve_sku
    sku = str(sku or '').strip()
    product = Product.objects.filter(sku__iexact=sku).first() or (resolve_sku(sku) if sku else None)
    if product is None:
        raise FileError(f'товару «{sku}» немає на складі Minerva')
    return product


# ── Відкрити файл за посиланням ───────────────────────────────────────────────

def parse(ref):
    m = REF_RE.match(' '.join(str(ref or '').split()))
    if not m or m.group(1) not in KINDS:
        raise FileError(f'невідоме посилання на файл «{str(ref)[:80]}» — беріть ref з list_files')
    return m.group(1), m.group(2)


def open_file(user, ref, personal=False):
    """(назва, байти, тип) файлу за посиланням — лише якщо людина має право його бачити; інакше FileError."""
    kind, rest = parse(ref)
    try:
        out = _OPEN[kind](user, rest, personal)
    except FileError:
        raise
    except (ValueError, IndexError, OSError) as e:
        logger.info('agent file %s: %s', ref, e)
        raise FileError('такого файлу немає') from None
    if out is None:
        raise FileError('такого файлу немає')
    name, data, ctype = out
    if len(data) > MAX_BYTES:
        raise FileError(f'файл більший за {MAX_BYTES // 1024 // 1024} МБ')
    return name, data, _ctype(name, ctype)


def _open_order(user, rest, personal):
    from email_assistant.attachments import order_dir
    from sales.models import SalesOrder
    _perm(user, 'sales.view_salesorder', 'замовлення')
    pk, _, name = rest.partition(':')
    order = SalesOrder.objects.filter(pk=int(pk)).first()
    if order is None:
        raise FileError('такого замовлення немає')
    path, name = _inside(order_dir(order), name)
    return name, _read_path(path), ''


def _open_invoice(user, rest, personal):
    from sales.invoice_service import attachment
    _perm(user, 'sales.view_salesinvoice', 'рахунки')
    att = attachment(int(rest))
    return (att['name'], att['content'], att['content_type']) if att else None


def _open_offer(user, rest, personal):
    from offers.services import revision_attachment
    _perm(user, 'offers.view_offer', 'Angebote')
    att = revision_attachment(int(rest))
    return (att['name'], att['content'], att['content_type']) if att else None


def _open_rfq(model_name):
    def opener(user, rest, personal):
        from suppliers import models as sm
        _perm(user, 'suppliers.view_supplierrequest', 'запити постачальникам')
        f = getattr(sm, model_name).objects.filter(pk=int(rest)).first()
        if f is None or not f.file:
            return None
        with f.file.open('rb') as fh:
            data = fh.read(MAX_BYTES + 1)
        return f.name, data, getattr(f, 'content_type', '')
    return opener


def _open_kb(user, rest, personal):
    from rag_assistant import knowledge_items as ki
    from rag_assistant.models import KnowledgeItem
    item = KnowledgeItem.objects.filter(pk=int(rest)).select_related('knowledge_source').first()
    if item is None or not ki.can_view(user, item):
        raise FileError('такого документа бази знань немає або він вам не видимий')
    path = ki.file_path(item) if ki.has_file(item) else ''
    if not path:
        raise FileError('у документа бази знань немає файлу (посилання на сторінку — відкрийте його адресу)')
    name = item.file_name or os.path.basename(path)
    return name, _read_path(path), item.content_type


def _open_cust(user, rest, personal):
    from crm.models import Customer
    _perm(user, 'crm.view_customer', 'клієнтів CRM')
    pk, _, name = rest.partition(':')
    if not Customer.objects.filter(pk=int(pk)).exists():
        raise FileError('такого клієнта немає')
    path, name = _inside(customer_dir(pk), name)
    return name, _read_path(path), ''


def _own_message(user, pk):
    from email_assistant.models import EmailMessage
    msg = EmailMessage.objects.filter(pk=int(pk), account__user=user, is_deleted=False).select_related('account') \
        .first()
    if msg is None:
        raise FileError('листа з таким id немає у ваших скриньках')
    return msg


def _open_msg(user, rest, personal):
    from email_assistant.attachments import attachment_bytes
    if not personal:
        raise FileError('вкладення пошти — лише через 🔒 особистий ключ (агент — сам власник скриньки)')
    pk, _, index = rest.partition(':')
    msg = _own_message(user, pk)
    atts = msg.attachments or []
    i = int(index)
    if i < 0 or i >= len(atts) or not isinstance(atts[i], dict):
        raise FileError(f'у листа #{msg.pk} немає вкладення №{index}')
    data = attachment_bytes(msg.account, msg, i)
    if data is None:
        raise FileError('вкладення не вдалося взяти з поштового сервера')
    return atts[i].get('name') or 'attachment', data, atts[i].get('content_type')


def _open_datasheet(user, rest, personal):
    from agent.tools import ToolError, _datasheet_bytes
    _perm(user, 'inventory.view_product', 'товари')
    product = _product(rest)
    try:
        data, _source = _datasheet_bytes(product)
    except ToolError as e:
        raise FileError(f'даташит {product.sku}: {e}') from None
    return f'{product.sku}-datasheet.pdf', data, 'application/pdf'


def _open_photo(user, rest, personal):
    from inventory.services import media_files
    _perm(user, 'inventory.view_product', 'товари')
    product = _product(rest)
    if product.image and product.image.name:
        try:
            data = media_files.read_field(product.image, media_files.IMAGE)
            return os.path.basename(product.image.name), data, ''
        except media_files.MediaError as e:
            logger.info('agent photo file %s: %s', product.sku, e)
    if product.image_url:
        try:
            data = media_files.fetch(product.image_url, media_files.IMAGE)
        except media_files.MediaError as e:
            raise FileError(f'фото {product.sku}: {e}') from None
        name = os.path.basename(product.image_url.split('?')[0]) or f'{product.sku}.jpg'
        return name, data, ''
    raise FileError(f'у товару {product.sku} немає фото')


def _open_upload(user, rest, personal):
    from agent.models import AgentFile
    f = AgentFile.objects.filter(pk=int(rest), owner=user).first()
    if f is None or f.expires_at <= timezone.now() or not f.file:
        raise FileError('такого файлу немає (або його вже прибрано — файли від агента лежать '
                        f'{KEEP_DAYS} днів)')
    with f.file.open('rb') as fh:
        data = fh.read(MAX_BYTES + 1)
    return f.name, data, f.content_type


_OPEN = {'order': _open_order, 'invoice': _open_invoice, 'offer': _open_offer, 'rfq': _open_rfq('SupplierRequestFile'),
         'rfqreply': _open_rfq('SupplierReplyFile'), 'kb': _open_kb, 'cust': _open_cust, 'msg': _open_msg,
         'datasheet': _open_datasheet, 'photo': _open_photo, 'upload': _open_upload}


def describe(user, ref, personal=False) -> dict:
    """{ref, name, size, content_type} — файл справді відкривається для людини (інакше FileError)."""
    name, data, ctype = open_file(user, ref, personal)
    return {'ref': ' '.join(str(ref).split()), 'name': name, 'size': len(data), 'content_type': ctype}


# ── Які файли є ───────────────────────────────────────────────────────────────

def _row(ref, name, size=0, what=''):
    return {'ref': ref, 'name': name, 'size': int(size or 0), 'what': what}


def _kb_rows(user, kind, obj):
    try:
        from rag_assistant import knowledge_items as ki
    except ImportError:
        return []
    out = []
    for item in ki.items_for(user, kind, obj, limit=LIST_MAX):
        if ki.has_file(item):
            out.append(_row(f'kb:{item.pk}', item.file_name or item.title, item.file_size,
                            '📚 документ бази знань' + (f' «{item.title}»' if item.title != item.file_name else '')))
    return out


def find_order(user, value):
    """Замовлення за номером («102497269», «джерело:номер») або #pk."""
    from sales.models import SalesOrder
    _perm(user, 'sales.view_salesorder', 'замовлення')
    value = ' '.join(str(value or '').split())
    m = re.fullmatch(r'#(\d{1,10})', value)
    if m:
        found = list(SalesOrder.objects.filter(pk=int(m.group(1)))[:1])
    else:
        source, _, number = value.rpartition(':') if ':' in value else ('', '', value)
        qs = SalesOrder.objects.filter(order_number__iexact=number)
        if source:
            qs = qs.filter(source__iexact=source)
        found = list(qs.order_by('-pk')[:6])
    if not found:
        raise FileError(f'замовлення «{value}» немає')
    if len(found) > 1:
        raise FileError('кілька замовлень з цим номером: ' + '; '.join(f'{o.source}:{o.order_number}' for o in found)
                        + ' — вкажіть «джерело:номер»')
    return found[0]


def find_customer(user, value):
    from agent.rag import _customer
    from agent.tools import ToolError
    try:
        return _customer(user, value)
    except ToolError as e:
        raise FileError(str(e)) from None


def find_project(user, value):
    from agent.rag import _project
    from agent.tools import ToolError
    try:
        return _project(user, value)
    except ToolError as e:
        raise FileError(str(e)) from None


def order_rows(user, order) -> list:
    from email_assistant.attachments import order_documents
    rows = [_row(d['ref'], d['name'], d['size'], '📂 документ замовлення') for d in order_documents(order)]
    if user.has_perm('sales.view_salesinvoice'):
        try:
            from sales.invoice_service import file_base
            from sales.models_invoice import SalesInvoice
            for inv in SalesInvoice.objects.filter(sales_order=order).exclude(pdf_file='').order_by('-pk')[:10]:
                rows.append(_row(f'invoice:{inv.pk}', f'{file_base(inv)}.pdf', 0, f'🧾 рахунок {inv.number}'))
        except Exception as e:  # noqa: BLE001 — без розділу рахунків
            logger.info('agent files invoices %s: %s', order.pk, e)
    return rows + _kb_rows(user, 'order', order)


def product_rows(user, product) -> list:
    rows = []
    if (product.datasheet_file and product.datasheet_file.name) or product.datasheet_url:
        rows.append(_row(f'datasheet:{product.sku}', f'{product.sku}-datasheet.pdf', 0, '📄 даташит товару'))
    if (product.image and product.image.name) or product.image_url:
        rows.append(_row(f'photo:{product.sku}', os.path.basename((product.image.name if product.image else '')
                                                                  or product.image_url.split('?')[0]) or product.sku,
                         0, '🖼 фото товару'))
    return rows + _kb_rows(user, 'product', product)


def request_rows(user, request) -> list:
    from suppliers.models import SupplierReplyFile
    rows = [_row(f'rfq:{f.pk}', f.name, f.size, f'📨 файл запиту {request.code}') for f in request.files.all()]
    for f in SupplierReplyFile.objects.filter(recipient__request=request).select_related('recipient__supplier') \
            .order_by('-pk')[:LIST_MAX]:
        who = getattr(f.recipient.supplier, 'name', '') if f.recipient_id else ''
        rows.append(_row(f'rfqreply:{f.pk}', f.name, f.size, '📥 від постачальника' + (f' {who}' if who else '')))
    return rows


def project_rows(user, project) -> list:
    rows = _kb_rows(user, 'project', project)
    if user.has_perm('suppliers.view_supplierrequest'):
        try:
            from suppliers.models import SupplierRequest
            for req in SupplierRequest.objects.filter(project=project).order_by('-pk')[:5]:
                rows += request_rows(user, req)
        except Exception as e:  # noqa: BLE001
            logger.info('agent files project rfq %s: %s', project.pk, e)
    return rows


def customer_rows(user, customer) -> list:
    rows = []
    try:
        entries = sorted((e for e in os.scandir(customer_dir(customer.pk)) if e.is_file()
                          and not e.name.startswith('.')), key=lambda e: e.stat().st_mtime, reverse=True)
    except OSError:
        entries = []
    for e in entries[:LIST_MAX]:
        rows.append(_row(f'cust:{customer.pk}:{e.name}', e.name, e.stat().st_size, '📎 документ клієнта'))
    return rows + _kb_rows(user, 'customer', customer)


def mail_rows(user, msg) -> list:
    return [_row(f'msg:{msg.pk}:{i}', a.get('name') or 'attachment', a.get('size'),
                 '🖼 картинка в тексті листа' if a.get('is_inline') else '📎 вкладення листа')
            for i, a in enumerate(msg.attachments or []) if isinstance(a, dict)]


def offer_rows(user, offer) -> list:
    rows = []
    for rev in offer.revisions.order_by('-revision')[:5]:
        f = rev.pdf_file or rev.docx_file
        if f:
            ext = 'pdf' if rev.pdf_file else 'docx'
            rows.append(_row(f'offer:{rev.pk}', f'{rev.file_name}.{ext}', 0,
                             f'📝 Angebot {offer.number} версія {rev.revision}'
                             + (' · надіслано' if rev.sent_at else '')))
    return rows


def upload_rows(user) -> list:
    from agent.models import AgentFile
    return [_row(f.ref, f.name, f.size, f'⬆ передано агентом {timezone.localtime(f.created_at):%d.%m %H:%M}')
            for f in AgentFile.objects.filter(owner=user, expires_at__gt=timezone.now())[:LIST_MAX]]


def list_files(user, personal=False, *, order='', product='', project='', request='', mail='', customer='',
               offer='', uploads=False) -> tuple:
    """(що, [рядки]) — файли одного об'єкта Minerva, які людина бачить."""
    if str(order or '').strip():
        o = find_order(user, order)
        return f'замовлення {o.source}:{o.order_number}', order_rows(user, o)
    if str(product or '').strip():
        _perm(user, 'inventory.view_product', 'товари')
        p = _product(product)
        return f'товар {p.sku}', product_rows(user, p)
    if str(project or '').strip():
        p = find_project(user, project)
        return f'проєкт {p.code}', project_rows(user, p)
    if str(request or '').strip():
        _perm(user, 'suppliers.view_supplierrequest', 'запити постачальникам')
        from suppliers.models import SupplierRequest
        code = ' '.join(str(request).split())
        req = SupplierRequest.objects.filter(code__iexact=code).first() or (
            SupplierRequest.objects.filter(pk=int(code.lstrip('#'))).first() if code.lstrip('#').isdigit() else None)
        if req is None:
            raise FileError(f'запиту «{code}» немає — потрібен код RFQ-РРРР-NNN')
        return f'запит {req.code}', request_rows(user, req)
    if str(mail or '').strip():
        if not personal:
            raise FileError('пошта — лише через 🔒 особистий ключ (агент — сам власник скриньки)')
        try:
            msg = _own_message(user, int(str(mail).strip().lstrip('#')))
        except ValueError:
            raise FileError('mail — id листа (число)') from None
        return f'лист #{msg.pk}', mail_rows(user, msg)
    if str(customer or '').strip():
        c = find_customer(user, customer)
        return f'клієнт #{c.pk} {c.company or c.name}', customer_rows(user, c)
    if str(offer or '').strip():
        _perm(user, 'offers.view_offer', 'Angebote')
        from offers.models import Offer
        number = ' '.join(str(offer).split())
        o = Offer.objects.filter(number__iexact=number).first()
        if o is None:
            raise FileError(f'Angebot «{number}» немає')
        return f'Angebot {o.number}', offer_rows(user, o)
    if uploads:
        return 'файли, передані агентом', upload_rows(user)
    raise FileError('вкажіть, чиї файли: order, product, project, request, mail, customer, offer або uploads')


# ── Передати файл у Minerva ───────────────────────────────────────────────────

def check_name(name) -> str:
    from email_assistant.attachments import safe_name
    name = os.path.basename(str(name or '').replace('\\', '/')).strip()[:200]
    if not name:
        raise FileError('потрібна назва файлу з розширенням (напр. «offer.pdf»)')
    if ext_of(name) in BLOCKED_EXT:
        raise FileError(f'файли «.{ext_of(name)}» (програми й скрипти) Minerva не приймає')
    return safe_name(name)


def save(user, name, data: bytes, content_type='', *, key=None, source='mcp', url=''):
    """Файл → AgentFile (upload:<pk>)."""
    from django.core.files.base import ContentFile

    from agent.models import AgentFile
    name = check_name(name)
    if not data:
        raise FileError('порожній файл')
    if len(data) > MAX_BYTES:
        raise FileError(f'файл більший за {MAX_BYTES // 1024 // 1024} МБ')
    from email_assistant.imap_client import sniff_content_type
    ctype = _ctype(name, content_type)
    sniffed = sniff_content_type(data)
    if sniffed and ctype in ('application/octet-stream', 'binary/octet-stream'):
        ctype = sniffed
    f = AgentFile(owner=user, api_key=key if getattr(key, 'pk', None) else None, name=name, content_type=ctype,
                  size=len(data), sha256=hashlib.sha256(data).hexdigest(), source=source, url=str(url or '')[:500],
                  expires_at=timezone.now() + datetime.timedelta(days=KEEP_DAYS))
    f.file.save(name, ContentFile(data), save=False)
    f.save()
    return f


def save_from_url(user, url, name='', *, key=None):
    """Публічна адреса в інтернеті → AgentFile (лише публічні адреси, кожна переадресація перевіряється)."""
    from rag_assistant.knowledge_items import ItemError, _download
    try:
        final, data, ctype = _download(str(url or '').strip())
    except ItemError as e:
        raise FileError(str(e)) from None
    if not name:
        name = os.path.basename(final.split('?')[0].split('#')[0]) or 'download'
        if '.' not in name:
            name += mimetypes.guess_extension(ctype or '') or ''
    return save(user, name, data, ctype, key=key, source='url', url=final)


def keep(refs):
    """Пропозиція з файлами від агента — вони лежать ще KEEP_DAYS від сьогодні."""
    from agent.models import AgentFile
    pks = [int(r.split(':', 1)[1]) for r in refs if str(r).startswith('upload:') and r.split(':', 1)[1].isdigit()]
    if pks:
        AgentFile.objects.filter(pk__in=pks).update(
            expires_at=timezone.now() + datetime.timedelta(days=KEEP_DAYS))


def cleanup() -> int:
    """Прибрати прострочені файли від агента (cron agent_runs)."""
    from agent.models import AgentFile
    n = 0
    for f in AgentFile.objects.filter(expires_at__lte=timezone.now())[:500]:
        try:
            if f.file:
                path = f.file.path
                f.file.delete(save=False)
                try:
                    os.rmdir(os.path.dirname(path))
                except OSError:
                    pass
        except (OSError, ValueError, NotImplementedError) as e:
            logger.info('agent file cleanup %s: %s', f.pk, e)
        f.delete()
        n += 1
    return n


def cleanup_due() -> int:
    """Раз на годину (спільний кеш) — з cron agent_runs."""
    from agent.models import AgentFile
    from core.shared_cache import cache
    if not cache.add('agent-files-cleanup', 1, 3600):
        return 0
    if not AgentFile.objects.filter(expires_at__lte=timezone.now()).exists():
        return 0
    return cleanup()


# ── Текст файлу ───────────────────────────────────────────────────────────────

def read_text(name, data, first_page=None, last_page=None) -> str:
    """Текст файлу для агента: PDF — по сторінках, DOCX / TXT / CSV / XLSX / HTML / MD."""
    ext = ext_of(name)
    if ext == 'pdf' or data[:4] == b'%PDF':
        from agent.tools import ToolError, _pdf_pages
        try:
            pages = _pdf_pages(data)
        except ToolError as e:
            return f'{e} — get_file дасть сам файл.'
        if not pages:
            return 'PDF без тексту (скан?) — прочитати не можна; get_file дасть сам файл.'
        a = max(1, int(first_page or 1))
        b = min(len(pages), int(last_page or len(pages)))
        out, size = [], 0
        for n in range(a, b + 1):
            chunk = f'— сторінка {n} з {len(pages)} —\n{pages[n - 1]}'
            if size + len(chunk) > READ_CHARS:
                out.append(f'… далі — first_page={n}')
                break
            out.append(chunk)
            size += len(chunk)
        return '\n\n'.join(out)
    if ext in ('png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp'):
        return 'Це картинка — get_file покаже її.'
    readers = {'docx', 'txt', 'csv', 'xlsx', 'html', 'htm', 'md', 'json', 'xml'}
    if ext not in readers:
        return f'Файл «.{ext or "?"}» як текст не читається — get_file дасть сам файл.'
    if ext in ('txt', 'csv', 'md', 'json', 'xml'):
        text = data.decode('utf-8-sig', errors='replace')
        return text[:READ_CHARS] + ('…' if len(text) > READ_CHARS else '')
    fd, path = tempfile.mkstemp(suffix='.' + ext)
    try:
        with os.fdopen(fd, 'wb') as fh:
            fh.write(data)
        if ext == 'docx':
            from email_assistant.reply_brief import read_attachment_text
            text = read_attachment_text({'name': name}, READ_CHARS, path)
        else:
            from rag_assistant.knowledge_sources import extract_text
            text = extract_text(path, 'html' if ext == 'htm' else ext)
    except Exception as e:  # noqa: BLE001 — зламаний файл: агент бачить причину
        logger.info('agent read_file %s: %s', name, e)
        return f'Файл не вдалося прочитати ({type(e).__name__}).'
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    text = text or ''
    return (text[:READ_CHARS] + ('…' if len(text) > READ_CHARS else '')) or 'Тексту у файлі немає.'


# ── Підписані посилання ───────────────────────────────────────────────────────

def link_token(user, key, ref, personal=False) -> str:
    return signing.dumps({'u': user.pk, 'k': getattr(key, 'pk', None), 'r': ref, 'p': bool(personal)},
                         salt=DL_SALT, compress=True)


def upload_token(user, key, personal=False) -> str:
    return signing.dumps({'u': user.pk, 'k': getattr(key, 'pk', None), 'p': bool(personal)}, salt=UP_SALT)


def _person(data):
    """(людина, ключ, personal) з підписаного посилання — ключ і людина ще чинні."""
    from django.contrib.auth import get_user_model

    from api.models import APIKey
    user = get_user_model().objects.filter(pk=data.get('u'), is_active=True).first()
    if user is None:
        raise FileError('посилання недійсне')
    key = None
    if data.get('k'):
        key = APIKey.objects.filter(pk=data['k'], is_active=True).first()
        if key is None or key.agent_user_id != user.pk or (key.expires_at and key.expires_at < timezone.localdate()):
            raise FileError('ключ API цього посилання вимкнено або прострочено')
    return user, key, bool(data.get('p'))


def from_link(token):
    """GET /api/v1/files/link/<token>/ → (людина, ключ, ref, personal)."""
    try:
        data = signing.loads(token, salt=DL_SALT, max_age=LINK_SECONDS)
    except signing.SignatureExpired:
        raise FileError('посилання застаріло — попросіть агента get_file ще раз') from None
    except signing.BadSignature:
        raise FileError('посилання недійсне') from None
    user, key, personal = _person(data)
    return user, key, data.get('r'), personal


def from_upload(token):
    """POST /api/v1/files/upload/<token>/ → (людина, ключ)."""
    try:
        data = signing.loads(token, salt=UP_SALT, max_age=UPLOAD_SECONDS)
    except signing.SignatureExpired:
        raise FileError('посилання для вивантаження застаріло — попросіть upload_link ще раз') from None
    except signing.BadSignature:
        raise FileError('посилання недійсне') from None
    user, key, _personal = _person(data)
    return user, key


def site_base(ctx=None) -> str:
    base = getattr(ctx, 'base_url', '') if ctx is not None else ''
    if base:
        return base.rstrip('/')
    try:
        from config.models import SystemSettings
        return (SystemSettings.get().site_url or '').rstrip('/')
    except Exception:  # noqa: BLE001
        return ''
