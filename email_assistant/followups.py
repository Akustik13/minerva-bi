"""⏰ Чекаю реакції на надісланий лист: 📬 запит на прочитання й нагадування «немає реакції через N днів».

Звідки: галочки форми «Написати» (`options` → `track`, і для «⏰ Пізніше» — `ScheduledEmail.send_options`), правий
клік на надісланому листі «⏰ Нагадати, якщо немає реакції…» (`track_message`). Модель — `MailFollowup`.

Реакція на лист (`on_incoming`, з `sync.save_message` для кожного нового листа):
- 📬 підтвердження прочитання (MDN, RFC 8098: multipart/report; report-type=disposition-notification з
  Original-Message-ID) — `parse_mdn`; запасний шлях — тема «Read: / Gelesen: / Прочитано: …» з In-Reply-To /
  References на наш лист. «deleted» (видалено без прочитання) — одразу «⚠️ немає реакції»;
- ↩ відповідь — лист із In-Reply-To / References на наш лист, інакше в тій самій гілці від отримувача (або його
  домену). Автовідповіді («Out of Office», «Automatische Antwort»…) і повідомлення про недоставку — не реакція.

Немає реакції до `remind_at` (`run_due`, cron `mail_followups` щоцикл) — лист і Telegram власнику скриньки
(agent/notify.deliver): «⚠️ Немає реакції … — потрібне ручне втручання» з посиланням на лист у Minerva. «Немає
реакції» — `until`: reaction (ні підтвердження прочитання, ні відповіді) або reply (немає відповіді).
Багато поштових програм не надсилають підтвердження без згоди отримувача — тому відповідь теж рахується.
"""
import datetime
import logging
import re

from django.db.models import Q
from django.utils import timezone

from email_assistant.models import EmailMessage, MailFollowup

logger = logging.getLogger('email_assistant')

DAYS = (1, 2, 3, 5, 7, 14)
DEFAULT_DAYS = 3
MAX_DAYS = 60
SNOOZE_DAYS = 2
PER_LOOP = 50

READ_SUBJECT_RE = re.compile(
    r'^\s*(read|gelesen|прочитано|прочитаний|прочитан[оe]|lu|letto|le[ií]do|gelezen|přečteno|odczytan[ao]'
    r'|read receipt|lesebestätigung)\s*:', re.I)
NOT_READ_SUBJECT_RE = re.compile(r'^\s*(not read|nicht gelesen|ungelesen|не прочитано)\s*:', re.I)
NOT_REACTION_RE = re.compile(
    r'^\s*(automatic reply|automatische antwort|auto[- ]?reply|autoreply|out of office|abwesenheit|abwesend'
    r'|автовідповідь|автоответ|undeliverable|unzustellbar|nicht zustellbar|delivery status notification'
    r'|mail delivery failed|returned mail|delivery failure)', re.I)
_ID_RE = re.compile(r'<[^<>\s]+>')


# ── Параметри листа ──────────────────────────────────────────────────────────

def _on(data, key) -> bool:
    return str(data.get(key) or '').strip().lower() in ('1', 'on', 'true', 'yes')


def options(data) -> dict:
    """Галочки форми листа (POST або JSON) → {importance, read_receipt, remind_days, until}."""
    try:
        days = int(data.get('remind_days') or DEFAULT_DAYS)
    except (TypeError, ValueError):
        days = DEFAULT_DAYS
    return {
        'importance': 'important' if _on(data, 'opt_important') else '',
        'read_receipt': _on(data, 'opt_receipt'),
        'remind_days': max(1, min(days, MAX_DAYS)) if _on(data, 'opt_remind') else 0,
        'until': MailFollowup.UNTIL_REPLY if data.get('remind_until') == MailFollowup.UNTIL_REPLY
        else MailFollowup.UNTIL_REACTION,
    }


def stored(data) -> dict:
    """Те саме для «⏰ Пізніше» (ScheduledEmail.send_options): {} — нічого не вибрано."""
    o = options(data)
    return o if (o['importance'] or o['read_receipt'] or o['remind_days']) else {}


# ── Створити ─────────────────────────────────────────────────────────────────

def track(account, *, msg_id, subject='', recipients=(), sent_at=None, message=None, read_receipt=False,
          remind_days=0, until=MailFollowup.UNTIL_REACTION, user=None):
    """Надісланий лист чекає реакції (лише якщо попросили підтвердження прочитання або нагадування)."""
    if not msg_id or not (read_receipt or remind_days):
        return None
    sent_at = sent_at or timezone.now()
    f, _ = MailFollowup.objects.update_or_create(account=account, msg_id=msg_id[:500], defaults=dict(
        message=message, subject=(subject or '')[:500], recipients=[r.lower() for r in recipients][:50],
        sent_at=sent_at, read_receipt=bool(read_receipt), until=until, created_by=user,
        remind_at=(sent_at + datetime.timedelta(days=remind_days)) if remind_days else None))
    return f


