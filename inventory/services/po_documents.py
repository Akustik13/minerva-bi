"""📎 Документи закупівлі (inventory.PurchaseOrderDocument) за категоріями: запит, пропозиція, наше замовлення,
підтвердження, рахунок, оплата, відвантаження, митниця, сертифікати, рекламація, інше.

• `guess_category(name, subject='')` — без AI, за назвою файлу (і темою листа): «Invoice.pdf» → рахунок,
  «Zahlungsbestätigung» → оплата, «Lieferschein» → відвантаження …
• `add(po, name, data, category, …)` — файл у MEDIA/purchase_orders/<закупівля>/<категорія>/; той самий вміст у цій
  закупівлі вже є (sha256) або те саме вкладення листа (`source_ref`) — не дублює.
• `mail_candidates(po, user, q, days)` — листи ВЛАСНИХ скриньок з вкладеннями про цю закупівлю: від постачальника
  (адреса, домен — крім поштовиків), його адреса в тексті (переслав колега), код закупівлі, трек-номер, номер
  замовлення постачальника в темі чи тексті; q — пошук за темою / відправником.
• `add_from_mail(po, user, picks)` — вибрані вкладення {«<лист>:<номер>»: категорія} → документи (лише свої листи).
• `attach_message(po, msg, user, source)` — усі документи-вкладення листа (агент після ✓ закупівлі / посилки).
• `summary(po)` — групи за категоріями й стан «🧾 рахунок є — 💳 оплати немає».

🔎 Що замовляли (`PurchaseOrder.ordered_skus`) — артикули з документів, без AI:
• `detect(po, docs, catalog)` → [(артикул, звідки)]: назва документа й файлу, назви файлів у ZIP-архіві (Gerber —
  «TR240401-01.zip», «TR240401-01.GTL»), тема листа, з якого документ, текст PDF / DOCX / Excel / CSV / TXT (рахунок).
  Артикул каталогу або аліас (`Catalog`: точний збіг, без розділювачів, «TR240401-01-gerber» → TR240401-01) — звідусіль;
  схожий на наш артикул без каталогу (`CODE_RE`: TR240401-01, AN220213-01C) — лише з назв (архіву — і після «-»), не з
  тексту; номери документів (`NOT_CODE`: INV…, PI…, IMG…), код закупівлі, номер постачальника й трек-номер — ні.
• `update_ordered(po, docs)` — нове дописується в поле (виправлене людиною лишається); викликають `add`, збереження
  документів у картці, кнопка «🔎 Визначити з документів» і дія списку.
"""
import datetime
import hashlib
import logging
import os
import re
import zipfile

from django.core.files.base import ContentFile
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

FILE_MAX_MB = 25
ALLOWED = {'pdf', 'doc', 'docx', 'xls', 'xlsx', 'xlsm', 'odt', 'ods', 'rtf', 'csv', 'txt', 'xml', 'zip', 'rar', '7z',
           'png', 'jpg', 'jpeg', 'gif', 'webp', 'tif', 'tiff', 'heic', 'eml', 'msg'}
IMAGES = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
LOGO_MAX = 20 * 1024            # картинка менша — логотип з підпису, не документ
MAIL_DAYS = 120
MAIL_MAX = 40
PICK_MAX = 30
_BAD_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]')

