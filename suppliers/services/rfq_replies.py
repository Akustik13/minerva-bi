"""
suppliers/services/rfq_replies.py — 📥 Відповіді постачальника на запит (RFQ): листування рядка, вкладення → файли
запиту, швидкий огляд (панель праворуч на сторінці запиту, suppliers/admin.py panel_view).

  mail(r)              — листи рядка: гілка запиту (r.thread) і прив'язані пізніше (extra_threads) + наш лист і
                         відповідь, за датою (без видалених і спаму); threads(r), last_theirs_at(r);
  theirs(msg)          — лист від постачальника, не наш;
  wanted(att)          — вкладення, яке береться: не вбудоване в текст, не логотип підпису (картинка < LOGO_MAX);
  collect(r, fetch)    — вкладення листів постачальника → SupplierReplyFile (той самий вміст — раз). Файл уже на диску
                         (пошта зберігає документи сама) — одразу; немає — лише з fetch=True: IMAP, один запит на лист
                         (email_assistant.attachments.fetch_documents), шлях дописується в лист (UPDATE без сигналу).
                         → {saved, waiting (ще не забрано), error};
  snippet(msg, n)      — новий текст листа без цитат і підпису, коротко (підказка при наведенні, панель);
  can_read(msg, user)  — текст і тему листа бачить лише власник скриньки (як скрізь у Minerva);
  peek(r, user)        — підказка при наведенні на «✉️ відповідь»;
  panel(req, r, user, row) — усе для панелі рядка: пропозиція, вкладення, листування, історія.

Вкладення — документи запиту: їх бачать усі, хто бачить запит. Текст листів — лише власник скриньки.
"""
import hashlib
import logging
import os
import re

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q

logger = logging.getLogger(__name__)

LOGO_MAX = 20 * 1024              # картинки, менші за це, — логотипи підпису
FILE_MAX = 25 * 1024 * 1024
MAIL_MAX = 60                     # листів рядка в панелі
SNIPPET = 320
FULL_MAX = 6000                   # новий текст листа в панелі (розгорнути)
ICONS = (("pdf", "📕"), ("xls", "📗"), ("csv", "📗"), ("doc", "📘"), ("zip", "🗜"), ("rar", "🗜"), ("7z", "🗜"),
         ("png", "🖼"), ("jpg", "🖼"), ("jpeg", "🖼"), ("gif", "🖼"), ("webp", "🖼"), ("step", "📐"), ("stp", "📐"),
         ("dxf", "📐"), ("dwg", "📐"), ("gbr", "🟩"), ("ger", "🟩"))


def threads(r) -> list:
    """Гілки листування рядка: гілка запиту + прив'язані пізніше (окремий лист, інша адреса — rfq_match.link)."""
    out = [r.thread_id] if r.thread_id else []
    if r.pk:
        out += [t for t in r.extra_threads.values_list("pk", flat=True) if t not in out]
    return out


def last_theirs_at(r):
    """Коли прийшов найновіший лист постачальника в листуванні рядка (None — ще жодного)."""
    own = _own()
    times = [m.sent_at for m in mail(r) if m.sent_at and theirs(m, own)]
    return max(times) if times else None


def mail(r) -> list:
    from email_assistant.models import EmailMessage
    q = Q(pk__in=[x for x in (r.sent_message_id, r.reply_message_id) if x])
    th = threads(r)
    if th:
        q |= Q(thread_id__in=th)
    return list(EmailMessage.objects.filter(q).filter(is_deleted=False, is_spam=False)
                .select_related("account").order_by("sent_at", "pk")[:MAIL_MAX])


def _own():
    from email_assistant.crm_link import own_addresses
    return own_addresses()


def theirs(msg, own=None) -> bool:
    from email_assistant.crm_link import is_own
    if msg.folder == msg.FOLDER_SENT:
        return False
    return not is_own(msg.from_email, _own() if own is None else own)


def wanted(att) -> bool:
    if not isinstance(att, dict) or att.get("is_inline") or not att.get("name"):
        return False
    ctype = str(att.get("content_type") or "").lower()
    return not (ctype.startswith("image/") and (att.get("size") or 0) < LOGO_MAX)


def _local(att) -> str:
    path = att.get("file_path") or ""
    if not path:
        return ""
    full = os.path.join(str(settings.MEDIA_ROOT), path)
    return full if os.path.isfile(full) else ""