def track_message(msg, days, until=MailFollowup.UNTIL_REACTION, user=None):
    """«⏰ Нагадати, якщо немає реакції…» для вже надісланого листа. ValueError — чому ні."""
    if not msg.message_id:
        raise ValueError('у листа немає Message-ID — відповідь на нього не розпізнати')
    if not _own(msg):
        raise ValueError('нагадування — лише для ваших надісланих листів')
    days = max(1, min(int(days), MAX_DAYS))
    now = timezone.now()
    sent_at = msg.sent_at or now
    remind_at = sent_at + datetime.timedelta(days=days)
    if remind_at <= now:          # лист давніший за N днів — N днів від сьогодні
        remind_at = now + datetime.timedelta(days=days)
    f, _ = MailFollowup.objects.get_or_create(account=msg.account, msg_id=msg.message_id[:500], defaults=dict(
        message=msg, subject=(msg.subject or '')[:500], sent_at=sent_at, created_by=user,
        recipients=[r.lower() for r in list(msg.to_emails or []) + list(msg.cc_emails or [])][:50]))
    f.message, f.remind_at, f.until = msg, remind_at, until
    f.notified_at, f.notify_result = None, ''
    f.status = MailFollowup.READ if f.receipt == 'displayed' else MailFollowup.WAITING
    reply = _existing_reply(f)
    if reply is not None:
        _replied(f, reply, save=False)
    f.save()
    return f


def _own(msg) -> bool:
    from email_assistant.crm_link import is_own, own_addresses
    return msg.folder == EmailMessage.FOLDER_SENT or is_own(msg.from_email, own_addresses())


# ── Підтвердження прочитання й відповіді ─────────────────────────────────────

def parse_mdn(msg):
    """Лист — підтвердження прочитання (MDN)? → {'for': '<Message-ID>', 'disposition': 'displayed' | 'deleted' |
    …} або None. msg — email.message.Message."""
    if msg.get_content_type() != 'multipart/report':
        return None
    if str(msg.get_param('report-type') or '').lower() != 'disposition-notification':
        return None
    for part in msg.walk():
        if part.get_content_type() != 'message/disposition-notification':
            continue
        text = part.as_string()
        found = re.search(r'^Original-Message-ID:\s*(<[^<>\s]+>)', text, re.I | re.M)
        disp = re.search(r'^Disposition:\s*([^\r\n]+)', text, re.I | re.M)
        if found:
            value = (disp.group(1) if disp else '').lower()
            kind = value.split(';')[-1].strip().split('/')[0].strip() if value else 'displayed'
            return {'for': found.group(1), 'disposition': kind or 'displayed'}
    return None


def _ids(data) -> set:
    refs = [data.get('in_reply_to') or ''] + list(data.get('references') or [])
    return {m for r in refs for m in _ID_RE.findall(str(r))}


def _open():
    return MailFollowup.objects.exclude(status=MailFollowup.DONE)


def on_incoming(em, data):
    """Новий лист зі скриньки (sync.save_message): копія нашого надісланого — прив'язати; підтвердження
    прочитання чи відповідь на лист, що чекає реакції, — записати."""
    if _own(em):
        if em.message_id:
            MailFollowup.objects.filter(account=em.account, msg_id=em.message_id, message__isnull=True) \
                .update(message=em)
        return None
    waiting = _open().filter(account=em.account)
    if not waiting.exists():
        return None
    mdn = (data or {}).get('mdn')
    if mdn and mdn.get('for'):
        f = waiting.filter(msg_id=mdn['for']).first()
        return _receipt(f, em, mdn.get('disposition', 'displayed')) if f else None
    ids = _ids(data or {'in_reply_to': em.in_reply_to})
    subject = em.subject or ''
    if ids and (READ_SUBJECT_RE.match(subject) or NOT_READ_SUBJECT_RE.match(subject)):
        f = waiting.filter(msg_id__in=ids).first()
        if f:
            return _receipt(f, em, 'deleted' if NOT_READ_SUBJECT_RE.match(subject) else 'displayed')
    if NOT_REACTION_RE.match(subject):
        return None
    pending = waiting.filter(replied_at__isnull=True)
    f = pending.filter(msg_id__in=ids).first() if ids else None
    if f is None and em.thread_id:
        for cand in pending.filter(message__thread_id=em.thread_id).order_by('-sent_at'):
            if (not em.sent_at or cand.sent_at <= em.sent_at) and _from_recipient(em.from_email, cand):
                f = cand
                break
    return _replied(f, em) if f else None