# Порядок важливий: «Payment receipt» — оплата, а не «receipt»; «Order confirmation» — підтвердження, а не замовлення.
RULES = [
    ('payment', r'payment|zahlung|überweisung|ueberweisung|remittance|swift|paid|bezahlt|оплат|платіж|платеж|'
                r'платіжк|квитанц'),
    ('claim', r'credit[ _-]?note|gutschrift|\brma\b|\breturn|reklamation|complaint|retoure|повернен|рекламац|'
              r'кредит-нот'),
    ('customs', r'customs|zoll|\babd\b|atlas|declaration|eust|einfuhr|\bt1\b|cn ?2[23]|митн|деклараці'),
    ('confirmation', r'confirm|bestätigung|bestaetigung|auftragsbest|\bab[ _-]?\d|\boc[ _-]?\d|підтвердж'),
    ('invoice', r'invoice|rechnung|proforma|pro-forma|faktura|factura|\binv[ _-]?\d|bill\b|рахун|счет|счёт|інвойс'),
    ('quote', r'quotation|\bquote|angebot|offer|price[ _-]?list|preisliste|прайс|пропозиці|предложени'),
    ('rfq', r'\brfq\b|request for quot|anfrage|запит'),
    ('shipping', r'packing|lieferschein|delivery[ _-]?note|waybill|\bawb\b|shipping|dispatch|label|tracking|'
                 r'versand|накладн|відвантаж|доставк'),
    ('quality', r'\bcoc\b|certificate|zertifikat|rohs|reach|test[ _-]?report|inspection|prüfbericht|сертифік'),
    ('order', r'purchase[ _-]?order|bestellung|\bpo[ _-]?\d|order|замовлен'),
]
_RULES = [(cat, re.compile(rx, re.IGNORECASE)) for cat, rx in RULES]


def guess_category(name, subject='') -> str:
    """Категорія за назвою файлу, а не вийшло — за темою листа; нічого — «інше»."""
    from inventory.models import PurchaseOrderDocument as D
    for text in (name or '', subject or ''):
        text = text.replace('_', ' ')
        for cat, rx in _RULES:
            if rx.search(text):
                return cat
    return D.OTHER


def _ext(name) -> str:
    name = name or ''
    return name.rsplit('.', 1)[-1].lower() if '.' in name else ''


def allowed(name) -> bool:
    return _ext(name) in ALLOWED


def safe_name(name) -> str:
    name = os.path.basename((name or '').replace('\\', '/'))
    name = _BAD_CHARS.sub('_', name)
    name = re.sub(r'\.{2,}', '.', name).strip(' .') or 'document'
    stem, dot, ext = name.rpartition('.')
    return (stem[:140].rstrip(' .') + '.' + ext[:10]) if dot else name[:150]


def add(po, name, data, category=None, *, source=None, message=None, user=None, ref='', doc_date=None, note=''):
    """Новий документ закупівлі → (документ, створено?). Той самий вміст / те саме вкладення — наявний документ."""
    from inventory.models import PurchaseOrderDocument as D
    if not data:
        raise ValueError('порожній файл')
    if len(data) > FILE_MAX_MB * 1024 * 1024:
        raise ValueError(f'файл більший за {FILE_MAX_MB} МБ')
    name = safe_name(name)
    if not allowed(name):
        raise ValueError(f'«{name}» — такий тип файлу не приймаємо')
    sha = hashlib.sha256(data).hexdigest()
    same = po.documents.filter(Q(sha256=sha) | (Q(source_ref=ref) if ref else Q(pk__in=[]))).first()
    if same is not None:
        return same, False
    doc = D(purchase_order=po, category=category or guess_category(name), title=name[:200], source=source or D.UPLOAD,
            source_ref=ref[:40], email_message=message, sha256=sha, uploaded_by=user, note=note[:255],
            doc_date=doc_date)
    doc.file.save(name, ContentFile(data), save=False)
    doc.save()
    try:
        update_ordered(po, [doc])
    except Exception as e:  # noqa: BLE001 — документ уже додано, артикули — додатково
        logger.warning('po %s: ordered skus from %s: %s', po.pk, name, e)
    return doc, True


# ── 📥 З пошти ────────────────────────────────────────────────────────────────

def _documents(msg) -> list:
    """[(номер, вкладення)] — документи листа: не вбудовані, дозволеного типу, не логотип підпису."""
    out = []
    for i, att in enumerate(msg.attachments or []):
        if not isinstance(att, dict) or att.get('is_inline') or not allowed(att.get('name')):
            continue
        if _ext(att.get('name')) in IMAGES and (att.get('size') or 0) < LOGO_MAX:
            continue
        out.append((i, att))
    return out


