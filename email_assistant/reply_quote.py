"""
email_assistant/reply_quote.py — відповідь як справжня відповідь у гілці листування.

Цитата листа, на який відповідаємо (історія листування під текстом, як в Outlook / Thunderbird): форма «Написати»
ставить її в текст сама, а швидка відповідь у прев'ю листа й на сторінці гілки — галочкою «📜 З історією
листування» (`with_quote` у `views._handle_send`). Ланцюжок References (усі листи гілки до цього) — щоб поштова
програма клієнта поставила відповідь у ту саму розмову (`smtp_client.SMTPClient.send`).
"""
import html as _html

from django.utils import timezone

MARKER = '-------- Original Message --------'   # quotes.new_text обриває тут: для AI й аналізу — лише новий текст
QUOTE_MAX = 20000
REFS_MAX = 20


def _when(msg) -> str:
    return timezone.localtime(msg.sent_at).strftime('%d.%m.%Y %H:%M') if msg.sent_at else ''


def _sender(msg) -> str:
    name, address = (msg.from_name or '').strip(), (msg.from_email or '').strip()
    return f'{name} <{address}>' if name and name.lower() != address.lower() else address


def header_lines(msg) -> list:
    """[(From, …), (Sent, …), (To, …), (Cc, …), (Subject, …)] — без порожніх."""
    rows = [('From', _sender(msg)), ('Sent', _when(msg)), ('To', ', '.join(msg.to_emails or [])),
            ('Cc', ', '.join(msg.cc_emails or [])), ('Subject', msg.subject or '')]
    return [(k, v) for k, v in rows if v]


def quote_text(msg) -> str:
    body = (msg.body_text or '').strip()
    if not body and msg.body_html:
        from email_assistant.imap_client import html_to_text
        body = html_to_text(msg.body_html).strip()
    head = '\n'.join(f'{k}: {v}' for k, v in header_lines(msg))
    return f'\n\n{MARKER}\n{head}\n\n{body[:QUOTE_MAX]}'


def quote_html(msg) -> str:
    """Цитата для HTML-листа: кольори без змінних теми (лист читає інша програма), чужий HTML — лише дозволене."""
    from email_assistant.html_clean import clean_html
    inner = clean_html(msg.body_html) if msg.body_html else ''
    if not inner.strip():
        inner = f'<div style="white-space:pre-wrap">{_html.escape((msg.body_text or "")[:QUOTE_MAX])}</div>'
    head = '<br>'.join(f'<b>{_html.escape(k)}:</b> {_html.escape(v)}' for k, v in header_lines(msg))
    return ('<div style="margin-top:16px;padding-top:8px;border-top:1px solid #b5c4df;'
            'font-family:Arial,sans-serif;font-size:13px;color:#333333">' + head + '</div>'
            '<blockquote style="margin:8px 0 0 4px;padding:0 0 0 10px;border-left:2px solid #b5c4df">'
            + inner + '</blockquote>')


def with_quote(reply_to, body_text: str, body_html: str):
    """(текст, HTML) відповіді з історією листування під ним; без HTML — HTML з тексту."""
    text = (body_text or '').rstrip() + quote_text(reply_to)
    html_part = body_html or '<div>' + _html.escape(body_text or '').replace('\n', '<br>') + '</div>'
    return text, html_part + quote_html(reply_to)


def references(reply_to) -> str:
    """References відповіді: листи гілки до цього за часом, потім лист, на який він відповідав, і він сам
    (RFC 5322: References батька + його Message-ID). Без Message-ID — ''."""
    own = (reply_to.message_id or '').strip()
    if not own:
        return ''
    parent = (reply_to.in_reply_to or '').strip()
    ids = []
    if reply_to.thread_id:
        from email_assistant.models import EmailMessage
        qs = EmailMessage.objects.filter(thread_id=reply_to.thread_id).exclude(message_id='')
        if reply_to.sent_at:
            qs = qs.filter(sent_at__lte=reply_to.sent_at)
        for mid in qs.order_by('sent_at', 'pk').values_list('message_id', flat=True)[:500]:
            mid = (mid or '').strip()
            if mid and mid not in ids and mid not in (own, parent):
                ids.append(mid)
    if parent:
        ids.append(parent)
    return ' '.join(ids[-(REFS_MAX - 1):] + [own])
