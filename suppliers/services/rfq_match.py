"""
suppliers/services/rfq_match.py — 🔎 Відповідь постачальника, що прийшла не в гілку запиту: окремим листом, з іншої
адреси (frank@bld-pcb.com замість charles@jypcba.com), без номера «RFQ-…» — але з номером деталі з запиту
(«TM270402-04H») і назвою фірми («--JY Electronics»).

  keys(req)              — ключі запиту: номери деталей / плат з назви й файлів запиту (≥ KEY_MIN знаків, є і літери, і
                           цифри; без розмірів «1x34x85»), нормалізовані (без розділювачів, верхній регістр);
  name_variants(name)    — назва постачальника для пошуку в листі: «jyelectronics», «hkvictory», «sunkingcircuits»;
  reasons(msg, req)      — чому лист схожий на відповідь на цей запит: номер запиту, ключ (у темі / тексті / файлах);
  recipient_for(msg, req)— (отримувач, чому) — відправник цього постачальника (адреса / домен) або назва фірми в листі;
  auto(msg)              — новий лист (rfq.on_incoming, коли гілка й номер не підійшли): ключ запиту + рівно один
                           отримувач за відправником / назвою → link; інакше — нічого (покаже «Знайти пропущені»);
  link(r, msg, user)     — лист (і вся його гілка) → листування рядка: extra_threads, «📥 Відповів», файли, подія;
  missed(req, user)      — «🔎 Знайти пропущені відповіді»: листи з власних скриньок людини після надсилання, ще не в
                           листуванні жодного рядка, схожі на відповідь (номер, ключ, відправник чи назва фірми
                           з отримувачів) → [{msg, reasons, rid, score}], найсхожіші першими;
  skip(req, msg_pk)      — «✕ Не відповідь» — більше не пропонувати.
"""
import logging
import re
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

KEY_MIN = 6
SCAN_MAX = 3000                 # листів у «Знайти пропущені» (найновіші після надсилання)
SHOW_MAX = 50
AUTO_DAYS = 180                 # авто-прив'язка — лише до запитів, надісланих за стільки днів
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*[A-Za-z0-9]")       # «_», «.», пробіл — розділювачі
_DIM_RE = re.compile(r"^\d+(x\d+)+$", re.I)
_LEGAL_RE = re.compile(
    r"\b(gmbh|co|kg|ag|ug|ohg|mbh|llc|ltd|limited|inc|corp|corporation|company|bv|b\.v|sa|s\.a|srl|s\.r\.l|oy|ab|aps|"
    r"llp|plc|spa|s\.p\.a|sas|sarl|kft|sp\.?\s?z\.?o\.?o|public)\b\.?", re.I)


def norm(text) -> str:
    return re.sub(r"[^0-9a-zа-яіїєґ]+", "", str(text or "").lower())


def keys(req) -> set:
    """Номери деталей / плат із назви запиту й назв його файлів: «TM270402-04H» → «tm27040204h»."""
    import os
    texts = [req.title or ""] + [os.path.splitext(f.name)[0] for f in req.files.all()]
    out = set()
    for t in texts:
        for w in _WORD_RE.findall(t):
            digits = sum(c.isdigit() for c in w)
            letters = sum(c.isalpha() for c in w)
            if digits < 3 or not letters or _DIM_RE.match(w) or w.upper().startswith("RFQ-"):
                continue
            k = norm(w)
            if len(k) >= KEY_MIN:
                out.add(k)
    return out


def name_variants(name) -> set:
    """«JY Electronics» → {«jyelectronics»}, «HK Victory Electronic Technology Co., Ltd» → {«hkvictoryelectronic…»,
    «hkvictory»}; коротше за 5 знаків — ні (випадкові збіги)."""
    base = str(name or "").split("(")[0]
    words = [w for w in re.findall(r"[0-9a-zа-яіїєґ]+", _LEGAL_RE.sub(" ", base.lower())) if w]
    out = set()
    if words:
        out.add("".join(words))
        if len(words) > 2:
            out.add("".join(words[:2]))
    from suppliers.services import match
    out.add(match.name_key(base))
    return {v for v in out if len(v) >= 5}