def _supplier_domain(po) -> str:
    from email_assistant.crm_link import FREE_MAIL_DOMAINS
    email = (po.supplier.email or '').strip().lower()
    domain = email.split('@', 1)[1] if '@' in email else ''
    return '' if domain in FREE_MAIL_DOMAINS else domain


def _tokens(po) -> list:
    return [t for t in (po.code, po.tracking_number, po.supplier_order_no) if t and len(t.strip()) >= 4]


def mail_candidates(po, user, q='', days=MAIL_DAYS) -> list:
    """[{msg, why, atts: [{index, name, size, category, added}]}] — свіжі першими."""
    from email_assistant.models import EmailMessage
    since = timezone.now() - datetime.timedelta(days=days)
    qs = EmailMessage.objects.filter(account__user=user, is_deleted=False, is_spam=False, sent_at__gte=since)
    q = ' '.join(str(q or '').split())[:100]
    if q:
        qs = qs.filter(Q(subject__icontains=q) | Q(from_email__icontains=q) | Q(from_name__icontains=q))
    else:
        email = (po.supplier.email or '').strip()
        domain = _supplier_domain(po)
        cond = Q(pk__in=[])
        if email:
            cond |= Q(from_email__iexact=email) | Q(body_text__icontains=email)
        if domain:
            cond |= Q(from_email__iendswith='@' + domain) | Q(body_text__icontains='@' + domain)
        for token in _tokens(po):
            cond |= Q(subject__icontains=token) | Q(body_text__icontains=token)
        qs = qs.filter(cond)
    added = set(po.documents.exclude(source_ref='').values_list('source_ref', flat=True))
    rows = []
    for msg in qs.order_by('-sent_at')[:300]:
        docs = _documents(msg)
        if not docs:
            continue
        why = _why(po, msg)
        rows.append({'msg': msg, 'why': why, 'atts': [
            {'index': i, 'name': a.get('name') or '', 'size': a.get('size') or 0,
             'category': guess_category(a.get('name'), msg.subject), 'added': f'msg:{msg.pk}:{i}' in added}
            for i, a in docs]})
        if len(rows) >= MAIL_MAX:
            break
    return rows


def _why(po, msg) -> str:
    frm = (msg.from_email or '').lower()
    email = (po.supplier.email or '').strip().lower()
    domain = _supplier_domain(po)
    text = f'{msg.subject or ""}\n{msg.body_text or ""}'
    if email and frm == email:
        return 'від постачальника'
    if domain and frm.endswith('@' + domain):
        return 'з домену постачальника'
    for token in _tokens(po):
        if token.lower() in text.lower():
            return f'згадано {token}'
    if email and email in text.lower():
        return 'переслано (адреса постачальника в тексті)'
    return ''


def add_from_mail(po, user, picks) -> dict:
    """picks — {«<лист>:<номер>»: категорія}; лише листи своїх скриньок. → {added, same, failed: [текст]}."""
    from email_assistant.attachments import attachment_bytes
    from email_assistant.models import EmailMessage
    from inventory.models import PurchaseOrderDocument as D
    out = {'added': 0, 'same': 0, 'failed': []}
    valid = dict(D.CATEGORIES)
    for key, category in list(picks.items())[:PICK_MAX]:
        m = re.fullmatch(r'(\d+):(\d+)', str(key))
        if not m:
            continue
        msg = EmailMessage.objects.filter(pk=int(m.group(1)), account__user=user).select_related('account').first()
        idx = int(m.group(2))
        atts = msg.attachments if msg is not None else []
        if msg is None or not (0 <= idx < len(atts or [])) or not isinstance(atts[idx], dict):
            out['failed'].append(f'{key}: вкладення не знайдено у ваших листах')
            continue
        name = atts[idx].get('name') or 'document'
        try:
            data = attachment_bytes(msg.account, msg, idx)
            _doc, created = add(po, name, data, category if category in valid else None, source=D.MAIL, message=msg,
                                user=user, ref=f'msg:{msg.pk}:{idx}',
                                doc_date=timezone.localtime(msg.sent_at).date() if msg.sent_at else None)
        except Exception as e:  # noqa: BLE001 — одне вкладення не зупиняє решту
            out['failed'].append(f'«{name}»: {e}'[:200])
            continue
        out['added' if created else 'same'] += 1
    return out


