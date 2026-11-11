"""
suppliers/services/discover.py — 🔎 Постачальники з пошти: хто з листування ваших скриньок схожий на постачальника,
але його ще немає в Minerva. Без AI: співрозмовники за період групуються за доменом (поштовики — за адресою);
не беруться свої адреси, клієнти CRM (email / домен), уже відомі постачальники, сервіси (перевізники, банки,
маркетплейси), розсилки й «не постачальник» (SupplierMailIgnore). Бал — за словами закупівлі в темах і текстах
(Quotation, Proforma, Auftragsbestätigung, MOQ, lead time, PCB…), за нашими запитами до них (RFQ, Anfrage) і
кількістю листування; від MIN_SCORE. Перевірка AI (ai.classify_candidates) — на вимогу.
"""
import datetime
import re
from collections import defaultdict

from django.utils import timezone

from suppliers.services import match

DAYS = 365
SCAN_MAX = 6000
MIN_SCORE = 3
SHOW_MAX = 80
SUPPLY_RE = re.compile(
    r"\b(quotation|quote|offer|proforma|pro-?forma|PI\b|invoice|order confirmation|sales order|purchase order|"
    r"price ?list|catalog(?:ue)?|datasheet|MOQ|lead ?time|delivery time|production|manufactur\w*|factory|"
    r"samples?|PCB|PCBA|gerber|stack-?up|cable assembl\w*|harness|connector|antenna|component|"
    r"angebot|auftragsbestätigung|lieferschein|lieferzeit|preisliste|rechnung|muster|fertigung|hersteller|"
    r"пропозиці\w*|рахунок|прайс|виробни\w*|зразк\w*)", re.I)
OUR_REQUEST_RE = re.compile(r"\b(RFQ|request for (?:quotation|quote|offer)|inquiry|enquiry|anfrage|"
                            r"bitte um (?:ein )?angebot|please (?:quote|send (?:us )?(?:a |your )?(?:offer|quotation))|"
                            r"запит)\b", re.I)


def _known(own):
    """Що не пропонувати: клієнти CRM (адреси й домени), постачальники, «не постачальник»."""
    from crm.models import Customer
    from suppliers.models import SupplierMailIgnore
    idx = match.Index()
    cust_emails = {e.lower() for e in Customer.objects.exclude(email="").values_list("email", flat=True)}
    cust_domains = {e.split("@", 1)[1] for e in cust_emails if "@" in e} - set(match.FREE_MAIL_DOMAINS)
    ignored = set(SupplierMailIgnore.objects.values_list("key", flat=True))
    return idx, cust_emails, cust_domains, ignored


def candidates(user, days=DAYS, date_from=None, date_to=None) -> list:
    """[{key, domain, email, name, mails, incoming, outgoing, score, why, subjects, last, message_pk, addresses}]"""
    from email_assistant.categorize import _is_service, counterparts
    from email_assistant.crm_link import own_addresses
    from email_assistant.models import EmailMessage
    from suppliers.services.mailscan import period
    date_from, date_to = period(date_from, date_to, days)
    start = timezone.make_aware(datetime.datetime.combine(date_from, datetime.time.min))
    end = timezone.make_aware(datetime.datetime.combine(date_to + datetime.timedelta(days=1), datetime.time.min))
    own = own_addresses()
    idx, cust_emails, cust_domains, ignored = _known(own)
    groups = {}
    qs = (EmailMessage.objects.filter(account__user=user, is_deleted=False, is_spam=False, sent_at__gte=start,
                                      sent_at__lt=end)
          .exclude(folder__in=[EmailMessage.FOLDER_DRAFT, EmailMessage.FOLDER_SPAM, EmailMessage.FOLDER_TRASH])
          .exclude(category__in=["newsletter", "internal", "customer"])
          .only("pk", "subject", "body_text", "from_email", "from_name", "to_emails", "cc_emails", "folder",
                "sent_at", "category", "thread_id")
          .order_by("-sent_at"))
    for m in qs[:SCAN_MAX]:
        for a in counterparts(m, own):
            a = match.addr(a)
            if not a or a in cust_emails or _is_service(a):
                continue
            domain = a.split("@", 1)[1]
            free = match.is_free(domain)
            if (not free and domain in cust_domains) or idx.for_email(a) is not None:
                continue
            key = a if free else "@" + domain
            if key in ignored or a in ignored:
                continue
            g = groups.setdefault(key, {
                "key": key, "domain": "" if free else domain, "email": a, "name": "", "mails": 0, "incoming": 0,
                "outgoing": 0, "supply": 0, "requests": 0, "subjects": [], "last": None, "message_pk": None,
                "addresses": set(), "threads": set()})
            g["mails"] += 1
            g["addresses"].add(a)
            g["threads"].add(m.thread_id)
            incoming = match.addr(m.from_email) == a
            g["incoming" if incoming else "outgoing"] += 1
            if incoming and m.from_name and not g["name"]:
                g["name"] = m.from_name.strip(' "\'')[:120]
            if g["last"] is None:
                g["last"], g["message_pk"] = m.sent_at, m.pk
            if incoming and g["message_pk"] and not g.get("_in_msg"):
                g["message_pk"], g["_in_msg"] = m.pk, True     # створювати — з їхнього листа (підпис)
            text = f"{m.subject or ''}\n{(m.body_text or '')[:3000]}"
            if SUPPLY_RE.search(text):
                g["supply"] += 1
            if not incoming and OUR_REQUEST_RE.search(text):
                g["requests"] += 1
            subj = " ".join((m.subject or "").split())[:90]
            if subj and subj not in g["subjects"] and len(g["subjects"]) < 4:
                g["subjects"].append(subj)
    out = []
    for g in groups.values():
        score = g["supply"] + 2 * g["requests"] + (1 if g["incoming"] and g["outgoing"] else 0) \
            + min(len(g["threads"]), 4) // 2
        if score < MIN_SCORE:
            continue
        why = []
        if g["requests"]:
            why.append(f"наші запити: {g['requests']}")
        if g["supply"]:
            why.append(f"листів про ціни / замовлення / виробництво: {g['supply']}")
        if g["incoming"] and g["outgoing"]:
            why.append("листуємось в обидва боки")
        g.pop("_in_msg", None)
        out.append({**g, "score": score, "why": why, "addresses": sorted(g["addresses"])[:5],
                    "threads": len(g["threads"])})
    out.sort(key=lambda g: (-g["score"], -(g["last"].timestamp() if g["last"] else 0)))
    return out[:SHOW_MAX]


def ignore(key, user=None) -> bool:
    from suppliers.models import SupplierMailIgnore
    key = str(key or "").strip().lower()[:254]
    if not key or ("@" not in key):
        return False
    SupplierMailIgnore.objects.get_or_create(key=key, defaults={"user": user})
    return True
