"""Терміни з архіву пошти за маскою → календар Minerva (після ✓ або «без підтвердження»).

Порядок: маска (скриньки, папки, період, відправник, тема, категорія, клієнт CRM, проєкт) → відбір без AI (у листі є
дата або слово-термін) → один запит AI на лист (з датою листа — відносні «до п'ятниці» рахуються від неї) →
перевірка: дата розбирається й лежить у розумних межах, цитата справді є в листі → FoundDeadline.
Перевірений лист позначається (DeadlineScanMark) — повторний пошук не витрачає AI на нього.
"""
from __future__ import annotations

import datetime
import json
import logging
import re
import threading

from django.db import connection
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

MAX_PER_RUN = 1000
DATE_HINT = re.compile(
    r"\b\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\b|\b\d{4}-\d{2}-\d{2}\b|\bKW\s?\d{1,2}\b"
    r"|\b\d{1,2}\.?\s+(?:jan|feb|mär|mar|apr|mai|may|jun|jul|aug|sep|okt|oct|nov|dez|dec|січ|лют|бер|кві|тра|чер"
    r"|лип|сер|вер|жов|лис|гру)\w*"
    r"|\b(?:bis|spätestens|deadline|due|until|termin|frist|liefertermin|до\s+\d|не пізніше|строк|термін)\b",
    re.I)


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def messages_for(user, mask: dict):
    """Листи користувача за маскою (лише його скриньки; без спаму й кошика)."""
    from email_assistant.models import EmailMessage
    qs = EmailMessage.objects.filter(account__user=user, is_spam=False, is_deleted=False) \
        .exclude(folder=EmailMessage.FOLDER_TRASH)
    if mask.get("accounts"):
        qs = qs.filter(account_id__in=[int(a) for a in mask["accounts"] if str(a).isdigit()])
    if mask.get("folder") == "inbox":
        qs = qs.filter(folder=EmailMessage.FOLDER_INBOX)
    elif mask.get("folder") == "sent":
        qs = qs.filter(folder=EmailMessage.FOLDER_SENT)
    tz = timezone.get_current_timezone()
    for key, op in (("date_from", "gte"), ("date_to", "lt")):
        value = mask.get(key)
        if value:
            d = datetime.date.fromisoformat(value) + datetime.timedelta(days=1 if key == "date_to" else 0)
            qs = qs.filter(**{f"sent_at__{op}": timezone.make_aware(datetime.datetime.combine(d, datetime.time.min),
                                                                    tz)})
    if mask.get("sender"):
        qs = qs.filter(from_email__icontains=mask["sender"].strip().lstrip("@"))
    if mask.get("subject"):
        qs = qs.filter(subject__icontains=mask["subject"].strip())
    if mask.get("category"):
        qs = qs.filter(category=mask["category"])
    if mask.get("customer"):
        qs = qs.filter(thread__crm_customer_id=mask["customer"])
    if mask.get("project"):
        from projects.models import Project
        from projects.services import project_messages
        project = Project.objects.filter(pk=mask["project"]).first()
        qs = qs.filter(pk__in=project_messages(project).values("pk")) if project else qs.none()
    return qs.order_by("-sent_at")


def candidates(user, mask: dict, limit: int = MAX_PER_RUN) -> tuple[int, list]:
    """(скільки листів за маскою, листи для AI: є дата / слово-термін, ще не перевірені, без вже внесених термінів)."""
    from calendar_app.models import CalendarEvent, DeadlineScanMark
    from email_assistant.quotes import new_text
    qs = messages_for(user, mask)
    total = qs.count()
    done = set(DeadlineScanMark.objects.filter(user=user).values_list("email_message_id", flat=True))
    legacy = set(CalendarEvent.objects.filter(user=user, event_type=CalendarEvent.TYPE_DEADLINE)
                 .exclude(email_message=None).values_list("email_message_id", flat=True))
    out = []
    for m in qs.only("pk", "subject", "body_text", "sent_at").iterator(chunk_size=300):
        if m.pk in done or m.pk in legacy:
            continue
        if DATE_HINT.search(f"{m.subject}\n{new_text(m.body_text or '')[:6000]}"):
            out.append(m)
            if len(out) >= limit:
                break
    return total, out


PROMPT = (
    "Ти витягуєш із ділового листа терміни й дедлайни: строки поставки, оплати, відповіді, подання документів, "
    "дзвінки й зустрічі з конкретною датою.\n"
    "Дата листа: {date}. Відносні дати («до п'ятниці», «next week», «in zwei Wochen») рахуй від неї.\n"
    "Поверни ТІЛЬКИ JSON-масив: [{{\"title\": \"коротко, до 80 символів, мовою листа\", \"date\": \"YYYY-MM-DD\", "
    "\"time\": \"HH:MM або порожньо\", \"quote\": \"дослівна цитата з листа, до 160 символів\"}}].\n"
    "Лише терміни дій або подій. Не бери дати минулих подій, дати в підписах, номери документів. Немає — [].\n\n"
    "Тема: {subject}\nВід: {sender}\nТекст листа (без процитованої історії):\n{text}"
)