def attach_message(po, msg, user, source=None) -> int:
    """Усі документи-вкладення листа → документи закупівлі (агент після ✓). → скільки додано нових."""
    from email_assistant.attachments import attachment_bytes
    from inventory.models import PurchaseOrderDocument as D
    if msg is None:
        return 0
    added = 0
    for idx, att in _documents(msg):
        try:
            data = attachment_bytes(msg.account, msg, idx)
            _doc, created = add(po, att.get('name') or 'document', data,
                                guess_category(att.get('name'), msg.subject), source=source or D.AGENT,
                                message=msg, user=user, ref=f'msg:{msg.pk}:{idx}',
                                doc_date=timezone.localtime(msg.sent_at).date() if msg.sent_at else None)
            added += created
        except Exception as e:  # noqa: BLE001 — закупівля вже є, документ — додатково
            logger.warning('po %s: attachment %s of mail %s: %s', po.pk, idx, msg.pk, e)
    return added


# ── Підсумок ─────────────────────────────────────────────────────────────────

def summary(po) -> dict:
    """{groups: [(категорія, назва, [документи])], has: {категорія}, unpaid: рахунок є, оплати немає}."""
    from inventory.models import PurchaseOrderDocument as D
    docs = list(po.documents.all())
    by = {}
    for d in docs:
        by.setdefault(d.category, []).append(d)
    groups = [(cat, label, by[cat]) for cat, label in D.CATEGORIES if cat in by]
    return {'groups': groups, 'has': set(by), 'count': len(docs),
            'unpaid': D.INVOICE in by and D.PAYMENT not in by}


# ── 🔎 Що замовляли ──────────────────────────────────────────────────────────

ARCHIVES = {'zip', 'rar', '7z'}
ARCHIVE_NAMES_MAX = 500          # назв файлів з одного ZIP-архіву
TEXT_TYPES = {'pdf', 'docx', 'txt', 'csv', 'xlsx', 'xlsm'}
TEXT_CHARS = 20000
TEXT_MAX_MB = 15
ORDERED_MAX = 500                # довжина поля «Що замовляли»
# Схоже на наш артикул: TR240401-01, AN220213-01C — 1–4 літери, 4–8 цифр, «-», 1–3 цифри, до 3 літер.
# Перед ним у назві документа / темі листа — не літера, не цифра й не «-» (PI-HR241105-01 — номер рахунку),
# у назві архіву й файлів у ньому «-» можна (Gerber-TR240401-01.zip).
CODE_RE = re.compile(r'(?<![A-Za-z0-9-])([A-Za-z]{1,4}\d{4,8}-\d{1,3}[A-Za-z]{0,3})(?![A-Za-z0-9])')
ARCHIVE_CODE_RE = re.compile(r'(?<![A-Za-z0-9])([A-Za-z]{1,4}\d{4,8}-\d{1,3}[A-Za-z]{0,3})(?![A-Za-z0-9])')
# Номери документів і файли з телефона / сканера, а не артикули: INV240401-01, IMG20241105-01 …
NOT_CODE = {'INV', 'PI', 'CI', 'PO', 'OC', 'AB', 'RE', 'DN', 'PL', 'SO', 'ORD', 'NR', 'NO', 'ID', 'IMG', 'DSC', 'SCAN',
            'SCN', 'DOC', 'FILE', 'PIC', 'VID', 'WA', 'PXL', 'RMA', 'CN', 'AWB', 'REF', 'QT', 'QUO', 'RFQ', 'TRK'}