def _from_recipient(address, f) -> bool:
    address = (address or '').lower()
    if not address or '@' not in address:
        return False
    domain = address.split('@', 1)[1]
    return any(r == address or r.split('@', 1)[-1] == domain for r in f.recipients or [])


def _existing_reply(f):
    """Відповідь, що вже прийшла (для нагадування на старий лист)."""
    msg = f.message
    if msg is None or not msg.thread_id:
        return None
    later = EmailMessage.objects.filter(thread_id=msg.thread_id, is_deleted=False, sent_at__gt=msg.sent_at) \
        .exclude(folder=EmailMessage.FOLDER_SENT).order_by('sent_at')
    for m in later:
        if not NOT_REACTION_RE.match(m.subject or '') and not READ_SUBJECT_RE.match(m.subject or '') \
                and (m.in_reply_to == f.msg_id or _from_recipient(m.from_email, f)):
            return m
    return None


def _receipt(f, em, disposition):
    if f.read_at and f.receipt == 'displayed':
        return f
    f.read_at, f.read_by = em.sent_at or timezone.now(), (em.from_email or '')[:300]
    f.receipt = 'deleted' if 'delet' in (disposition or '') else 'displayed'
    fields = ['read_at', 'read_by', 'receipt']
    if f.receipt == 'deleted':
        if f.status != MailFollowup.REPLIED:
            f.status = MailFollowup.NO_REACTION
            fields.append('status')
        f.save(update_fields=fields)
        if f.notified_at is None:
            notify(f)
        return f
    if f.status in (MailFollowup.WAITING, MailFollowup.NO_REACTION):
        f.status = MailFollowup.READ
        fields.append('status')
    f.save(update_fields=fields)
    return f


def _replied(f, em, save=True):
    f.replied_at, f.reply = em.sent_at or timezone.now(), em
    f.status = MailFollowup.REPLIED
    if save:
        f.save(update_fields=['replied_at', 'reply', 'status'])
    return f


def satisfied(f) -> bool:
    if f.replied_at:
        return True
    return f.until == MailFollowup.UNTIL_REACTION and f.receipt == 'displayed'


# ── Нагадування ──────────────────────────────────────────────────────────────

def due(now=None):
    now = now or timezone.now()
    return (MailFollowup.objects.filter(remind_at__lte=now, notified_at__isnull=True)
            .filter(Q(status=MailFollowup.WAITING) | Q(status=MailFollowup.READ, until=MailFollowup.UNTIL_REPLY))
            .select_related('account', 'account__user', 'message').order_by('remind_at'))


def run_due(limit=PER_LOOP) -> dict:
    """Прохід cron: строк минув, реакції немає → повідомити власника скриньки (один раз)."""
    out = {'notified': 0, 'closed': 0}
    for f in due()[:limit]:
        if not MailFollowup.objects.filter(pk=f.pk, notified_at__isnull=True).update(notified_at=timezone.now()):
            continue   # уже повідомляє інший запуск
        if satisfied(f):
            out['closed'] += 1
            continue
        f.status = MailFollowup.NO_REACTION
        f.save(update_fields=['status'])
        notify(f, claimed=True)
        out['notified'] += 1
    return out


def why_text(f) -> str:
    when = lambda d: timezone.localtime(d).strftime('%d.%m.%Y %H:%M')   # noqa: E731
    if f.receipt == 'deleted':
        return f'лист видалено без прочитання ({when(f.read_at)})'
    if f.receipt == 'displayed':
        return f'прочитано {when(f.read_at)}, але відповіді немає'
    if f.read_receipt:
        return 'не підтверджено прочитання й немає відповіді'
    return 'немає відповіді'


def message_link(pk) -> str:
    """Повна адреса листа в Minerva (для листа й Telegram): /email/?msg=<pk>."""
    return _absolute(f'/email/?msg={pk}')


def _link(f) -> str:
    return message_link(f.message_id) if f.message_id else _absolute('/email/?folder=sent')


def _absolute(path) -> str:
    try:
        from config.models import SystemSettings
        s = SystemSettings.objects.filter(pk=1).first()
        if s is not None and s.site_domain:
            return s.site_url.rstrip('/') + path
    except Exception:  # noqa: BLE001
        pass
    return path


def compose(f) -> dict:
    to = ', '.join(f.recipients[:5]) or '—'
    sent = timezone.localtime(f.sent_at).strftime('%d.%m.%Y %H:%M')
    return {
        'subject': f'⚠️ Немає реакції: {f.subject or "(без теми)"}'[:200],
        'head': f'Немає реакції на лист «{f.subject or "(без теми)"}» — {why_text(f)}. Потрібне ручне втручання: '
                f'зателефонуйте або напишіть ще раз.',
        'lines': [f'Кому: {to}', f'Надіслано: {sent}',
                  f'Скринька: {f.account.email_address}'],
        'summary': '', 'link': _link(f),
    }