def _text(msg, body=3000) -> str:
    names = " ".join(str(a.get("name") or "") for a in (msg.attachments or []) if isinstance(a, dict))
    return norm(f"{msg.subject or ''} {msg.from_name or ''} {names} {(msg.body_text or '')[:body]}")


def reasons(msg, req, req_keys=None) -> list:
    from suppliers.services.rfq import CODE_RE
    out = []
    if req.code and req.code in CODE_RE.findall(f"{msg.subject or ''} {(msg.body_text or '')[:4000]}"):
        out.append(f"номер запиту {req.code}")
    text = _text(msg, 4000)
    for k in sorted(req_keys if req_keys is not None else keys(req)):
        if k in text:
            out.append(f"номер з запиту «{k.upper()}»")
            break
    return out


def recipient_for(msg, req, recipients=None):
    """(отримувач, чому) або (None, ""): відправник — адреса / домен постачальника рядка; інакше назва фірми в темі,
    імені відправника, назвах файлів чи початку тексту. Кілька різних — None (людина вибере)."""
    from suppliers.models import SupplierRequestRecipient as R
    from suppliers.services import match
    rows = recipients if recipients is not None else list(
        req.recipients.exclude(state=R.PLANNED).select_related("supplier"))
    sender = match.addr(msg.from_email)
    domain = sender.split("@", 1)[1] if "@" in sender else ""
    by_sender = []
    for r in rows:
        emails, domains = match.keys(r.supplier)
        if sender and (sender == (r.email or "").lower() or sender in emails or
                       (domain and not match.is_free(domain) and match.domain_matches(domain, domains))):
            by_sender.append(r)
    if len({r.pk for r in by_sender}) == 1:
        return by_sender[0], f"відправник — {by_sender[0].supplier.name}"
    text = _text(msg)
    by_name = [r for r in rows if any(v in text for v in name_variants(r.supplier.name))]
    if len({r.pk for r in by_name}) == 1:
        return by_name[0], f"у листі назва «{by_name[0].supplier.name}»"
    return None, ""


def _linked_ids(req) -> tuple:
    """(гілки, листи) уже в листуванні рядків запиту."""
    threads, msgs = set(), set()
    for r in req.recipients.prefetch_related("extra_threads"):
        threads |= {t for t in [r.thread_id] if t} | {t.pk for t in r.extra_threads.all()}
        msgs |= {m for m in (r.sent_message_id, r.reply_message_id) if m}
    return threads, msgs


def link(r, msg, user=None) -> bool:
    """Лист і його гілка → листування рядка (extra_threads), «📥 Відповів», відповідь (якщо ще не було), файли,
    подія. Уже там — False."""
    from suppliers.models import SupplierEvent, SupplierRequest, SupplierRequestRecipient as R
    threads, msgs = _linked_ids(r.request)
    if msg.pk in msgs or (msg.thread_id and msg.thread_id in threads):
        return False
    if msg.thread_id and msg.thread_id != r.thread_id:
        r.extra_threads.add(msg.thread_id)
    updates = {}
    if r.reply_message_id is None:
        updates["reply_message"] = msg
    if r.state in (R.SENT, R.FAILED, R.SILENT):
        updates.update(state=R.REPLIED, replied_at=msg.sent_at or timezone.now())
    if updates:
        R.objects.filter(pk=r.pk).update(**updates)
        for k, v in updates.items():
            setattr(r, k, v)
    SupplierRequest.objects.filter(pk=r.request_id, status=SupplierRequest.SENT).update(status=SupplierRequest.ANSWERED)
    SupplierEvent.objects.create(supplier=r.supplier, kind=SupplierEvent.REQUEST, request=r.request, message=msg,
                                 user=user, text=f"📥 Ще лист до запиту {r.request.code}: «{(msg.subject or '')[:120]}»")
    try:
        from suppliers.services.rfq_replies import collect
        collect(r)
    except Exception:  # noqa: BLE001 — файли забере панель рядка
        logger.warning("rfq link: reply files for recipient %s failed", r.pk, exc_info=True)
    return True