_TOKEN_SPLIT = re.compile(r'[^A-Za-z0-9.\-]+')
_SEP = re.compile(r'[^A-Za-z0-9]+')
_LIST_SPLIT = re.compile(r'[,;\n]+')


def _key(code) -> str:
    return _SEP.sub('', code or '').upper()


def ordered_codes(value) -> list:
    """«TR240401-01, AN220213-01C» → ['TR240401-01', 'AN220213-01C']."""
    return [c.strip() for c in _LIST_SPLIT.split(value or '') if c.strip()]


class Catalog:
    """Артикули й аліаси складу — один запит на всі закупівлі: {ВЕЛИКИМИ: артикул} і {без розділювачів: артикул}.
    Лише з літерою й цифрою, від 5 знаків (інакше «PCB», «2024» в тексті рахунку схожі на артикул)."""

    def __init__(self):
        from inventory.models import Product, ProductAlias
        self.exact, self.norm = {}, {}
        for sku in Product.objects.exclude(sku='').values_list('sku', flat=True):
            self._add(sku, sku)
        for alias, sku in ProductAlias.objects.values_list('alias', 'product__sku'):
            if alias and sku:
                self._add(alias, sku)

    def _add(self, code, sku):
        key = _key(code)
        if len(key) < 5 or not re.search(r'[A-Z]', key) or not re.search(r'\d', key):
            return
        self.exact.setdefault(code.strip().upper(), sku)
        self.norm.setdefault(key, sku)

    def find(self, code):
        """Точний артикул / аліас (без огляду на регістр і розділювачі) → артикул каталогу або None."""
        return self.exact.get((code or '').strip().upper()) or self.norm.get(_key(code))

    def match(self, token):
        """Слово з назви чи тексту → артикул каталогу. Хвіст відрізається лише на «.» (розширення, версія) і на «-»
        перед літерою («TR240401-01-gerber»), а не перед цифрою — «AN220213-01C» не стане «AN220213»."""
        t = (token or '').strip('.-').upper()
        if len(t) < 5:
            return None
        cuts = [len(t)] + [i for i in range(len(t) - 1, 3, -1)
                           if t[i] == '.' or (t[i] == '-' and i + 1 < len(t) and t[i + 1].isalpha())]
        for i in cuts:
            sku = self.exact.get(t[:i]) or self.norm.get(_key(t[:i]))
            if sku:
                return sku
        return None


class _Found:
    def __init__(self, catalog, skip):
        self.catalog, self.skip = catalog, skip
        self.rows, self.keys = [], set()

    def _put(self, code, where, known):
        key = _key(code)
        if not key or key in self.keys or key in self.skip:
            return
        self.keys.add(key)
        self.rows.append((code, where, known))

    def scan(self, text, where, *, pattern=True, archive=False):
        if not text:
            return
        for token in _TOKEN_SPLIT.split(text):
            sku = self.catalog.match(token)
            if sku:
                self._put(sku, where, True)
        if not pattern:
            return
        for m in (ARCHIVE_CODE_RE if archive else CODE_RE).finditer(text):
            code = m.group(1).upper()
            if re.match(r'[A-Z]+', code).group(0) in NOT_CODE:
                continue
            sku = self.catalog.find(code)
            self._put(sku or code, where, bool(sku))

    def result(self) -> list:
        """[(артикул, звідки)]; схожий на артикул, що є лише початком артикула каталогу («AN220213-01C» з
        «AN220213-01C-175-MHF1»), — не окремо."""
        known = [_key(c) for c, _w, k in self.rows if k]
        return [(c, w) for c, w, k in self.rows
                if k or not any(o != _key(c) and o.startswith(_key(c)) for o in known)]