def extract(message, user=None) -> list[dict]:
    """Терміни з одного листа через AI; кожен перевірено (дата, межі, цитата є в листі)."""
    from email_assistant import ai_helper
    from email_assistant.quotes import new_text
    text = new_text(message.body_text or "")[:5000]
    sent = timezone.localtime(message.sent_at) if message.sent_at else timezone.localtime()
    raw = (ai_helper._call_ai_direct(PROMPT.format(date=f"{sent:%Y-%m-%d (%A)}", subject=message.subject or "",
                                                   sender=message.from_email or "", text=text),
                                     max_tokens=900, user=user) or "").strip()
    m = re.search(r"\[.*\]", raw, re.S)
    try:
        items = json.loads(m.group(0)) if m else []
    except ValueError:
        items = []
    hay = _norm_ws(f"{message.subject}\n{text}")
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        try:
            day = datetime.date.fromisoformat(str(it.get("date", ""))[:10])
        except ValueError:
            continue
        if not (sent.date() - datetime.timedelta(days=30) <= day <= sent.date() + datetime.timedelta(days=730)):
            continue
        quote = str(it.get("quote") or "").strip().strip("«»\"'„“")
        if not quote or _norm_ws(quote)[:60] not in hay:
            continue   # цитати немає в листі — можливо, вигадане: не беремо
        t = re.fullmatch(r"(\d{1,2}):(\d{2})", str(it.get("time") or "").strip())
        moment = datetime.time(int(t.group(1)) % 24, int(t.group(2)) % 60) if t else datetime.time(9, 0)
        out.append({"title": (str(it.get("title") or "") or message.subject or "Термін")[:300].strip(),
                    "due_at": timezone.make_aware(datetime.datetime.combine(day, moment)),
                    "all_day": not t, "quote": quote[:500]})
    return out


def scan(user, mask: dict, limit: int = 200, progress=None) -> tuple[int, int]:
    """(перевірено листів AI, нових термінів)."""
    from calendar_app.models import CalendarSettings, DeadlineScanMark, FoundDeadline
    _, msgs = candidates(user, mask, limit)
    auto = CalendarSettings.for_user(user).deadlines_auto
    found = 0
    for i, msg in enumerate(msgs, 1):
        items = extract(msg, user)
        for it in items:
            obj, created = FoundDeadline.objects.get_or_create(
                user=user, email_message=msg, due_at=it["due_at"], title=it["title"],
                defaults={"all_day": it["all_day"], "quote": it["quote"]})
            if created:
                found += 1
                if auto:
                    accept(obj)
        DeadlineScanMark.objects.update_or_create(user=user, email_message=msg, defaults={"found": len(items)})
        if progress and i % 10 == 0:
            progress(i, len(msgs), found)
    return len(msgs), found


def accept(d):
    from calendar_app.models import CalendarEvent, CalendarSettings, FoundDeadline
    msg = d.email_message
    thread = getattr(msg, "thread", None)
    desc = "\n".join(x for x in (f"«{d.quote}»" if d.quote else "", f"✉️ {msg.subject}" if msg.subject else "",
                                 f"від {msg.from_email}" if msg.from_email else "") if x)
    ev = CalendarEvent.objects.create(
        user=d.user, title=d.title[:300], description=desc, event_type=CalendarEvent.TYPE_DEADLINE,
        start_at=d.due_at, all_day=d.all_day, email_message=msg,
        crm_customer=getattr(thread, "crm_customer", None) if thread else None,
        remind_minutes_before=CalendarSettings.for_user(d.user).default_remind_minutes)
    d.status, d.event = FoundDeadline.ADDED, ev
    d.save(update_fields=["status", "event"])
    return ev


def dismiss(d):
    from calendar_app.models import FoundDeadline
    d.status = FoundDeadline.DISMISSED
    d.save(update_fields=["status"])


_lock = threading.Lock()


def start_scan(user, mask: dict, limit: int):
    from calendar_app.models import CalendarSettings
    caller = threading.current_thread()

    def status(text):
        CalendarSettings.objects.filter(user=user).update(deadline_scan_status=text[:255])

    def run():
        if not _lock.acquire(blocking=False):
            status("⏳ Пошук уже йде — зачекайте")
            return
        try:
            status("⏳ Відбір листів…")
            seen, found = scan(user, mask, limit, progress=lambda done, total, n: status(
                f"⏳ AI перевірив {done} з {total} листів, термінів {n}"))
            status(f"✅ {timezone.localtime():%d.%m.%Y %H:%M} · перевірено листів {seen}, нових термінів {found}")
        except Exception as e:  # noqa: BLE001
            logger.exception("deadline scan failed: %s", e)
            status(f"❌ Пошук перервано ({type(e).__name__}) — спробуйте ще раз")
        finally:
            _lock.release()
            if threading.current_thread() is not caller:
                connection.close()

    threading.Thread(target=run, daemon=True).start()
