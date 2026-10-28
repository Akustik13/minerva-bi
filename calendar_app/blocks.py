"""Відправники, від яких у календар нічого не заносити (запрошення, терміни з пошти, нагадування з листів).

Два шляхи сюди:
  - у календарі на події з листа — «🙅 Не потрібен» (лише ця подія) або «🚫 Більше не заносити від …»
    (адреса чи весь @домен): спершу видно, що саме прибереться, потім усе від нього зникає з календаря;
  - лист позначено спамом (email_assistant/spam.py) — те саме з причиною «спам».
Спам-лист і спам-відправник скриньки в календар не потрапляють ніколи (is_blocked). Рішення — CalendarSenderBlock.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

SENDER, DOMAIN = "sender", "domain"
NOT_NEEDED, SPAM = "not_needed", "spam"
PREVIEW_LIMIT = 40


def counterpart(message, own=None) -> str:
    """Від кого цей лист для календаря: відправник, а для нашого листа — перший зовнішній отримувач."""
    from email_assistant.categorize import counterparts
    from email_assistant.crm_link import own_addresses
    if message is None:
        return ""
    found = counterparts(message, own or own_addresses())
    return found[0] if found else ""


def key_for(address: str, scope: str = SENDER) -> str:
    address = (address or "").strip().lower()
    if "@" not in address:
        return ""
    return "@" + address.split("@", 1)[1] if scope == DOMAIN else address


def normalize_key(value: str) -> str:
    """Введене вручну: «a@b.c», «@b.c» або «b.c» → ключ; інакше ''."""
    value = (value or "").strip().lower().strip("<>")
    if not value:
        return ""
    if "@" in value:
        return value if not value.startswith("@") or "." in value else ""
    return "@" + value if "." in value else ""


def _matches(address: str, key: str) -> bool:
    from email_assistant.spam import matches
    return matches(address, key)


def keys(user) -> list:
    from .models import CalendarSenderBlock
    return list(CalendarSenderBlock.objects.filter(user=user).values_list("key", flat=True))


def blocked_key(user, address: str, user_keys=None) -> str:
    """Ключ блоку, який підходить до адреси, або ''."""
    for key in keys(user) if user_keys is None else user_keys:
        if _matches(address, key):
            return key
    return ""


def is_blocked(user, message, user_keys=None, own=None) -> bool:
    """Лист не повинен давати подій у календарі: спам, спам-відправник скриньки або відправник у блоці."""
    if message is None or user is None:
        return False
    if getattr(message, "is_spam", False):
        return True
    if blocked_key(user, counterpart(message, own), user_keys):
        return True
    try:
        from email_assistant.spam import is_spam_sender
        return bool(message.account_id) and is_spam_sender(message.account, message.from_email)
    except Exception:  # noqa: BLE001 — немає скриньки / правил
        return False


def _collect(user, group_of):
    """Події, запрошення й терміни з листів, для яких group_of(лист) повертає непорожню групу.
    → {група: {"events": [...], "invites": [...], "deadlines": [...]}}"""
    from .models import CalendarEvent, FoundDeadline, MeetingInvite
    cache, out = {}, {}

    def group(msg):
        if msg is None:
            return ""
        if msg.pk not in cache:
            cache[msg.pk] = group_of(msg) or ""
        return cache[msg.pk]

    sources = (
        ("events", CalendarEvent.objects.filter(user=user).exclude(email_message=None)
         .select_related("email_message", "email_message__account").order_by("start_at")),
        ("invites", MeetingInvite.objects.filter(user=user).exclude(email_message=None)
         .exclude(status__in=[MeetingInvite.DISMISSED, MeetingInvite.CANCELLED])
         .select_related("email_message", "email_message__account").order_by("start_at")),
        ("deadlines", FoundDeadline.objects.filter(user=user).exclude(status=FoundDeadline.DISMISSED)
         .select_related("email_message", "email_message__account", "target__account").order_by("due_at")),
    )
    for kind, qs in sources:
        for obj in qs:
            g = group(obj.email_message)
            if g:
                out.setdefault(g, {"events": [], "invites": [], "deadlines": []})[kind].append(obj)
    return out


def _from_mail(user, key):
    """Усе в календарі користувача, що прийшло з листів цього відправника: події, запрошення, терміни."""
    from email_assistant.crm_link import own_addresses
    own = own_addresses()
    found = _collect(user, lambda msg: key if _matches(counterpart(msg, own), key) else "").get(key, {})
    return found.get("events", []), found.get("invites", []), found.get("deadlines", [])


def preview(user, key: str) -> dict:
    """Що прибереться з календаря, якщо заблокувати відправника (без змін)."""
    from .models import FoundDeadline, MeetingInvite
    events, invites, deadlines = _from_mail(user, key)
    tz_events = [{"title": e.title, "date": e.start_at.isoformat(), "done": e.is_done} for e in events]
    return {
        "key": key,
        "events": tz_events[:PREVIEW_LIMIT],
        "n_events": len(events),
        "n_invites": sum(1 for i in invites if i.status in MeetingInvite.PENDING),
        "n_deadlines": sum(1 for d in deadlines if d.status == FoundDeadline.NEW),
        "n_remote": sum(1 for d in deadlines if d.status == FoundDeadline.ADDED and d.target_id and d.remote_id),
    }


def remove(user, events=(), invites=(), deadlines=()) -> dict:
    """Прибрати з календаря: події видалити, запрошення й терміни → «не заносити» (повторний пошук їх не поверне),
    терміни, занесені в Google / Outlook, — видалити там (якщо вдасться)."""
    from django.utils import timezone
    from .models import CalendarEvent, FoundDeadline, MeetingInvite
    events, invites, deadlines = list(events), list(invites), list(deadlines)
    remote = failed = 0
    for d in deadlines:
        if d.status == FoundDeadline.ADDED and d.target_id and d.remote_id:
            try:
                from .sync import client
                client(d.target.account).delete(d.target.remote_id, d.remote_id)
                remote += 1
            except Exception as e:  # noqa: BLE001 — Google / Outlook недоступний: подія лишиться там
                failed += 1
                logger.warning("calendar cleanup remote %s: %s", d.pk, e)
    # події вибраних запрошень і термінів теж ідуть
    event_ids = ({e.pk for e in events} | {i.event_id for i in invites if i.event_id}
                 | {d.event_id for d in deadlines if d.event_id})
    now = timezone.now()
    MeetingInvite.objects.filter(user=user, pk__in=[i.pk for i in invites]).update(
        status=MeetingInvite.DISMISSED, decided_at=now, event=None)
    FoundDeadline.objects.filter(user=user, pk__in=[d.pk for d in deadlines]).update(
        status=FoundDeadline.DISMISSED, event=None)
    MeetingInvite.objects.filter(user=user, event_id__in=event_ids).update(
        status=MeetingInvite.DISMISSED, decided_at=now, event=None)
    FoundDeadline.objects.filter(user=user, event_id__in=event_ids).update(status=FoundDeadline.DISMISSED, event=None)
    n_events = CalendarEvent.objects.filter(user=user, pk__in=event_ids).delete()[0]
    return {"events": n_events, "invites": len(invites), "deadlines": len(deadlines), "remote": remote,
            "remote_failed": failed, "removed": n_events + remote}


def purge(user, key: str) -> dict:
    """Прибрати з календаря все від відправника."""
    events, invites, deadlines = _from_mail(user, key)
    return remove(user, events, invites, deadlines)


# ── «🧹 Почистити календар»: усе, що підпадає під фільтри, — перегляд і підтвердження ──────────
def cleanup_plan(user, extra_key: str = "") -> list:
    """Групи за фільтрами: спам (лист у Спамі або відправник-спамер скриньки), кожен заблокований відправник і,
    якщо задано, extra_key — новий відправник, якого ще не заблоковано (переглянути, перш ніж додати фільтр).
    → [{"id", "key", "reason", "label", "new", "events", "invites", "deadlines", "count"}]"""
    from email_assistant.crm_link import own_addresses
    from email_assistant.spam import is_spam_sender, sender_key, spam_keys
    from .models import CalendarSenderBlock
    own = own_addresses()
    blocks = {b.key: b for b in CalendarSenderBlock.objects.filter(user=user)}
    order = sorted(blocks, key=lambda k: (k.startswith("@"), k))   # точна адреса раніше за домен
    if extra_key and extra_key not in blocks:
        order.insert(0, extra_key)
    rules = {}

    def group_of(msg):
        address = counterpart(msg, own)
        for key in order:
            if _matches(address, key):
                return key
        if msg.account_id not in rules:
            rules[msg.account_id] = spam_keys(msg.account)
        if msg.is_spam or is_spam_sender(msg.account, msg.from_email, rules[msg.account_id]):
            return "spam:" + (sender_key(msg.from_email, own) or address or "?")
        return ""

    groups = []
    for gid, items in _collect(user, group_of).items():
        if gid.startswith("spam:"):
            key, reason, new = gid[5:], SPAM, False
            label = "🚫 Спам — " + key
        else:
            b = blocks.get(gid)
            key, new = gid, b is None
            reason = b.reason if b else NOT_NEEDED
            label = ("🔎 " if new else ("🚫 " if reason == SPAM else "🙅 ")) + gid
        groups.append({"id": gid, "key": key, "reason": reason, "label": label, "new": new,
                       "count": sum(len(v) for v in items.values()), **items})
    groups.sort(key=lambda g: (not g["new"], g["reason"] != SPAM, g["key"]))
    if extra_key and not any(g["id"] == extra_key for g in groups):
        groups.insert(0, {"id": extra_key, "key": extra_key, "reason": NOT_NEEDED, "label": "🔎 " + extra_key,
                          "new": extra_key not in blocks, "count": 0, "events": [], "invites": [], "deadlines": []})
    return groups


def cleanup(user, event_ids=(), invite_ids=(), deadline_ids=()) -> dict:
    """Прибрати вибране на сторінці «🧹 Почистити календар» (лише своє)."""
    from .models import CalendarEvent, FoundDeadline, MeetingInvite
    return remove(
        user,
        CalendarEvent.objects.filter(user=user, pk__in=list(event_ids)),
        MeetingInvite.objects.filter(user=user, pk__in=list(invite_ids)),
        FoundDeadline.objects.filter(user=user, pk__in=list(deadline_ids)).select_related("target__account"),
    )


def block(user, key: str, reason: str = NOT_NEEDED, do_purge: bool = True) -> dict:
    """Заблокувати відправника для календаря (і прибрати вже внесене)."""
    from .models import CalendarSenderBlock
    key = (key or "").strip().lower()
    if not key:
        return {"key": "", "removed": 0}
    obj, created = CalendarSenderBlock.objects.get_or_create(user=user, key=key, defaults={"reason": reason})
    if not created and reason == SPAM and obj.reason != SPAM:
        obj.reason = SPAM
        obj.save(update_fields=["reason"])
    result = purge(user, key) if do_purge else {"removed": 0}
    if result.get("removed"):
        CalendarSenderBlock.objects.filter(pk=obj.pk).update(removed=obj.removed + result["removed"])
    result.update({"key": key, "created": created})
    return result


def unblock(user, pk) -> bool:
    from .models import CalendarSenderBlock
    return CalendarSenderBlock.objects.filter(user=user, pk=pk).delete()[0] > 0


def unblock_spam(user, address: str) -> int:
    """«Не спам» у пошті: зняти блоки календаря з причиною «спам», що підходять до адреси."""
    from .models import CalendarSenderBlock
    gone = [b.pk for b in CalendarSenderBlock.objects.filter(user=user, reason=SPAM) if _matches(address, b.key)]
    return CalendarSenderBlock.objects.filter(pk__in=gone).delete()[0]


def not_needed(event) -> dict:
    """«🙅 Не потрібен»: прибрати одну подію; запрошення / знайдений термін, з якого вона, більше не повертаються."""
    from .models import FoundDeadline, MeetingInvite
    MeetingInvite.objects.filter(event=event).update(status=MeetingInvite.DISMISSED, event=None)
    FoundDeadline.objects.filter(event=event).update(status=FoundDeadline.DISMISSED, event=None)
    title = event.title
    event.delete()
    return {"title": title}