def auto(msg) -> int:
    """Новий лист, який не підійшов ні гілкою, ні номером запиту: номер деталі з відкритого запиту + рівно один
    отримувач за відправником / назвою фірми → прив'язати. → скільки прив'язано."""
    from suppliers.models import SupplierRequest, SupplierRequestRecipient as R
    since = timezone.now() - timedelta(days=AUTO_DAYS)
    text = _text(msg, 4000)
    if not text:
        return 0
    found = 0
    reqs = SupplierRequest.objects.filter(status__in=[SupplierRequest.SENT, SupplierRequest.ANSWERED],
                                          sent_at__gte=since).prefetch_related("files")
    for req in reqs:
        ks = keys(req)
        if not ks or not any(k in text for k in ks):
            continue
        r, _why = recipient_for(msg, req)
        if r is not None and link(r, msg):
            found += 1
    return found


def missed(req, user, limit=SHOW_MAX) -> list:
    """Листи з власних скриньок людини після надсилання запиту, ще не в листуванні рядків, схожі на відповідь."""
    from email_assistant.crm_link import is_own, own_addresses
    from email_assistant.models import EmailMessage
    from suppliers.models import SupplierRequestRecipient as R
    from suppliers.services import match
    if not req.sent_at:
        return []
    rows = list(req.recipients.exclude(state=R.PLANNED).select_related("supplier"))
    if not rows:
        return []
    threads, msgs = _linked_ids(req)
    skip = {int(x) for x in (req.missed_skip or []) if str(x).isdigit()}
    own = own_addresses()
    sender_keys = []
    for r in rows:
        emails, domains = match.keys(r.supplier)
        sender_keys.append((emails | {(r.email or "").lower()}, domains))
    req_keys = keys(req)
    qs = (EmailMessage.objects.filter(account__user=user, sent_at__gte=req.sent_at - timedelta(days=1),
                                      is_deleted=False, is_spam=False)
          .exclude(folder__in=[EmailMessage.FOLDER_SENT, EmailMessage.FOLDER_DRAFT, EmailMessage.FOLDER_SPAM,
                               EmailMessage.FOLDER_TRASH])
          .exclude(pk__in=msgs | skip).exclude(thread_id__in=threads)
          .select_related("account").order_by("-sent_at")[:SCAN_MAX])
    out = []
    for m in qs:
        if is_own(m.from_email, own):
            continue
        why = reasons(m, req, req_keys)
        sender = match.addr(m.from_email)
        dom = sender.split("@", 1)[1] if "@" in sender else ""
        from_supplier = any(sender in e or (dom and not match.is_free(dom) and match.domain_matches(dom, d))
                            for e, d in sender_keys)
        if not (why or from_supplier):     # лише назва фірми без номера й без її адреси — не пропонувати
            continue
        r, who = recipient_for(m, req, rows)
        score = 3 * any(w.startswith("номер запиту") for w in why) + 2 * any(w.startswith("номер з") for w in why) \
            + 2 * from_supplier + (1 if r is not None else 0)
        if who:
            why.append(who)
        out.append({"msg": m, "reasons": why, "rid": r.pk if r is not None else None, "score": score})
    out.sort(key=lambda x: (-x["score"], -(x["msg"].sent_at.timestamp() if x["msg"].sent_at else 0)))
    return out[:limit]


def skip(req, msg_pk):
    from suppliers.models import SupplierRequest
    cur = [int(x) for x in (req.missed_skip or []) if str(x).isdigit()]
    if int(msg_pk) not in cur:
        cur = (cur + [int(msg_pk)])[-500:]
        SupplierRequest.objects.filter(pk=req.pk).update(missed_skip=cur)
        req.missed_skip = cur
