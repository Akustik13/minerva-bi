"""📨 Оригінал листа — як він прийшов (або пішов): сирий лист з сервера пошти (IMAP BODY.PEEK[] — без позначки
«прочитано»), з текстом, картинками й усіма вкладеннями. Лист без копії на сервері (надіслано з Minerva й ще не
синхронізовано, імпорт .eml) або сервер не відповів — зібраний з того, що є в Minerva (текст, HTML, файли на диску;
файлів, яких на диску немає, — `missing`).

Пересилання «як вкладення» (.eml, message/rfc822 — так пересилає кілька листів Outlook / Thunderbird):
suppliers/services/rfq_talk.forward_originals (↪ «Переслати колезі» → «📨 Листи як є»).

  fetch(msgs, report)   → {pk: Original(data, rebuilt, missing)}: одне з'єднання IMAP на скриньку; лист на сервері
                          перевіряється за Message-ID (UID міг змінитись) — інакше збирається з Minerva;
  rebuild(msg)          → (bytes, missing) — лист з даних Minerva;
  estimate(msg)         → приблизний розмір без сервера (для плану «скількома листами піде»);
  filename(msg, prefix) → «HongRong (shenzhen) - 2026-11-19 10-30 Anduin Chen - RE RFQ-2026-001.eml» (збережені
                          в теку — за постачальником, далі за датою);
  attachment(name, data)→ вкладення SMTPClient: message/rfc822, 7bit / 8bit — не base64 (RFC 2046: вкладений лист
                          так не кодується, і поштові програми відкривають його як звичайний лист).
"""
import logging
import os
import re
from collections import namedtuple

logger = logging.getLogger(__name__)

Original = namedtuple("Original", "data rebuilt missing")

NAME_MAX = 100               # символів у назві .eml (без розширення)
_BAD_NAME_RE = re.compile(r'[\x00-\x1f\\/:*?"<>|]+')
_LOCAL_PREFIXES = ("minerva:", "eml:")   # копії без листа на сервері


def on_server(msg) -> bool:
    """Чи є в листа копія на сервері пошти, яку можна взяти як є."""
    name = msg.imap_folder_name or ""
    return bool(msg.imap_uid) and msg.account_id is not None and not name.startswith(_LOCAL_PREFIXES)


def _norm_id(value) -> str:
    return "".join(str(value or "").split()).strip("<>").lower()


def _same(data, msg) -> bool:
    """Сервер віддав саме цей лист: Message-ID збігається (або його немає з якогось боку)."""
    if not msg.message_id:
        return True
    from email.parser import BytesHeaderParser
    try:
        head = BytesHeaderParser().parsebytes(data[:262144])
    except Exception:  # noqa: BLE001 — зіпсований заголовок: краще зібрати з Minerva
        return False
    got = head.get("Message-ID")
    return not got or _norm_id(got) == _norm_id(msg.message_id)


def fetch(msgs, report=None) -> dict:
    """Оригінали листів → {pk: Original}. report(текст) — хід для сторінки («📥 3 / 16»)."""
    from email_assistant.attachments import message_bytes
    from email_assistant.imap_client import IMAPClient
    msgs = list(msgs)
    out, groups = {}, {}
    for m in msgs:
        if on_server(m):
            groups.setdefault(m.account_id, []).append(m)
    done = 0
    for items in groups.values():
        account = items[0].account
        try:
            with IMAPClient(account) as imap:
                current = None
                for m in sorted(items, key=lambda x: (x.imap_folder_name or "", x.imap_uid)):
                    folder = m.imap_folder_name or account.imap_folder_inbox or "INBOX"
                    try:
                        if folder != current:
                            current = folder if imap.select_folder(folder) else None
                        if current is None:
                            continue
                        _, raw = imap.conn.uid("fetch", str(m.imap_uid).encode(), "(BODY.PEEK[])")
                        data = message_bytes(raw)
                        if data and _same(data, m):
                            out[m.pk] = Original(data, False, [])
                    except Exception:  # noqa: BLE001 — один лист не взявся: його зберемо з Minerva
                        logger.info("originals: fetch %s failed", m.pk, exc_info=True)
                        current = None
                    finally:
                        done += 1
                        if report:
                            report(f"📥 Беру листи з пошти: {done} / {len(msgs)}")
        except Exception:  # noqa: BLE001 — сервер не відповів: усі листи скриньки — з Minerva
            logger.warning("originals: IMAP %s failed", account.pk, exc_info=True)
    for m in msgs:
        if m.pk not in out:
            data, missing = rebuild(m)
            out[m.pk] = Original(data, True, missing)
    return out