def _zip_names(doc) -> list:
    try:
        with doc.file.open('rb') as fh, zipfile.ZipFile(fh) as z:
            return [n for n in z.namelist() if not n.startswith('__MACOSX')][:ARCHIVE_NAMES_MAX]
    except Exception as e:  # noqa: BLE001 — пошкоджений / не ZIP: лише назва архіву
        logger.info('po document %s: zip not readable: %s', doc.pk, e)
        return []


def _text(doc, ext) -> str:
    """Текст документа (рахунок, підтвердження) — лише щоб знайти артикули каталогу."""
    try:
        path = doc.file.path
        if os.path.getsize(path) > TEXT_MAX_MB * 1024 * 1024:
            return ''
        if ext in ('xlsx', 'xlsm'):
            from openpyxl import load_workbook
            wb = load_workbook(path, read_only=True, data_only=True)
            parts, size = [], 0
            for ws in wb.worksheets[:5]:
                for row in ws.iter_rows(max_row=400, values_only=True):
                    line = ' '.join(str(v) for v in row if v not in (None, ''))
                    parts.append(line)
                    size += len(line)
                    if size > TEXT_CHARS:
                        break
            wb.close()
            return '\n'.join(parts)[:TEXT_CHARS]
        from email_assistant.reply_brief import read_attachment_text
        return read_attachment_text({'name': 'x.' + ext}, limit=TEXT_CHARS, path=path) or ''
    except Exception as e:  # noqa: BLE001 — файл не читається: лише назва
        logger.info('po document %s: text not readable: %s', doc.pk, e)
        return ''


def detect(po, docs=None, catalog=None) -> list:
    """[(артикул, звідки)] — що замовляли, за документами закупівлі (див. початок модуля)."""
    catalog = catalog or Catalog()
    found = _Found(catalog, {_key(t) for t in _tokens(po)})
    if docs is None:
        docs = po.documents.select_related('email_message')
    for d in docs:
        name = d.file_name if d.file else ''
        ext = _ext(name) or _ext(d.title)
        archive = ext in ARCHIVES
        where = d.title or name
        found.scan(d.title, where, archive=archive)
        if name != d.title:
            found.scan(name, where, archive=archive)
        if ext == 'zip' and d.file:
            for inner in _zip_names(d):
                found.scan(inner, f'{where} → {inner}', archive=True)
        if d.email_message_id and d.email_message is not None:
            found.scan(d.email_message.subject, f'лист «{(d.email_message.subject or "")[:60]}»')
        if ext in TEXT_TYPES and d.file:
            found.scan(_text(d, ext), f'{where} (текст)', pattern=False)
    return found.result()


def merge(current, codes) -> str:
    """Дописати нові артикули в кінець; наявні (і виправлені людиною) лишаються, повтори — ні."""
    parts = ordered_codes(current)
    have = {_key(c) for c in parts}
    for code in codes:
        key = _key(code)
        if not key or key in have or len(', '.join(parts + [code])) > ORDERED_MAX:
            continue
        parts.append(code)
        have.add(key)
    return ', '.join(parts)


def update_ordered(po, docs=None, catalog=None) -> list:
    """Знайдене в документах → «Що замовляли» (лише дописує). → [(новий артикул, звідки)]."""
    from inventory.models import PurchaseOrder
    found = detect(po, docs, catalog)
    if not found:
        return []
    current = PurchaseOrder.objects.filter(pk=po.pk).values_list('ordered_skus', flat=True).first() or ''
    have = {_key(c) for c in ordered_codes(current)}
    new = [(c, w) for c, w in found if _key(c) not in have]
    value = merge(current, [c for c, _w in new])
    if value != current:
        PurchaseOrder.objects.filter(pk=po.pk).update(ordered_skus=value)   # без сигналів і вебхука
    po.ordered_skus = value
    added = {_key(c) for c in ordered_codes(value)} - have
    return [(c, w) for c, w in new if _key(c) in added]
