"""
email_assistant/drafts.py — чернетки листів (EmailDraft).

Звідки беруться: «💾 Зберегти» на сторінці «✏️ Написати», AI-автовідповідь у режимі «Зберегти як чернетку»
(sync_email), «Нове замовлення → чернетка листа» (signals.py). Видно в скриньці — «📝 Чернетки»; відкривається
у «✏️ Написати» (?draft=<pk>), звідти — відправити, запланувати або зберегти ще раз (та сама чернетка).

- save: створити або оновити ту саму чернетку — нічого не надсилає; вкладення — attachments.from_form
  (файли з комп'ютера — у MEDIA, документи Minerva — посиланням);
- for_account / count: чернетки скриньки (лише власника — скринька користувача), пошук;
- chips: вкладення чернетки для форми листа («draft:<pk>:<i>» — файл, інакше саме посилання);
- discard: чернетку надіслано / заплановано / видалено — запис і її файли прибираються (сигнал post_delete).
"""
import html as _html

from django.db.models import Q

from email_assistant import attachments
from email_assistant.models import EmailDraft


def addresses(raw) -> list:
    return [e.strip() for e in (raw or '').split(',') if e.strip()]


def for_account(account, q=''):
    qs = EmailDraft.objects.filter(account=account)
    q = (q or '').strip()
    if q:
        qs = qs.filter(Q(subject__icontains=q) | Q(to_emails__icontains=q) | Q(cc_emails__icontains=q)
                       | Q(body__icontains=q) | Q(body_html__icontains=q))
    return qs.select_related('reply_to').order_by('-updated_at', '-pk')


def count(account) -> int:
    return EmailDraft.objects.filter(account=account).count() if account else 0


def get(account, pk):
    if account is None or not str(pk or '').isdigit():
        return None
    return EmailDraft.objects.filter(pk=int(pk), account=account).select_related('reply_to').first()


def save(account, *, to='', cc='', subject='', body='', body_html='', reply_to=None,
         refs=(), names=(), files=(), draft=None) -> EmailDraft:
    """«💾 Зберегти»: нова чернетка або оновлення тієї самої (draft). Лист не надсилається.
    Порожня чернетка (ні адреси, ні теми, ні тексту, ні вкладень) — ValueError."""
    if not (addresses(to) or addresses(cc) or (subject or '').strip() or (body or '').strip()
            or list(refs or []) or list(files or [])):
        raise ValueError('Порожній лист — нічого зберігати')
    items, error = attachments.from_form(account, list(refs or []), list(names or []), list(files or []),
                                         draft=draft)
    if error:
        raise ValueError(error)
    old = list(draft.attachments or []) if draft is not None else []
    if draft is None:
        draft = EmailDraft(account=account, source=EmailDraft.SOURCE_MANUAL)
    draft.to_emails = addresses(to)
    draft.cc_emails = addresses(cc)
    draft.subject = (subject or '').strip()[:500]
    draft.body = body or ''
    draft.body_html = body_html or ''
    draft.reply_to = reply_to
    draft.attachments = items
    draft.save()
    attachments.remove_stored(old, keep=items)   # прибрані з чернетки файли — з диска
    return draft


def chips(draft) -> list:
    """Вкладення чернетки для форми листа: [{ref, name, size}]."""
    out = []
    for i, item in enumerate(draft.attachments or []):
        if not isinstance(item, dict):
            continue
        if item.get('file'):
            out.append({'ref': f'draft:{draft.pk}:{i}', 'name': item.get('name') or 'attachment',
                        'size': item.get('size') or 0})
        elif item.get('ref'):
            out.append({'ref': item['ref'], 'name': item.get('name') or item['ref'].rsplit(':', 1)[-1], 'size': 0})
    return out


def text_html(text) -> str:
    """Текст чернетки AI (без HTML) → для редактора листа."""
    return _html.escape(text or '').replace('\n', '<br>')


def preview(draft, limit=220) -> str:
    """Рядок тексту для списку чернеток."""
    text = draft.body or ''
    if not text.strip() and draft.body_html:
        from email_assistant.imap_client import html_to_text
        text = html_to_text(draft.body_html)
    return ' '.join(text.split())[:limit]


def discard(account, pk) -> bool:
    """Чернетку надіслано / заплановано / видалено → прибрати (файли — сигнал post_delete)."""
    draft = get(account, pk)
    if draft is None:
        return False
    draft.delete()
    return True