def _file_bytes(att):
    from django.conf import settings
    rel = str(att.get("file_path") or "")
    if not rel:
        return None
    root = os.path.realpath(str(settings.MEDIA_ROOT))
    path = os.path.realpath(os.path.join(root, rel))
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _set(mime, name, value):
    from email_assistant.smtp_client import one_line
    value = one_line(value or "")
    if not value:
        return
    try:
        mime[name] = value
    except Exception:  # noqa: BLE001 — дивний заголовок чужої пошти не зупиняє пересилання
        logger.info("originals: header %s skipped", name)


def rebuild(msg) -> tuple:
    """Лист із даних Minerva: заголовки, текст, HTML, файли з диска (без IMAP). → (bytes, [назви без файлу])."""
    import mimetypes
    from email.message import EmailMessage as Mime
    from email.policy import SMTP
    from email.utils import format_datetime, formataddr
    from email_assistant.imap_client import html_to_text
    mime = Mime(policy=SMTP)
    sender = formataddr((msg.from_name, msg.from_email)) if msg.from_name and msg.from_email else msg.from_email
    _set(mime, "From", sender)
    for header, value in (("To", msg.to_emails), ("Cc", msg.cc_emails)):
        if isinstance(value, list) and value:
            _set(mime, header, ", ".join(str(a) for a in value if a))
    _set(mime, "Subject", msg.subject)
    if msg.sent_at:
        _set(mime, "Date", format_datetime(msg.sent_at))
    _set(mime, "Message-ID", msg.message_id)
    _set(mime, "In-Reply-To", msg.in_reply_to)
    html = msg.body_html or ""
    mime.set_content(msg.body_text or (html_to_text(html) if html else "") or " ")
    if html:
        mime.add_alternative(html, subtype="html")
    missing = []
    for att in msg.attachments or []:
        if not isinstance(att, dict) or not att.get("name"):
            continue
        data = _file_bytes(att)
        if data is None:
            missing.append(att["name"])
            continue
        ctype = str(att.get("content_type") or "") or mimetypes.guess_type(att["name"])[0] or ""
        maintype, _, subtype = ctype.partition("/")
        if not maintype or not subtype or maintype in ("multipart", "message"):
            maintype, subtype = "application", "octet-stream"
        cid = str(att.get("content_id") or "").strip("<>")
        mime.add_attachment(data, maintype=maintype, subtype=subtype, filename=att["name"],
                            disposition="inline" if att.get("is_inline") else "attachment",
                            cid=f"<{cid}>" if cid else None)
    return mime.as_bytes(), missing


def estimate(msg) -> int:
    """Приблизний розмір листа на сервері: текст + HTML + вкладення в base64 (~ × 1,37) + заголовки."""
    size = len((msg.body_text or "").encode("utf-8")) + len((msg.body_html or "").encode("utf-8")) + 4096
    size += sum(int(a.get("size") or 0) for a in msg.attachments or [] if isinstance(a, dict)) * 137 // 100
    return size


def filename(msg, prefix="") -> str:
    from django.utils import timezone
    when = timezone.localtime(msg.sent_at).strftime("%Y-%m-%d %H-%M") if msg.sent_at else ""
    who = msg.from_name or msg.from_email or ""
    name = " - ".join(p for p in (prefix, " ".join(p for p in (when, who) if p), msg.subject or "") if p)
    name = " ".join(_BAD_NAME_RE.sub(" ", name).split()).strip(" .") or f"mail {msg.pk}"
    return name[:NAME_MAX].rstrip(" .") + ".eml"


def attachment(name, data) -> dict:
    """Лист як вкладення (message/rfc822) для SMTPClient.send — без base64 (cte 7bit / 8bit)."""
    return {"name": name, "content": data, "content_type": "message/rfc822",
            "cte": "7bit" if data.isascii() else "8bit"}