def notify(f, claimed=False) -> str:
    """Лист і Telegram власнику скриньки. → що записано в `notify_result`."""
    if not claimed and not MailFollowup.objects.filter(pk=f.pk, notified_at__isnull=True).update(
            notified_at=timezone.now()):
        return f.notify_result
    from agent.notify import deliver
    user = f.account.user
    try:
        text = deliver(user, compose(f), link_text='Відкрити лист') if user else 'у скриньки немає власника'
    except Exception as e:  # noqa: BLE001 — повідомлення не має зупиняти синхронізацію
        logger.warning('followup notify %s: %s', f.pk, e)
        text = f'🔕 {type(e).__name__}'
    MailFollowup.objects.filter(pk=f.pk).update(notify_result=text[:300])
    f.notify_result = text[:300]
    return text


# ── Дії людини ───────────────────────────────────────────────────────────────

def act(f, action, days=None) -> MailFollowup:
    """done — ✓ закрити; snooze — нагадати ще через N днів; stop — без нагадування (стан прочитання лишається)."""
    if action == 'done':
        f.status = MailFollowup.DONE
    elif action == 'snooze':
        days = max(1, min(int(days or SNOOZE_DAYS), MAX_DAYS))
        f.remind_at = timezone.now() + datetime.timedelta(days=days)
        f.notified_at, f.notify_result = None, ''
        f.status = MailFollowup.REPLIED if f.replied_at else (
            MailFollowup.READ if f.receipt == 'displayed' else MailFollowup.WAITING)
    elif action == 'stop':
        f.remind_at = None
        if f.status == MailFollowup.NO_REACTION:
            f.status = MailFollowup.DONE
    else:
        raise ValueError('невідома дія')
    f.save()
    return f


# ── Показ ────────────────────────────────────────────────────────────────────

def state(f) -> dict:
    """{icon, text, tone ok | wait | bad | done} — рядок у прев'ю й мітка в списку."""
    when = lambda d: timezone.localtime(d).strftime('%d.%m %H:%M')   # noqa: E731
    if f.status == MailFollowup.DONE:
        return {'icon': '✓', 'text': 'закрито', 'tone': 'done', 'short': '✓'}
    if f.replied_at:
        return {'icon': '↩', 'text': f'відповіли {when(f.replied_at)}', 'tone': 'ok', 'short': '↩ відповіли'}
    if f.status == MailFollowup.NO_REACTION:
        return {'icon': '⚠️', 'text': f'немає реакції — {why_text(f)}. Потрібне ручне втручання', 'tone': 'bad',
                'short': '⚠️ без реакції'}
    parts = []
    if f.receipt == 'displayed':
        parts.append(f'📬 прочитано {when(f.read_at)}')
    elif f.read_receipt:
        parts.append('📬 запит на прочитання — ще не підтверджено')
    if f.remind_at:
        parts.append(f'⏰ нагадаю {when(f.remind_at)}, якщо ' + (
            'не відповідять' if f.until == MailFollowup.UNTIL_REPLY or f.receipt == 'displayed'
            else 'не буде ні підтвердження прочитання, ні відповіді'))
    tone = 'ok' if f.receipt == 'displayed' and not f.remind_at else 'wait'
    short = '📬 прочитано' if f.receipt == 'displayed' else ('⏰ чекаю' if f.remind_at else '📬 запит')
    return {'icon': '⏰' if f.remind_at else '📬', 'text': ' · '.join(parts) or 'чекаю реакції', 'tone': tone,
            'short': short}


def for_messages(messages) -> dict:
    """{pk листа: MailFollowup} — мітки в списку й рядок у прев'ю."""
    pks = [m.pk for m in messages]
    out = {}
    for f in MailFollowup.objects.filter(message_id__in=pks).order_by('-pk'):
        out.setdefault(f.message_id, f)
    return out


def attach(messages):
    found = for_messages(messages)
    for m in messages:
        f = found.get(m.pk)
        m.followup = f
        m.fu_state = state(f) if f else None


def message_pks(account, *, open_only=True):
    qs = MailFollowup.objects.filter(account=account, message__isnull=False)
    if open_only:
        qs = qs.exclude(status__in=(MailFollowup.DONE, MailFollowup.REPLIED))
    return qs.values('message_id')


def counts(account) -> dict:
    qs = MailFollowup.objects.filter(account=account).exclude(status__in=(MailFollowup.DONE, MailFollowup.REPLIED))
    return {'open': qs.count(), 'bad': qs.filter(status=MailFollowup.NO_REACTION).count()}