def collect(r, fetch=False) -> dict:
    from django.core.files.base import ContentFile
    from email_assistant.models import EmailMessage
    from suppliers.models import SupplierReplyFile
    out = {"saved": 0, "waiting": 0, "error": ""}
    done = set(r.reply_files.values_list("message_id", "index"))
    have = set(r.reply_files.values_list("sha256", flat=True))
    own = _own()
    for msg in mail(r):
        if not theirs(msg, own):
            continue
        atts = list(msg.attachments or [])
        todo = [i for i, a in enumerate(atts) if wanted(a) and (msg.pk, i) not in done]
        if not todo:
            continue
        paths = {i: _local(atts[i]) for i in todo if _local(atts[i])}
        lacking = [i for i in todo if i not in paths]
        if lacking and fetch:
            try:
                from email_assistant.attachments import fetch_documents
                got = fetch_documents(msg, lacking)
                for i, rel in got.items():
                    atts[i] = {**atts[i], "file_path": rel}
                    paths[i] = os.path.join(str(settings.MEDIA_ROOT), rel)
                if got:   # шлях — у лист, наступного разу з диска (без сигналу — лист не «новий»)
                    EmailMessage.objects.filter(pk=msg.pk).update(attachments=atts)
            except Exception as e:  # noqa: BLE001 — пошта недоступна: покажемо, решта працює
                logger.warning("rfq reply files %s: %s", msg.pk, e)
                out["error"] = re.sub(r"^b?['\"](.*)['\"]$", r"\1", str(e).strip())[:200] or "немає з'єднання"
        out["waiting"] += len([i for i in lacking if i not in paths])
        for i in todo:
            path = paths.get(i)
            if not path:
                continue
            try:
                with open(path, "rb") as fh:
                    data = fh.read(FILE_MAX + 1)
            except OSError:
                continue
            if not data or len(data) > FILE_MAX:
                continue
            sha = hashlib.sha256(data).hexdigest()
            if sha in have:
                continue
            name = str(atts[i].get("name") or "attachment")[:255]
            try:
                with transaction.atomic():
                    SupplierReplyFile.objects.create(
                        recipient=r, message=msg, index=i, name=name, size=len(data), sha256=sha,
                        content_type=str(atts[i].get("content_type") or "")[:100], received_at=msg.sent_at,
                        file=ContentFile(data, name=name))
            except IntegrityError:          # паралельно вже зберегли
                pass
            else:
                out["saved"] += 1
            have.add(sha)
    return out


def snippet(msg, n=SNIPPET) -> str:
    from email_assistant.quotes import new_text, strip_signature
    text = strip_signature(new_text(msg.body_text or "")) or (msg.body_text or "")
    text = re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", text)).strip()
    return text if len(text) <= n else text[:n].rstrip() + "…"


def can_read(msg, user) -> bool:
    return bool(msg.account_id) and msg.account.user_id == user.pk


def icon(name) -> str:
    ext = str(name or "").lower().rsplit(".", 1)[-1] if "." in str(name or "") else ""
    for key, ic in ICONS:
        if ext.startswith(key):
            return ic
    return "📎"


def size_text(n) -> str:
    n = n or 0
    return f"{n / 1024 / 1024:.1f} МБ" if n >= 1024 * 1024 else f"{max(1, round(n / 1024))} КБ"


def peek(r, user):
    """Підказка при наведенні на «✉️ відповідь»: від кого, коли, тема, початок листа, вкладення. Лише власнику."""
    msg = r.reply_message
    if msg is None or not can_read(msg, user):
        return None
    return {"from": msg.from_name or msg.from_email, "email": msg.from_email, "at": msg.sent_at,
            "subject": msg.subject or "", "text": snippet(msg),
            "files": [a.get("name") for a in (msg.attachments or []) if wanted(a)][:8]}


def panel(req, r, user, row=None) -> dict:
    """Дані панелі рядка (кого стосується, пропозиція, файли, листування, історія)."""
    from suppliers.models import SupplierEvent
    own = _own()
    letters = []
    for m in mail(r):
        mine = can_read(m, user)
        ours = not theirs(m, own)
        files = [{"name": a.get("name"), "icon": icon(a.get("name")), "size": size_text(a.get("size"))}
                 for a in (m.attachments or []) if wanted(a)]
        letters.append({
            "pk": m.pk, "at": m.sent_at, "ours": ours, "reply": m.pk == r.reply_message_id,
            "sent": m.pk == r.sent_message_id, "readable": mine,
            "who": (m.from_name or m.from_email) if mine else ("ми" if ours else r.supplier.name),
            "email": m.from_email if mine else "",
            "subject": (m.subject or "") if mine else "",
            "short": snippet(m, 220) if mine else "",
            "full": snippet(m, FULL_MAX) if mine else "",
            "files": files,
        })
    letters.reverse()                          # найновіші вгорі
    files = [{"obj": f, "icon": icon(f.name), "size": size_text(f.size)} for f in r.reply_files.select_related("message")]
    owner = (req.account.user if req.account_id and req.account and req.account.user_id else None)
    events = SupplierEvent.objects.filter(request=req, supplier=r.supplier).select_related("user")[:30]
    ai = r.ai or {}
    return {
        "r": r, "row": row or {}, "letters": letters, "files": files, "events": events,
        "hidden_mail": any(not x["readable"] for x in letters), "owner": owner,
        "request_files": list(req.files.all()), "ai": ai,
        "conditions": ai.get("conditions") or "",
    }
