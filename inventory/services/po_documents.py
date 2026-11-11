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
"""
import datetime
import hashlib
import logging
import os
import re

from django.core.files.base import ContentFile
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

FILE_MAX_MB = 25
ALLOWED = {'pdf', 'doc', 'docx', 'xls', 'xlsx', 'xlsm', 'odt', 'ods', 'rtf', 'csv', 'txt', 'xml', 'zip',
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
